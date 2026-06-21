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
            if ! kill "$pid" 2>/dev/null; then
                echo "Warning: failed to stop process $pid from $pid_file" >&2
            fi
        fi
        rm -f "$pid_file"
    fi
}

start_service() {
    local pid_file="$1"
    local log_file="$2"
    shift 2
    nohup "$@" > "$log_file" 2>&1 &
    echo $! > "$pid_file"
}

stop_service "$PID_DIR/backend_api.pid"
stop_service "$PID_DIR/bridge_server.pid"
stop_service "$PID_DIR/web_dashboard.pid"

start_service "$PID_DIR/backend_api.pid" "$SCRIPT_DIR/logs/backend_api.log" python3 "$SCRIPT_DIR/backend_api.py"
start_service "$PID_DIR/bridge_server.pid" "$SCRIPT_DIR/logs/bridge_server.log" python3 "$SCRIPT_DIR/bridge_server.py"
start_service "$PID_DIR/web_dashboard.pid" "$SCRIPT_DIR/logs/web_dashboard.log" python3 -m http.server 8080 --directory "$SCRIPT_DIR"

echo "K.A.S.H. production services started."
echo "Backend API:   http://localhost:5001"
echo "Bridge API:    http://localhost:5000/api/live"
echo "Dashboard:     http://localhost:8080/web_dashboard.html"
echo "Logs:          $SCRIPT_DIR/logs"
echo "PIDs:          $PID_DIR"
