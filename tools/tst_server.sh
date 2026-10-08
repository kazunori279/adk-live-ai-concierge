#!/usr/bin/env bash
# Start test rig server for tst_runner with required env and log redirection (#724, #725)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PORT="${AI_TERMINAL_PORT:-8021}"
HOST="${AI_TERMINAL_HOST:-127.0.0.1}"
SERVER_LOG="${AI_TERMINAL_SERVER_LOG:-/tmp/tst-server.log}"
SESSIONS_DB="${SESSIONS_DB:-/tmp/tst-sessions.db}"
REVISION="$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
RUN_LOCK_FILE="${AI_TERMINAL_RUN_LOCK:-/tmp/tst-runner.lock}"

export ENABLE_MANAGEMENT_ROUTES=1
export SESSIONS_DB="$SESSIONS_DB"
export AI_TERMINAL_REVISION="$REVISION"

check_run_lock() {
    python3 - <<EOF
import fcntl, os, sys
lock_path = "${RUN_LOCK_FILE}"
if os.path.exists(lock_path):
    try:
        fd = os.open(lock_path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except (BlockingIOError, OSError):
        holder = "unknown"
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                c = f.read().strip()
                if c:
                    holder = c.split()[0]
        except Exception:
            pass
        sys.stderr.write(f"ERROR: tst_runner is active (PID {holder}); refuse to restart/kill server while run is in progress\n")
        sys.exit(1)
EOF
}

if [[ "${1:-}" == "--kill" || "${1:-}" == "--stop" || "${1:-}" == "kill" || "${1:-}" == "stop" ]]; then
    check_run_lock
    pids=$(lsof -ti :"$PORT" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "Stopping rig server on port $PORT (PID $pids)..."
        kill $pids 2>/dev/null || true
    else
        echo "No rig server found on port $PORT"
    fi
    exit 0
fi

if [[ "${1:-}" == "--restart" || "${1:-}" == "restart" ]]; then
    check_run_lock
    pids=$(lsof -ti :"$PORT" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "Stopping existing server on port $PORT (PID $pids)..."
        kill $pids 2>/dev/null || true
        sleep 0.5
    fi
    shift || true
fi

# Check if port is in use; if replacing existing server, check lock first (#725)
pids=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [[ -n "$pids" ]]; then
    check_run_lock
    echo "Stopping existing server on port $PORT (PID $pids)..."
    kill $pids 2>/dev/null || true
    sleep 0.5
fi

if [[ "${1:-}" == "--background" || "${1:-}" == "-b" || "${1:-}" == "-d" ]]; then
    .venv/bin/python -m ai_terminal.live.main --host "$HOST" --port "$PORT" > "$SERVER_LOG" 2>&1 &
    PID=$!
    echo "Started rig server (PID $PID) on ${HOST}:${PORT} logging to ${SERVER_LOG}"
else
    echo "Starting rig server on ${HOST}:${PORT} (revision ${REVISION}) -> ${SERVER_LOG}"
    exec .venv/bin/python -m ai_terminal.live.main --host "$HOST" --port "$PORT" > "$SERVER_LOG" 2>&1
fi
