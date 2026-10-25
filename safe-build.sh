#!/usr/bin/env bash
#
# safe-build.sh -- run a build (or any command) under a memory watchdog.
#
# Why: this box hard-locks when unified memory runs out (see the guard block
# in ~/gpu_monitor.py), and a full -j20 C++ build against the live maya
# server (~22 GiB free of 119 GiB at a good moment) can push it there.  The
# gpu_monitor guard's last resort is killing the top GPU consumer -- the
# engine -- which a build should never be able to cause.  So the build gets:
#
#   0. pre-flight    -- refuse to start when system memory is already low
#   1. isolation     -- its own session/process group, niced for CPU + I/O
#   2. watchdog      -- every 5 s: MemAvailable, swap growth, PSI full,
#                       device-free (every DEVICE_PROBE_EVERY_S sample, the
#                       pool the driver allocates from -- the number
#                       MemAvailable gets wrong on GB10, cf. gpu_monitor.py)
#                       -> breach: SIGSTOP the whole process group (freeze)
#                       -> recovered for 2 checks: SIGCONT (resume)
#   3. escalation    -- frozen too long, or available memory deeper than the
#                       abort floor: SIGTERM the group, then SIGKILL
#
# Usage:
#   ./safe-build.sh                      # ninja -C build -j4
#   ./safe-build.sh -C build-tests       # ninja -C build-tests -j4
#   ./safe-build.sh -j2 -C build         # ninja -C build -j2
#   ./safe-build.sh -- cmake --build ... # any command under the watchdog
#
# Everything is env-overridable (SAFE_* vars), thresholds in bytes/GiB:
#   SAFE_J                    ninja parallelism (default 4)
#   SAFE_DIR                  build dir (default: repo root / ./build)
#   SAFE_POLL_S               watchdog interval (default 5)
#   SAFE_FLOOR_GIB            freeze when MemAvailable < this (default 8)
#   SAFE_ABORT_GIB            abort when MemAvailable < this (default 4)
#   SAFE_SWAP_FLOOR_GIB       freeze when swap used > this (default 4)
#   SAFE_PSI_FULL             freeze when PSI memory full avg10 > this,
#                             two consecutive samples (default 5.0; the
#                             guard in ~/gpu_monitor.py kills at 5.0 too,
#                             but it samples ~10 s apart while this polls at
#                             SAFE_POLL_S, so the build freezes first and the
#                             guard never has to pick a victim for a build)
#   SAFE_DEVICE_FLOOR_MIB     freeze when device-free < this (default 4096;
#                             sits above gpu_monitor's kill floor of 3072 so
#                             the build freezes before the guard ever kills)
#   SAFE_DEVICE_PROBE_EVERY_S device-free probe interval (default 60)
#   SAFE_FROZEN_MAX_S         max time frozen before SIGTERM (default 300)
#   SAFE_NO_PREFLIGHT         set to 1 to skip the pre-flight check
#
# Exit codes: 0 ok, 124 build frozen-aborted (watchdog), 125 pre-flight
# refused, 126 command not executable, 127 not found; else the command's.
set -u

SAFE_J="${SAFE_J:-4}"
SAFE_DIR="${SAFE_DIR:-}"
SAFE_POLL_S="${SAFE_POLL_S:-5}"
SAFE_FLOOR_GIB="${SAFE_FLOOR_GIB:-8}"
SAFE_ABORT_GIB="${SAFE_ABORT_GIB:-4}"
SAFE_SWAP_FLOOR_GIB="${SAFE_SWAP_FLOOR_GIB:-4}"
SAFE_PSI_FULL="${SAFE_PSI_FULL:-5.0}"
SAFE_DEVICE_FLOOR_MIB="${SAFE_DEVICE_FLOOR_MIB:-4096}"
SAFE_DEVICE_PROBE_EVERY_S="${SAFE_DEVICE_PROBE_EVERY_S:-60}"
SAFE_FROZEN_MAX_S="${SAFE_FROZEN_MAX_S:-300}"

# --- parse args ---------------------------------------------------------------
CMD=()
while [ $# -gt 0 ]; do
    case "$1" in
        -C) SAFE_DIR="$2"; shift 2 ;;
        -j) SAFE_J="$2"; shift 2 ;;
        --) shift; CMD=("$@"); break ;;
        *)  CMD+=("$1"); shift ;;
    esac
done

if [ -z "$SAFE_DIR" ]; then
    # default to the repo root's build/ (this script lives at the repo root)
    SAFE_DIR="$(dirname "$(readlink -f "$0")")/build"
