#!/bin/bash

# --- CONFIGURATION ---
# Replace these with your actual absolute paths
PYTHON_PATH="/usr/bin/python3"
SCRIPT_PATH="/home/username/public_html/yourcode.py"
LOG_PATH="/home/username/public_html/logfile.log"
# ---------------------

# Check if the script is already running to prevent duplicates
PID=$(pgrep -f "$SCRIPT_PATH")

if [ -n "$PID" ]; then
    echo "Warning: Script is already running with PID $PID."
    echo "If you want to restart it, kill it first using: kill $PID"
    exit 1
fi

echo "Starting $SCRIPT_PATH in the background..."

# Launch using nohup and backgrounding
nohup $PYTHON_PATH $SCRIPT_PATH > $LOG_PATH 2>&1 &

# Brief pause to let the process initialize
sleep 1

# Confirm it started and show the new PID
NEW_PID=$(pgrep -f "$SCRIPT_PATH")
if [ -n "$NEW_PID" ]; then
    echo "Success! Script is running in the background."
    echo "Process ID (PID): $NEW_PID"
    echo "Logs are being written to: $LOG_PATH"
else
    echo "Error: Failed to start the script. Check $LOG_PATH for details."
fi
