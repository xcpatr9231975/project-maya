#!/usr/bin/env bash
#
# nvme-stall-mitigations.sh -- reversible storage-stall experiments for the
# GB10 hard locks.
#
# Why: every unexplained hard lock on this box (2026-10-24 .. 2026-10-27) died
# with the engine streaming experts at 94-96% GPU for hours.  The 2026-10-27
# 05:44 post-mortem found the first broken thing was the kernel's write path
# (a report closed at 05:44:25 never reached the NVMe) while journald's fsyncs
# kept landing -- and NVMe SMART is clean (media_errors=0, empty controller
# error log).  That leaves two plausible triggers on a Samsung OEM drive under
# sustained load: APST power-state transitions and NVIDIA's interrupt
# coalescing (0x107) applied at boot.  See
# docs/2026-10-27-hardlock-0544-writeback-stall.md, "Ranked experiments".
#
# Nothing here is a proven fix.  Each change is one variable, reversible, and
# cheap; the point is to attribute the freeze, not to paper over it.
#
# Usage:
#   tools/nvme-stall-mitigations.sh status
#   sudo tools/nvme-stall-mitigations.sh apply apst          # E1, next boot
#   sudo tools/nvme-stall-mitigations.sh apply coalescing    # E2, live
#   sudo tools/nvme-stall-mitigations.sh apply io-timeout    # E3, next boot
#   sudo tools/nvme-stall-mitigations.sh revert apst|coalescing|io-timeout
#
#   add --dry-run to apply/revert to print the files and commands only.
#
# Env:
#   NVME_DEV        block device (default /dev/nvme0)
#   NVME_IO_SEC     io_timeout for E3 (default 10)
#   NVME_GRUB_DROPIN  drop-in path (default /etc/default/grub.d/99-nvme-stall-mitigations.cfg)
#
# Test hook (paths redirected to a scratch dir):
#   NVME_ALLOW_NONROOT=1  skip the uid check; file writes still need perms
#   NVME_SKIP_GRUB=1      do not run update-grub (drop-in logic tests)
#   NVME_APST_PARAM       file holding default_ps_max_latency_us
#                         (default /sys/module/nvme_core/parameters/default_ps_max_latency_us)
#   NVME_CMDLINE          kernel command line (default /proc/cmdline)
#                         status must read these from files, never from the live
#                         host, or the suite fails wherever APST is already off.
#
# Notes:
#   - APST and io_timeout live on the kernel command line, so they take effect
#     on the next boot.  A controller reset cannot arm them live: the root
#     filesystem is on the same device.
#   - Coalescing is a live NVMe feature write and takes effect immediately.
#   - Changing the cmdline re-runs update-grub; the drop-in appends to
#     GRUB_CMDLINE_LINUX_DEFAULT exactly like the existing DGX/NVIDIA drop-ins,
#     so it never rewrites another package's arguments.

set -euo pipefail

NVME_DEV="${NVME_DEV:-/dev/nvme0}"
NVME_IO_SEC="${NVME_IO_SEC:-10}"
GRUB_DROPIN="${NVME_GRUB_DROPIN:-/etc/default/grub.d/99-nvme-stall-mitigations.cfg}"
GRUB_CFG="${NVME_GRUB_CFG:-/boot/grub/grub.cfg}"
COALESCE_SVC="${NVME_COALESCE_SVC:-nvidia-nvme-interrupt-coalescing.service}"
APST_PARAM="${NVME_APST_PARAM:-/sys/module/nvme_core/parameters/default_ps_max_latency_us}"
CMDLINE="${NVME_CMDLINE:-/proc/cmdline}"

TOKEN_APST="nvme_core.default_ps_max_latency_us=0"
COALESCE_FEATURE=0x08
COALESCE_OFF=0
COALESCE_NVIDIA=0x107

DRY_RUN=0
ACTION=""
TARGET=""

usage() {
    sed -n '3,48p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
    echo "error: $*" >&2
    exit 1
}

require_root() {
    if [ "${NVME_ALLOW_NONROOT:-0}" = "1" ]; then
        return 0
    fi
    if [ "$(id -u)" -ne 0 ]; then
        die "must run as root (use sudo)"
    fi
}

io_token() {
    echo "nvme_core.io_timeout=${NVME_IO_SEC}"
}

dropin_tokens() {
    [ -f "$GRUB_DROPIN" ] || return 0
    sed -n 's/^GRUB_CMDLINE_LINUX_DEFAULT="\$GRUB_CMDLINE_LINUX_DEFAULT *\(.*\)"$/\1/p' \
        "$GRUB_DROPIN" | tr ' ' '\n' | sed '/^$/d'
}

