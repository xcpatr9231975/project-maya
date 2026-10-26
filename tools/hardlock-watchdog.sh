#!/usr/bin/env bash
#
# hardlock-watchdog.sh -- make a GB10 hard lock reboot itself.
#
# Why: the box has hard-locked four times (2026-10-24 19:40, 2026-10-24
# 21:08, 2026-10-25 17:11, 2026-10-25 17:49) with device-free healthy, no
# NV_ERR_NO_MEMORY, no Xid and no shutdown sequence.  The kernel's own
# hard-lock detector is unavailable here ("watchdog: NMI not fully
# supported" / "Hard watchdog permanently disabled"), so the kernel never
# panics and never reboots: a human power-cycles the box.
#
# The board still has a second, independent timer: /dev/watchdog0 is the
# SBSA Generic Watchdog (sbsa_gwdt, arm,sbsa-gwdt).  It is loaded in
# two-stage mode (module parameter action=1):
#
#   |--------WOR-------WS0--------WOR-------WS1
#   |----timeout-----(panic)----timeout-----reset
#
# WS0 raises an interrupt and panics; if the panic cannot run (the usual
# hard-lock case) WS1 asserts a real hardware reset one more timeout later.
# The reset line is driven by the SoC, not by Linux, so it fires even when
# userspace and the driver are wedged.
#
# systemd 255 can arm that timer: with RuntimeWatchdogSec set, PID 1 pings
# /dev/watchdog0 and the hardware reboots when the pings stop.  kernel.panic
# makes a panic (including the WS0 panic) reboot after a short delay instead
# of hanging at panic=0.
#
# This is recovery, not prevention: no userspace watchdog can stop an
# instantaneous whole-box lock.  It turns a manual power-cycle into an
# automatic reboot (worst case ~2 x the timeout in two-stage mode).
#
# Usage:
#   tools/hardlock-watchdog.sh status          # read-only, no root needed
#   sudo tools/hardlock-watchdog.sh apply      # arm it (takes effect now)
#   sudo tools/hardlock-watchdog.sh revert     # disarm and restore panic=0
#
#   add --dry-run to apply/revert to print the files and commands only.
#
# Env:
#   HARDLOCK_WATCHDOG_SEC   runtime + reboot watchdog timeout (default 30)
#   HARDLOCK_PANIC_SEC      seconds after a panic before reboot (default 15)
#
# Test hook (paths must be redirected to a scratch dir):
#   HARDLOCK_ALLOW_NONROOT=1  skip the uid check; file writes still need perms
#
# Notes:
#   - apply runs `systemctl daemon-reexec` so system.conf is re-read and the
#     watchdog is armed without restarting any service; a reboot does the
#     same on its own.
#   - A false arm costs a reboot.  If the box ever reboots under load with no
#     hard lock, raise HARDLOCK_WATCHDOG_SEC (PID 1 was starved longer than
#     the timeout).
#   - The engine and the memory guard are independent of this; nothing here
#     restarts or signals them.

set -euo pipefail

DEFAULT_WATCHDOG_SEC=30
DEFAULT_PANIC_SEC=15

WATCHDOG_SEC="${HARDLOCK_WATCHDOG_SEC:-$DEFAULT_WATCHDOG_SEC}"
PANIC_SEC="${HARDLOCK_PANIC_SEC:-$DEFAULT_PANIC_SEC}"

SYSTEMD_DROPIN="${HARDLOCK_SYSTEMD_DROPIN:-/etc/systemd/system.conf.d/50-hardlock-watchdog.conf}"
SYSCTL_DROPIN="${HARDLOCK_SYSCTL_DROPIN:-/etc/sysctl.d/60-hardlock-panic.conf}"

WATCHDOG_DEV="${HARDLOCK_WATCHDOG_DEV:-/dev/watchdog0}"
WATCHDOG_SYSFS="${HARDLOCK_WATCHDOG_SYSFS:-/sys/class/watchdog/watchdog0}"
PROC_PANIC="${HARDLOCK_PROC_PANIC:-/proc/sys/kernel/panic}"

DRY_RUN=0
ACTION=""

usage() {
    sed -n '3,60p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
    echo "error: $*" >&2
    exit 1
}

require_root() {
    if [ "${HARDLOCK_ALLOW_NONROOT:-0}" = "1" ]; then
        return 0
    fi
    if [ "$(id -u)" -ne 0 ]; then
        die "must run as root (use sudo)"
    fi
}

validate_int() {
    local name="$1" value="$2"
    case "$value" in
        ''|*[!0-9]*) die "$name must be a positive integer, got '$value'" ;;
    esac
    [ "$value" -gt 0 ] || die "$name must be greater than zero, got '$value'"
}

check_device() {
    [ -e "$WATCHDOG_DEV" ] || die "$WATCHDOG_DEV is missing (is sbsa_gwdt loaded?)"
    [ -d "$WATCHDOG_SYSFS" ] || die "$WATCHDOG_SYSFS is missing"

    local identity
    identity="$(cat "$WATCHDOG_SYSFS/identity" 2>/dev/null || true)"
    case "$identity" in
        *"SBSA"*) ;;
        *) die "$WATCHDOG_DEV is '$identity', not the SBSA Generic Watchdog" ;;
    esac

    local options
    options="$(cat "$WATCHDOG_SYSFS/options" 2>/dev/null || echo 0)"
    # WDIOF_SETTIMEOUT is 0x0080.  Without it the timeout cannot be changed.
    if [ $(( options & 0x0080 )) -eq 0 ]; then
        die "$WATCHDOG_DEV does not advertise WDIOF_SETTIMEOUT"
    fi
}