fi

if [ ${#CMD[@]} -eq 0 ]; then
    CMD=(ninja -C "$SAFE_DIR" -j "$SAFE_J")
fi

# --- helpers ------------------------------------------------------------------
avail_gib() {
    awk '/MemAvailable/ {printf "%.1f", $2 / 1048576}' /proc/meminfo
}

swap_gib() {
    awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {printf "%.1f", (t - f) / 1048576}' /proc/meminfo
}

psi_full() {
    # PSI memory "full" avg10 in %, or empty when unavailable
    awk '/^full/ {for (i = 1; i <= NF; i++) if ($i ~ /^avg10=/) {sub(/^avg10=/, "", $i); print $i}}' /proc/pressure/memory 2>/dev/null
}

device_free_mib() {
    # (free, total) MiB via cudaMemGetInfo -- the pool the driver allocates
    # from on GB10 (borrowed from ~/gpu_monitor.py's device_free()).
    python3 - <<'EOF' 2>/dev/null || echo none
import ctypes, glob
lib = None
for c in sorted(glob.glob("/usr/local/cuda*/lib64/libcudart.so*"), reverse=True) + ["libcudart.so"]:
    try:
        lib = ctypes.CDLL(c)
        lib.cudaMemGetInfo.restype = ctypes.c_int
        lib.cudaMemGetInfo.argtypes = [ctypes.POINTER(ctypes.c_size_t)] * 2
        break
    except OSError:
        continue
if lib is None:
    print("none")
    raise SystemExit
free = ctypes.c_size_t(0)
total = ctypes.c_size_t(0)
if lib.cudaMemGetInfo(ctypes.byref(free), ctypes.byref(total)) != 0:
    print("none")
else:
    print(f"{free.value >> 20} {total.value >> 20}")
EOF
}

log() {
    printf '[safe-build %s] %s\n' "$(date +%H:%M:%S)" "$*"
}

# --- 0. pre-flight ------------------------------------------------------------
if [ "${SAFE_NO_PREFLIGHT:-0}" != "1" ]; then
    a="$(avail_gib)"
    s="$(swap_gib)"
    d="$(device_free_mib)"
    dfree=""
    case "$d" in
        none) d="none" ;;
        *) dfree="$(awk '{printf "%.1f", $1 / 1024}' <<<"$d")" ;;
    esac
    log "pre-flight: mem_available=${a}GiB swap=${s}GiB device_free=${dfree}GiB"
    ok=1
    case "$d" in
        none) ;;
        *) awk "BEGIN {exit !($dfree < $SAFE_DEVICE_FLOOR_MIB / 1024)}" && ok=0 ;;
    esac
    [ "$(awk "BEGIN {exit !($a < $SAFE_FLOOR_GIB)}" && echo x)" ] && ok=0
    if [ "$ok" = 0 ]; then
        log "REFUSED: memory below the freeze floor before starting; do not build now"
        exit 125
    fi
fi

# --- 1. start the build in its own session ------------------------------------
log "start: ${CMD[*]} (watchdog poll ${SAFE_POLL_S}s)"
setsid nice -n 10 ionice -c2 -n7 "${CMD[@]}" &
BUILD_PID=$!
# setsid makes the child its own session leader, so its pgid == its pid and
# every compiler process it spawns joins that group -> one signal stops all.
sleep 0.3
PGID="$BUILD_PID"
if ! kill -0 "$BUILD_PID" 2>/dev/null; then
    log "command exited immediately"
    wait "$BUILD_PID" 2>/dev/null
    exit $?
fi
log "watching pid $BUILD_PID (pgid $PGID)"

# --- 2. watchdog loop ----------------------------------------------------------
FROZEN=0            # seconds the group has been frozen
FROZED_PID=0        # 0 = running, >0 = we have SIGSTOPped the group
PSI_STRIKES=0       # consecutive PSI-full samples
LAST_PROBE=0        # device-free probe bookkeeping (monotonic-ish)
STOPPED=0

stop_group()  { kill -STOP  "-$PGID" 2>/dev/null;  STOPPED=1; }
cont_group()  { kill -CONT  "-$PGID" 2>/dev/null;  STOPPED=0; }
term_group()  { kill -TERM  "-$PGID" 2>/dev/null;  }
kill_group()  { kill -KILL  "-$PGID" 2>/dev/null;  }

