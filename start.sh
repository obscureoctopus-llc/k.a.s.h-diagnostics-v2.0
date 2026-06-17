#!/usr/bin/env bash
# K.A.S.H. Diagnostics v2.1 — Start Script (Linux / macOS / Pi)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Install dependencies if needed
if ! python3 -c "import fastapi" 2>/dev/null; then
    echo "[K.A.S.H.] Installing dependencies..."
    pip3 install -r requirements.txt
fi

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║  K.A.S.H. DIAGNOSTICS v2.1                  ║"
echo "  ║  http://localhost:8000                       ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

python3 kash_diagnostics.py "$@"
