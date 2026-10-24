#!/bin/sh
# serve-maya.sh - the local entrypoint for the Maya-S-v2 API (the GB10 build in build/maya).
#
#   ./serve-maya.sh            start (default) and wait until the API answers
#   ./serve-maya.sh start      the same; extra args go to maya.py
#   ./serve-maya.sh stop       stop the server started here
#   ./serve-maya.sh restart    stop, then start
#   ./serve-maya.sh status     is it up, and where
#   ./serve-maya.sh logs       follow the server and engine logs
#
# Environment: MAYA_CONFIG, MAYA_PORT (8080), MAYA_START_TIMEOUT (600 s), MAYA_ENGINE_LOG.
# One model start loads ~84 GB of experts: the first answers wait for it, not for this script.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG=${MAYA_CONFIG:-"$ROOT/maya-maya-s-v2-iq2_xxs.json"}
PIDFILE="$ROOT/.serve-maya.pid"
OUTLOG="$ROOT/serve-maya.out.log"
ENGLOG=${MAYA_ENGINE_LOG:-"$ROOT/maya-maya-s-v2-iq2_xxs.log"}
HOST=127.0.0.1
PORT=${MAYA_PORT:-8080}
TIMEOUT=${MAYA_START_TIMEOUT:-600}

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

pid_of() {
    [ -f "$PIDFILE" ] && cat "$PIDFILE" 2>/dev/null || true
}

# the server started outside this script (a restart by hand, a supervisor): the same config's maya.py
find_pid() {
    ps -eo pid=,args= 2>/dev/null | awk -v cfg="$CONFIG" \
        'index($0, "maya.py") && index($0, "--config " cfg) { print $1; exit }'
}

# the pid of the running server: the pid file's, or the one this script did not start
running_pid() {
    p=$(pid_of)
    if [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null; then
        printf '%s\n' "$p"
        return 0
    fi
    p=$(find_pid)
    [ -n "${p:-}" ] || return 1
    printf '%s\n' "$p"
}

is_running() {
    running_pid >/dev/null
}

port_open() {
    command -v curl >/dev/null 2>&1 || return 1
    curl -fsS --max-time 2 "http://$HOST:$PORT/v1/models" >/dev/null 2>&1 ||
        curl -fsS --max-time 2 "http://$HOST:$PORT/" >/dev/null 2>&1
}

start() {
    [ -f "$CONFIG" ] || die "missing config: $CONFIG"
    [ -x "$ROOT/build/maya" ] || die "engine not built: $ROOT/build/maya (run ./maya.sh --setup, or build with maya.py)"
    [ -f "$ROOT/maya.py" ] || die "missing $ROOT/maya.py"
    if [ -x "$ROOT/.venv/bin/python" ]; then
        PY="$ROOT/.venv/bin/python"
    else
        PY=$(command -v python3 || command -v python || true)
    fi
    [ -n "${PY:-}" ] || die "no python: create .venv or install python3"

    if is_running; then
        p=$(running_pid)
        printf 'already running (pid %s)\nendpoint: http://%s:%s/v1\n' "$p" "$HOST" "$PORT"
        return 0
    fi
    rm -f "$PIDFILE"

    before=0
    [ -f "$ENGLOG" ] && before=$(wc -l <"$ENGLOG" | tr -d ' ')
    printf 'starting Maya-S-v2 (config %s) ...\n' "$CONFIG"
    nohup "$PY" "$ROOT/maya.py" --config "$CONFIG" --port "$PORT" "$@" >>"$OUTLOG" 2>&1 &
    p=$!
    echo "$p" >"$PIDFILE"

    waited=0
    while kill -0 "$p" 2>/dev/null; do
        if [ -f "$ENGLOG" ] && tail -n "+$((before + 1))" "$ENGLOG" 2>/dev/null | grep -q 'ready: http'; then
            break
        fi
        port_open && break
        [ "$waited" -ge "$TIMEOUT" ] && break
        sleep 2
        waited=$((waited + 2))
        if [ $((waited % 20)) -eq 0 ]; then
            printf '  still starting (%ss) ...\n' "$waited"
        fi
    done

    if ! kill -0 "$p" 2>/dev/null; then
        rm -f "$PIDFILE"
        printf 'engine exited during start; last lines:\n' >&2
        tail -n 30 "$OUTLOG" 2>/dev/null >&2 || true
        tail -n 30 "$ENGLOG" 2>/dev/null >&2 || true
        return 1
    fi
    if port_open || { [ -f "$ENGLOG" ] && tail -n "+$((before + 1))" "$ENGLOG" 2>/dev/null | grep -q 'ready: http'; }; then
        printf 'ready (pid %s)\n  API:       http://%s:%s/v1\n  dashboard: http://%s:%s/\n' \
            "$p" "$HOST" "$PORT" "$HOST" "$PORT"
    else
        printf 'still loading (pid %s, %ss); follow it with: %s logs\n' "$p" "$waited" "$0"
    fi
    return 0
}

stop() {
    p=$(running_pid || true)
    if [ -z "${p:-}" ] || ! kill -0 "$p" 2>/dev/null; then
        rm -f "$PIDFILE"
        printf 'not running\n'
        return 0
    fi
    printf 'stopping (pid %s) ...\n' "$p"
    kill -TERM "$p" 2>/dev/null || true
    waited=0
    while kill -0 "$p" 2>/dev/null && [ "$waited" -lt 60 ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$p" 2>/dev/null; then
        kill -KILL "$p" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
    printf 'stopped\n'
}

status() {
    if is_running; then
        p=$(running_pid)
        printf 'running (pid %s)\n' "$p"
        if port_open; then
            printf 'endpoint: http://%s:%s/v1\n' "$HOST" "$PORT"
        else
            printf 'port %s not answering yet (still loading?)\n' "$PORT"
        fi
        return 0
    fi
    rm -f "$PIDFILE"
    printf 'not running\n'
    return 1
}

logs() {
    [ -f "$OUTLOG" ] || : >"$OUTLOG"
    [ -f "$ENGLOG" ] || : >"$ENGLOG"
    tail -n 50 -f "$OUTLOG" "$ENGLOG"
}

cmd=${1:-start}
[ $# -gt 0 ] && shift
case "$cmd" in
    start) start "$@" ;;
    stop) stop ;;
    restart) stop; start "$@" ;;
    status) status ;;
    logs) logs ;;
    -h | --help | help)
        awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
        ;;
    *)
        die "unknown action '$cmd' (use start, stop, restart, status, logs)"
        ;;
esac