show_state() {
    local identity state timeout options nowayout panic runtime reboot
    identity="$(cat "$WATCHDOG_SYSFS/identity" 2>/dev/null || echo 'n/a')"
    state="$(cat "$WATCHDOG_SYSFS/state" 2>/dev/null || echo 'n/a')"
    timeout="$(cat "$WATCHDOG_SYSFS/timeout" 2>/dev/null || echo 'n/a')"
    options="$(cat "$WATCHDOG_SYSFS/options" 2>/dev/null || echo 'n/a')"
    nowayout="$(cat "$WATCHDOG_SYSFS/nowayout" 2>/dev/null || echo 'n/a')"
    panic="$(cat "$PROC_PANIC" 2>/dev/null || echo 'n/a')"
    runtime="$(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null || echo 'n/a')"
    reboot="$(systemctl show -p RebootWatchdogUSec --value 2>/dev/null || echo 'n/a')"

    printf 'device    : %s\n' "$WATCHDOG_DEV"
    printf 'identity  : %s\n' "$identity"
    printf 'driver    : %s\n' "$(readlink -f "$WATCHDOG_SYSFS/device/driver" 2>/dev/null || echo 'n/a')"
    printf 'state     : %s\n' "$state"
    printf 'timeout   : %s s\n' "$timeout"
    printf 'nowayout  : %s\n' "$nowayout"
    printf 'options   : %s\n' "$options"
    printf 'panic     : %s s\n' "$panic"
    printf 'systemd   : RuntimeWatchdogUSec=%s RebootWatchdogUSec=%s\n' "$runtime" "$reboot"

    if [ "$state" = "active" ] && [ "$runtime" != "0" ] && [ "$runtime" != "n/a" ]; then
        printf 'verdict   : ARMED -- a hard lock reboots automatically\n'
        return 0
    fi
    printf 'verdict   : NOT ARMED -- a hard lock needs a power-cycle\n'
    return 1
}

write_dropins() {
    install -d -m 0755 "$(dirname "$SYSTEMD_DROPIN")"
    install -d -m 0755 "$(dirname "$SYSCTL_DROPIN")"

    cat >"$SYSTEMD_DROPIN" <<EOF
# Installed by tools/hardlock-watchdog.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
# The SBSA Generic Watchdog is two-stage (action=1): WS0 panics, WS1 resets.
# PID 1 pings /dev/watchdog0; when the pings stop the hardware reboots.
[Manager]
RuntimeWatchdogSec=${WATCHDOG_SEC}s
RebootWatchdogSec=${WATCHDOG_SEC}s
EOF

    cat >"$SYSCTL_DROPIN" <<EOF
# Installed by tools/hardlock-watchdog.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
# Reboot instead of hanging after a panic/oops (the WS0 stage of the SBSA
# watchdog panics; this makes that path recover too).
kernel.panic = ${PANIC_SEC}
EOF

    chmod 0644 "$SYSTEMD_DROPIN" "$SYSCTL_DROPIN"
}

dry_run_apply() {
    echo "would write $SYSTEMD_DROPIN:"
    echo "  [Manager]"
    echo "  RuntimeWatchdogSec=${WATCHDOG_SEC}s"
    echo "  RebootWatchdogSec=${WATCHDOG_SEC}s"
    echo "would write $SYSCTL_DROPIN:"
    echo "  kernel.panic = ${PANIC_SEC}"
    echo "would run: sysctl -q -p $SYSCTL_DROPIN"
    echo "would run: systemctl daemon-reexec"
    echo "would verify: $WATCHDOG_SYSFS/state == active"
}

apply() {
    validate_int HARDLOCK_WATCHDOG_SEC "$WATCHDOG_SEC"
    validate_int HARDLOCK_PANIC_SEC "$PANIC_SEC"
    check_device

    if [ "$DRY_RUN" -eq 1 ]; then
        dry_run_apply
        return 0
    fi

    require_root
    write_dropins

    echo "applying kernel.panic=${PANIC_SEC}"
    sysctl -q -p "$SYSCTL_DROPIN"

    echo "re-executing systemd to arm ${WATCHDOG_DEV} (${WATCHDOG_SEC}s)"
    systemctl daemon-reexec

    local i
    for i in $(seq 1 20); do
        [ "$(cat "$WATCHDOG_SYSFS/state" 2>/dev/null || true)" = "active" ] && break
        sleep 0.5
    done

    echo
    if show_state; then
        echo
        echo "worst-case reset in two-stage mode: $(( WATCHDOG_SEC * 2 ))s"
    else
        echo
        die "watchdog did not arm; a reboot applies the drop-ins"
    fi
}

revert() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "would remove $SYSTEMD_DROPIN"
        echo "would remove $SYSCTL_DROPIN"
        echo "would run: sysctl -q -w kernel.panic=0"
        echo "would run: systemctl daemon-reexec"
        return 0
    fi

    require_root
    rm -f "$SYSTEMD_DROPIN" "$SYSCTL_DROPIN"
    sysctl -q -w kernel.panic=0 || true
    systemctl daemon-reexec

    echo
    show_state || true
    echo
    echo "if state is still active, a reboot clears it"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        status|show|verify) ACTION=status ;;
        apply) ACTION=apply ;;
        revert|disable) ACTION=revert ;;
        --dry-run|-n) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
    shift
done

[ -n "$ACTION" ] || { usage; exit 2; }

case "$ACTION" in
    status) show_state ;;
    apply) apply ;;
    revert) revert ;;
esac