write_dropin_tokens() {
    local src="$1" toks
    toks="$(tr '\n' ' ' <"$src" | sed 's/ *$//')"
    if [ -z "$toks" ]; then
        rm -f "$GRUB_DROPIN"
        return 0
    fi
    install -d -m 0755 "$(dirname "$GRUB_DROPIN")"
    {
        echo "# Installed by tools/nvme-stall-mitigations.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)."
        echo "# Storage-stall experiments for the GB10 hard locks; see"
        echo "# docs/2026-10-27-hardlock-0544-writeback-stall.md.  Remove with:"
        echo "#   sudo tools/nvme-stall-mitigations.sh revert <apst|io-timeout>"
        echo "GRUB_CMDLINE_LINUX_DEFAULT=\"\$GRUB_CMDLINE_LINUX_DEFAULT ${toks}\""
    } >"$GRUB_DROPIN"
    chmod 0644 "$GRUB_DROPIN"
}

add_token() {
    local tok="$1" tmp
    dropin_tokens | grep -qxF "$tok" && return 0
    tmp="$(mktemp)"
    { dropin_tokens; echo "$tok"; } | sort -u >"$tmp"
    write_dropin_tokens "$tmp"
    rm -f "$tmp"
}

remove_token() {
    local tok="$1" tmp
    [ -f "$GRUB_DROPIN" ] || return 0
    tmp="$(mktemp)"
    dropin_tokens | grep -vxF "$tok" >"$tmp" || true
    write_dropin_tokens "$tmp"
    rm -f "$tmp"
}

update_grub() {
    if [ "${NVME_SKIP_GRUB:-0}" = "1" ]; then
        echo "skipping update-grub (NVME_SKIP_GRUB=1)" >&2
        return 0
    fi
    if command -v update-grub >/dev/null 2>&1; then
        update-grub
    else
        grub-mkconfig -o "$GRUB_CFG"
    fi
}

verify_grub_tokens() {
    local tok rc=0
    while IFS= read -r tok; do
        [ -n "$tok" ] || continue
        if grep -qF -- "$tok" "$GRUB_CFG" 2>/dev/null; then
            echo "  ok: $tok present in $GRUB_CFG"
        else
            echo "  MISSING: $tok not in $GRUB_CFG" >&2
            rc=1
        fi
    done < <(dropin_tokens)
    return "$rc"
}

coalescing_value() {
    command -v nvme >/dev/null 2>&1 || { echo "n/a (nvme-cli missing)"; return 0; }
    local out
    if ! out="$(nvme get-feature "$NVME_DEV" -f "$COALESCE_FEATURE" 2>&1)"; then
        echo "n/a (needs root)"
        return 0
    fi
    printf '%s\n' "$out" | sed -n 's/.*value: *\([^,]*\).*/\1/p' | head -1
}

show_status() {
    local cur params val svc_enabled svc_active dropin_state pending
    cur="$(grep -o 'nvme_core\.[^ ]*' "$CMDLINE" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//' || true)"
    params="$(cat "$APST_PARAM" 2>/dev/null || echo n/a)"
    val="$(coalescing_value)"
    svc_enabled="$(systemctl is-enabled "$COALESCE_SVC" 2>/dev/null || echo n/a)"
    svc_active="$(systemctl is-active "$COALESCE_SVC" 2>/dev/null || echo n/a)"

    printf 'device          : %s\n' "$NVME_DEV"
    printf 'cmdline nvme    : %s\n' "${cur:-<none>}"
    printf 'APST latency cap: %s us (%s)\n' "$params" \
        "$([ "$params" = "0" ] && echo 'APST disabled' || echo 'APST enabled')"
    printf 'coalescing 0x08 : %s\n' "$val"
    printf 'coalesce service: %s / %s\n' "$svc_enabled" "$svc_active"

    if [ -f "$GRUB_DROPIN" ]; then
        printf 'grub drop-in    : %s\n' "$GRUB_DROPIN"
        dropin_tokens | sed 's/^/                  /'
        pending=""
        while IFS= read -r t; do
            [ -n "$t" ] || continue
            grep -qF -- "$t" "$CMDLINE" 2>/dev/null || pending="$pending $t"
        done < <(dropin_tokens)
        if [ -n "$pending" ]; then
            printf 'pending reboot  :%s\n' "$pending"
        else
            printf 'pending reboot  : none\n'
        fi
    else
        printf 'grub drop-in    : <not installed>\n'
    fi
}

