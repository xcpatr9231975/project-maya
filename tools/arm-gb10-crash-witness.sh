#!/usr/bin/env bash
#
# arm-gb10-crash-witness.sh -- one shot: arm the netconsole witness and turn
# off NVMe APST, so the next hard lock explains itself.
#
# Why: the GB10 hard locks leave no witnesses on the box.  journald and the
# monitor's files live on the same stalled NVMe, the kernel hard-lockup
# detector is disabled on this SoC, hung_task needs 120 s (the box dies in
# ~11 s) and the SBSA watchdog has never fired.  netconsole sends printk over
# UDP, independent of the disk, so the driver's own `I/O timeout, aborting` /
# `reset controller` lines reach another machine before the box goes dark.
# APST off removes the most likely trigger for a Samsung OEM drive that hangs
# under sustained load with a clean SMART log.
#
# See docs/2026-10-27-hardlock-0544-writeback-stall.md ("Ranked experiments",
# E0 and E1).
#
# Usage:
#   sudo tools/arm-gb10-crash-witness.sh              # netconsole + APST off
#   sudo tools/arm-gb10-crash-witness.sh --no-apst    # witness only
#   tools/arm-gb10-crash-witness.sh --status          # read-only
#   tools/arm-gb10-crash-witness.sh --dry-run         # print, change nothing
#
# Env:
#   GB10_WITNESS_DEV    uplink interface (default: default-route device)
#   GB10_WITNESS_IP     local IPv4 (default: address on that interface)
#   GB10_WITNESS_BCAST  destination IPv4 (default: that subnet's broadcast)
#   GB10_WITNESS_MAC    destination ethernet MAC (default: ff:ff:ff:ff:ff:ff)
#   GB10_WITNESS_PORT   UDP port, both ends (default 6666)
#   GB10_WITNESS_KMSG   also write a test line to /dev/kmsg (default 1)
#   GB10_WITNESS_NVME_TOOL  path to nvme-stall-mitigations.sh (default: next to this script)
#
# Notes:
#   - netconsole is armed immediately and at every boot.
#   - APST is a kernel command line change: it takes effect on the next boot.
#     Revert with: sudo tools/nvme-stall-mitigations.sh revert apst
#   - Nothing here reboots the box and nothing touches the engine.
#   - The receiver must be a *different* host: a host does not receive the
#     broadcast frames it sends itself, so socat on this box sees nothing.
#   - The default target is the subnet broadcast with the broadcast MAC, and
#     the upstream kernel docs warn that some switches suppress ethernet
#     broadcasts ("it is advised to explicitly specify the remote agents' MAC
#     addresses").  For a receiver whose IP and MAC are known, prefer
#     GB10_WITNESS_BCAST=<receiver-ip> GB10_WITNESS_MAC=<receiver-mac>.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NVME_TOOL="${GB10_WITNESS_NVME_TOOL:-$SCRIPT_DIR/nvme-stall-mitigations.sh}"

NETCONSOLE_MODPROBE="${GB10_WITNESS_MODPROBE:-/etc/modprobe.d/netconsole.conf}"
NETCONSOLE_MODULES="${GB10_WITNESS_MODULES:-/etc/modules-load.d/netconsole.conf}"
PORT="${GB10_WITNESS_PORT:-6666}"
MAC="${GB10_WITNESS_MAC:-ff:ff:ff:ff:ff:ff}"
DO_APST=1
DO_KMSG="${GB10_WITNESS_KMSG:-1}"
DRY_RUN=0
STATUS_ONLY=0

usage() {
    sed -n '3,44p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
    echo "error: $*" >&2
    exit 1
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root (use sudo)"
}

detect_net() {
    local addr prefix
    DEV="${GB10_WITNESS_DEV:-}"
    if [ -z "$DEV" ]; then
        DEV="$(ip -4 route show default 2>/dev/null |
            awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}' || true)"
    fi
    [ -n "${DEV:-}" ] || die "cannot determine the default-route interface; set GB10_WITNESS_DEV"

    if [ -n "${GB10_WITNESS_IP:-}" ]; then
        IP="$GB10_WITNESS_IP"
    else
        addr="$(ip -4 -o addr show dev "$DEV" scope global 2>/dev/null |
            awk '{print $4}' | head -1 || true)"
        [ -n "${addr:-}" ] || die "no IPv4 address on $DEV"
        IP="${addr%/*}"
        prefix="${addr#*/}"
    fi
    [ -n "${DEV:-}" ] && [ -n "${IP:-}" ] || die "could not determine interface/address"

    if [ -n "${GB10_WITNESS_BCAST:-}" ]; then
        BCAST="$GB10_WITNESS_BCAST"
    else
        [ -n "${prefix:-}" ] || die "cannot compute the broadcast address; set GB10_WITNESS_BCAST"
        command -v python3 >/dev/null 2>&1 || die "python3 is needed to compute the broadcast; set GB10_WITNESS_BCAST"
        BCAST="$(python3 -c 'import ipaddress,sys
