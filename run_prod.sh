#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p "$SCRIPT_DIR/logs"

pkill -f "python3 bridge_server.py" || true
pkill -f "python3 backend_api.py" || true
pkill -f "python3 -m http.server 8080" || true

nohup python3 backend_api.py > "$SCRIPT_DIR/logs/backend_api.log" 2>&1 &
nohup python3 bridge_server.py > "$SCRIPT_DIR/logs/bridge_server.log" 2>&1 &
nohup python3 -m http.server 8080 --directory "$SCRIPT_DIR" > "$SCRIPT_DIR/logs/web_dashboard.log" 2>&1 &

echo "K.A.S.H. production services started."
echo "Backend API:   http://localhost:5001"
echo "Bridge API:    http://localhost:5000/api/live"
echo "Dashboard:     http://localhost:8080/web_dashboard.html"
echo "Logs:          $SCRIPT_DIR/logs"
