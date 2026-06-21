#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$SCRIPT_DIR/run"
cd "$SCRIPT_DIR"

mkdir -p "$SCRIPT_DIR/logs"
[ -d "$SCRIPT_DIR/logs" ] || {
    echo "Failed to create log directory: $SCRIPT_DIR/logs" >&2
    exit 1
}

mkdir -p "$PID_DIR"
[ -d "$PID_DIR" ] || {
    echo "Failed to create PID directory: $PID_DIR" >&2
    exit 1
}

stop_service() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local pid
        pid="$(cat "$pid_file")"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$pid_file"
    fi
}

start_service() {
    local pid_file="$1"
    shift
    nohup "$@" &
    echo $! > "$pid_file"
}

stop_service "$PID_DIR/backend_api.pid"
stop_service "$PID_DIR/bridge_server.pid"
stop_service "$PID_DIR/web_dashboard.pid"

start_service "$PID_DIR/backend_api.pid" python3 "$SCRIPT_DIR/backend_api.py" > "$SCRIPT_DIR/logs/backend_api.log" 2>&1
start_service "$PID_DIR/bridge_server.pid" python3 "$SCRIPT_DIR/bridge_server.py" > "$SCRIPT_DIR/logs/bridge_server.log" 2>&1
start_service "$PID_DIR/web_dashboard.pid" python3 -m http.server 8080 --directory "$SCRIPT_DIR" > "$SCRIPT_DIR/logs/web_dashboard.log" 2>&1

echo "K.A.S.H. production services started."
echo "Backend API:   http://localhost:5001"
echo "Bridge API:    http://localhost:5000/api/live"
echo "Dashboard:     http://localhost:8080/web_dashboard.html"
echo "Logs:          $SCRIPT_DIR/logs"
echo "PIDs:          $PID_DIR"