print(ipaddress.ip_interface(f"{sys.argv[1]}/{sys.argv[2]}").network.broadcast_address)' \
            "$IP" "$prefix")"
    fi
    [ -n "${BCAST:-}" ] || die "cannot compute the broadcast address; set GB10_WITNESS_BCAST"
    echo "$MAC" | grep -Eq '^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$' ||
        die "GB10_WITNESS_MAC is not an ethernet address: $MAC"
    PARAM="netconsole=${PORT}@${IP}/${DEV},${PORT}@${BCAST}/${MAC}"
}

show_status() {
    local dev_ip
    dev_ip="$(ip -4 -br addr show 2>/dev/null | awk '$2 == "UP" { print $1 " " $3 }' | tr '\n' ' ' || true)"
    printf 'uplinks          : %s\n' "${dev_ip:-<none>}"
    printf 'netconsole module: %s\n' \
        "$(grep -q '^netconsole ' /proc/modules 2>/dev/null && echo loaded || echo 'NOT loaded')"
    printf 'modprobe config  : %s\n' \
        "$([ -f "$NETCONSOLE_MODPROBE" ] && sed -n 's/^options netconsole //p' "$NETCONSOLE_MODPROBE" || echo '<not installed>')"
    printf 'boot-load config : %s\n' \
        "$([ -f "$NETCONSOLE_MODULES" ] && echo present || echo '<not installed>')"
    printf 'receiver mac     : %s%s\n' "$MAC" \
        "$([ "$MAC" = "ff:ff:ff:ff:ff:ff" ] &&
            echo ' (broadcast -- a switch that suppresses broadcasts drops these)' ||
            echo ' (unicast)')"
    if [ -x "$NVME_TOOL" ]; then
        echo "--- nvme experiments:"
        "$NVME_TOOL" status 2>/dev/null | sed 's/^/    /'
    fi
}

arm_netconsole() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "would write $NETCONSOLE_MODPROBE: options netconsole $PARAM"
        echo "would write $NETCONSOLE_MODULES: netconsole"
        echo "would reload: modprobe netconsole"
        return 0
    fi
    require_root
    install -d -m 0755 "$(dirname "$NETCONSOLE_MODPROBE")" "$(dirname "$NETCONSOLE_MODULES")"
    {
        echo "# Installed by tools/arm-gb10-crash-witness.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)."
        echo "# Kernel console over UDP, so a hard lock's last messages survive the"
        echo "# stalled NVMe.  Receive with: socat -u UDP-RECVFROM:$PORT,fork - >> gb10-console.log"
        echo "options netconsole $PARAM"
    } >"$NETCONSOLE_MODPROBE"
    chmod 0644 "$NETCONSOLE_MODPROBE"
    echo netconsole >"$NETCONSOLE_MODULES"
    chmod 0644 "$NETCONSOLE_MODULES"

    if grep -q '^netconsole ' /proc/modules 2>/dev/null; then
        modprobe -r netconsole 2>/dev/null || true
    fi
    modprobe netconsole || die "modprobe netconsole failed"
    echo "netconsole armed: $PARAM"
}

test_message() {
    local line="netconsole witness armed on $(hostname) -- $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "would write to /dev/kmsg: $line"
        return 0
    fi
    printf '<6>%s\n' "$line" >/dev/kmsg 2>/dev/null || true
    echo "test line written to /dev/kmsg (it should appear on the receiver)"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --status|status) STATUS_ONLY=1 ;;
        --dry-run|-n) DRY_RUN=1 ;;
        --no-apst) DO_APST=0 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
    shift
done

if [ "$STATUS_ONLY" -eq 1 ]; then
    show_status
    exit 0
fi

detect_net
echo "witness: device $DEV  local $IP  destination $BCAST:$PORT  mac $MAC"
arm_netconsole
if [ "$DO_KMSG" = "1" ]; then
    test_message
fi

if [ "$DO_APST" -eq 1 ]; then
    if [ ! -x "$NVME_TOOL" ]; then
        die "$NVME_TOOL not found; run it manually or pass --no-apst"
    fi
    echo
    if [ "$DRY_RUN" -eq 1 ]; then
        "$NVME_TOOL" apply apst --dry-run
    else
        "$NVME_TOOL" apply apst
    fi
fi

echo
echo "next:"
echo "  1. on ANOTHER host on the same LAN (this box does not receive the"
echo "     broadcast frames it sends), record the console:"
echo "       socat -u UDP-RECVFROM:$PORT,fork - >> gb10-console.log"
echo "  2. if the receiver's IP and MAC are known, aim the console at it"
echo "     directly -- switches may drop broadcast frames:"
echo "       GB10_WITNESS_BCAST=<receiver-ip> GB10_WITNESS_MAC=<receiver-mac> \\"
echo "         sudo tools/arm-gb10-crash-witness.sh --no-apst"
echo "  3. reboot when convenient -- APST off takes effect then:"
echo "       sudo reboot"
echo "  4. afterwards: tools/arm-gb10-crash-witness.sh --status"