cleanup() {
    if [ "$STOPPED" = 1 ]; then
        cont_group
    fi
    kill -CONT "-$PGID" 2>/dev/null
}
# INT must exit: without the exit the script kept looping and the build kept
# running after a Ctrl-C.  TERM is not trapped at all by default, and an
# untrapped TERM used to leave the build running unwatched.
on_int()  { exit 130; }
on_term() {
    log "TERM: stopping the build's process group"
    term_group
    sleep 2
    kill -CONT "-$PGID" 2>/dev/null
    kill_group
    exit 124
}
trap cleanup EXIT
trap on_int INT
trap on_term TERM

STATUS=0
while kill -0 "$BUILD_PID" 2>/dev/null; do
    a="$(avail_gib)"
    s="$(swap_gib)"
    pf="$(psi_full)"
    freeze_reason=""

    now="$(date +%s)"
    if [ $(( now - LAST_PROBE )) -ge "$SAFE_DEVICE_PROBE_EVERY_S" ]; then
        LAST_PROBE="$now"
        d="$(device_free_mib)"
        case "$d" in
            none) log "telemetry: mem_available=${a}GiB swap=${s}GiB psi_full=${pf:-0}% device_free=none" ;;
            *)  dfree="$(awk '{print $1}' <<<"$d")"
                dfree_g="$(awk '{printf "%.1f", $1 / 1024}' <<<"$d")"
                log "telemetry: mem_available=${a}GiB swap=${s}GiB psi_full=${pf:-0}% device_free=${dfree_g}GiB"
                if [ "$dfree" -lt "$SAFE_DEVICE_FLOOR_MIB" ]; then
                    freeze_reason="device_free=${dfree}MiB < ${SAFE_DEVICE_FLOOR_MIB}MiB"
                fi
                ;;
        esac
    fi

    [ "$(awk "BEGIN {exit !($a < $SAFE_FLOOR_GIB)}" && echo x)" ] && \
        freeze_reason="${freeze_reason:+$freeze_reason; }mem_available=${a}GiB < ${SAFE_FLOOR_GIB}GiB"
    [ "$(awk "BEGIN {exit !($s >= $SAFE_SWAP_FLOOR_GIB)}" && echo x)" ] && \
        freeze_reason="${freeze_reason:+$freeze_reason; }swap=${s}GiB >= ${SAFE_SWAP_FLOOR_GIB}GiB"
    if [ -n "$pf" ]; then
        if [ "$(awk "BEGIN {exit !($pf >= $SAFE_PSI_FULL)}" && echo x)" ]; then
            PSI_STRIKES=$(( PSI_STRIKES + 1 ))
            if [ "$PSI_STRIKES" -ge 2 ]; then
                freeze_reason="${freeze_reason:+$freeze_reason; }psi_full=${pf}% x$PSI_STRIKES"
            fi
        else
            PSI_STRIKES=0
        fi
    fi

    if [ "$STOPPED" = 1 ]; then
        FROZEN=$(( FROZEN + SAFE_POLL_S ))
        if [ -n "$freeze_reason" ] && \
           [ "$(awk "BEGIN {exit !($a < $SAFE_ABORT_GIB)}" && echo x)" ]; then
            log "ABORT: while frozen, ${freeze_reason} (abort floor ${SAFE_ABORT_GIB}GiB)"
            term_group
            sleep 2
            kill -CONT "-$PGID" 2>/dev/null
            kill_group
            STATUS=124
            break
        fi
        if [ -z "$freeze_reason" ]; then
            log "RESUME: memory recovered (avail=${a}GiB swap=${s}GiB)"
            cont_group
            FROZEN=0
        elif [ "$FROZEN" -ge "$SAFE_FROZEN_MAX_S" ]; then
            log "ABORT: frozen ${FROZEN}s with ${freeze_reason}; terminating the build"
            term_group
            sleep 2
            kill -CONT "-$PGID" 2>/dev/null
            kill_group
            STATUS=124
            break
        fi
    elif [ -n "$freeze_reason" ]; then
        log "FREEZE: ${freeze_reason} -> SIGSTOP the build's process group"
        stop_group
        FROZEN=0
    fi

    sleep "$SAFE_POLL_S"
done

wait "$BUILD_PID" 2>/dev/null
RC=$?
if [ "$STATUS" = 124 ]; then
    log "exit: watchdog aborted the build (rc 124)"
    exit 124
fi
if [ "$RC" -eq 139 ]; then
    log "exit: command died (SIGSEGV or watchdog KILL)"
fi
log "exit: rc=$RC"
exit "$RC"