dry_run_token_change() {
    local what="$1" tok="$2"
    echo "would add '$tok' to $GRUB_DROPIN"
    echo "would run: update-grub"
    echo "would verify: '$tok' present in $GRUB_CFG"
    echo "($what takes effect on the next boot)"
}

apply_apst() {
    if [ "$DRY_RUN" -eq 1 ]; then
        dry_run_token_change "APST disable" "$TOKEN_APST"
        return 0
    fi
    require_root
    add_token "$TOKEN_APST"
    update_grub >/dev/null
    echo "APST disable installed (default_ps_max_latency_us=0)"
    verify_grub_tokens
}

apply_io_timeout() {
    local tok
    tok="$(io_token)"
    if [ "$DRY_RUN" -eq 1 ]; then
        dry_run_token_change "io_timeout=${NVME_IO_SEC}s" "$tok"
        return 0
    fi
    require_root
    add_token "$tok"
    update_grub >/dev/null
    echo "io_timeout=${NVME_IO_SEC}s installed"
    verify_grub_tokens
}

apply_coalescing() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "would run: nvme set-feature $NVME_DEV -f $COALESCE_FEATURE -v $COALESCE_OFF"
        echo "would run: systemctl mask --now $COALESCE_SVC"
        echo "(coalescing off takes effect immediately; a reboot keeps it off)"
        return 0
    fi
    require_root
    command -v nvme >/dev/null 2>&1 || die "nvme-cli is missing"
    nvme set-feature "$NVME_DEV" -f "$COALESCE_FEATURE" -v "$COALESCE_OFF" >/dev/null
    systemctl mask --now "$COALESCE_SVC" >/dev/null 2>&1 || true
    echo "coalescing disabled on $NVME_DEV (value $(coalescing_value))"
    echo "service masked: $(systemctl is-enabled "$COALESCE_SVC" 2>/dev/null || echo n/a)"
}

revert_apst() {
    local tok="$TOKEN_APST"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "would remove '$tok' from $GRUB_DROPIN"
        echo "would run: update-grub"
        return 0
    fi
    require_root
    [ -f "$GRUB_DROPIN" ] || { echo "nothing installed"; return 0; }
    remove_token "$tok"
    update_grub >/dev/null
    echo "APST disable removed (APST re-enables on the next boot)"
}

revert_io_timeout() {
    local tok
    tok="$(io_token)"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "would remove '$tok' from $GRUB_DROPIN"
        echo "would run: update-grub"
        return 0
    fi
    require_root
    [ -f "$GRUB_DROPIN" ] || { echo "nothing installed"; return 0; }
    remove_token "$tok"
    update_grub >/dev/null
    echo "$tok removed (default 30s io_timeout returns after the next boot)"
}

revert_coalescing() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "would run: systemctl unmask $COALESCE_SVC && systemctl enable $COALESCE_SVC"
        echo "would run: nvme set-feature $NVME_DEV -f $COALESCE_FEATURE -v $COALESCE_NVIDIA"
        return 0
    fi
    require_root
    systemctl unmask "$COALESCE_SVC" 2>/dev/null || true
    systemctl enable "$COALESCE_SVC" >/dev/null 2>&1 || true
    if command -v nvme >/dev/null 2>&1; then
        nvme set-feature "$NVME_DEV" -f "$COALESCE_FEATURE" -v "$COALESCE_NVIDIA" >/dev/null
    fi
    echo "coalescing restored ($(coalescing_value)); service: $(systemctl is-enabled "$COALESCE_SVC" 2>/dev/null || echo n/a)"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        status|show|verify) ACTION=status ;;
        apply) ACTION=apply ;;
        revert|disable) ACTION=revert ;;
        apst|coalescing|io-timeout|all) TARGET="$1" ;;
        --dry-run|-n) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
    shift
done

[ -n "$ACTION" ] || { usage; exit 2; }

case "$ACTION" in
    status) show_status ;;
    apply)
        case "$TARGET" in
            apst) apply_apst ;;
            coalescing) apply_coalescing ;;
            io-timeout) apply_io_timeout ;;
            all)
                apply_apst
                apply_coalescing
                apply_io_timeout
                ;;
            *) die "apply needs one of: apst | coalescing | io-timeout | all" ;;
        esac
        ;;
    revert)
        case "$TARGET" in
            apst) revert_apst ;;
            coalescing) revert_coalescing ;;
            io-timeout) revert_io_timeout ;;
            all)
                revert_apst
                revert_coalescing
                revert_io_timeout
                ;;
            *) die "revert needs one of: apst|coalescing|io-timeout|all" ;;
        esac
        ;;
esac
