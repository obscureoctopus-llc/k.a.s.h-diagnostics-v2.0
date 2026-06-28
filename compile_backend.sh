#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

fail() {
  echo "[compile_backend] ERROR: $1" >&2
  exit "${2:-1}"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1" 10
}

require_cmd python3
require_cmd pip3

echo "[compile_backend] python3: $(python3 --version)"
echo "[compile_backend] pip3: $(pip3 --version | head -n 1)"
if command -v pyinstaller >/dev/null 2>&1; then
  echo "[compile_backend] pyinstaller: $(pyinstaller --version)"
else
  echo "[compile_backend] pyinstaller: not installed globally (will install into .venv)"
fi

if [[ ! -d .venv ]]; then
  echo "[compile_backend] Creating virtual environment..."
  python3 -m venv .venv || fail "Failed to create virtual environment" 11
fi

# shellcheck disable=SC1091
source .venv/bin/activate || fail "Failed to activate virtual environment" 12

python -m pip install --upgrade pip >/dev/null || fail "Failed to upgrade pip" 13
python -m pip install -r requirements.txt pyinstaller || fail "Failed to install requirements and pyinstaller" 14

[[ -f src/kash_service.py ]] || fail "Missing src/kash_service.py" 15
[[ -f src/index.html ]] || fail "Missing src/index.html" 16
[[ -f src/styles.css ]] || fail "Missing src/styles.css" 17
python3 -c "import fastapi, serial, uvicorn" || fail "Pre-flight import verification failed" 18

python -m PyInstaller \
  --onefile \
  --name kash-diagnostics \
  --add-data "src/index.html:src" \
  --add-data "src/styles.css:src" \
  --hidden-import serial \
  --hidden-import serial.tools \
  --hidden-import serial.tools.list_ports \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops \
  --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols \
  --hidden-import uvicorn.protocols.http \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan \
  --hidden-import uvicorn.lifespan.on \
  --hidden-import fastapi \
  --hidden-import anyio \
  --hidden-import anyio._backends._asyncio \
  --strip \
  --clean \
  src/kash_service.py || fail "PyInstaller build failed" 19

BINARY="dist/kash-diagnostics"
[[ -f "$BINARY" ]] || fail "Expected binary not found at $BINARY" 20
SIZE_MB="$(python3 - <<'PY' || exit 1
from pathlib import Path
binary = Path('dist/kash-diagnostics')
print(f"{binary.stat().st_size / (1024 * 1024):.2f}")
PY
)" || fail "Failed to calculate binary size" 21
sha256sum "$BINARY" > dist/kash-diagnostics.sha256 || fail "Failed to generate checksum" 22

echo "[compile_backend] Build complete"
echo "[compile_backend] Binary: $BINARY (${SIZE_MB} MB)"
echo "[compile_backend] Checksum: dist/kash-diagnostics.sha256"
echo "[compile_backend] Usage: ./dist/kash-diagnostics"
echo "[compile_backend] Environment variables:"
echo "  KASH_HOST=${KASH_HOST:-0.0.0.0}"
echo "  KASH_PORT=${KASH_PORT:-8000}"
echo "  KASH_LOG_LEVEL=${KASH_LOG_LEVEL:-INFO}"
echo "  KASH_GPIO_PORT=${KASH_GPIO_PORT:-/dev/ttyAMA0}"
echo "  KASH_BAUD_RATE=${KASH_BAUD_RATE:-9600}"
echo "  KASH_RECONNECT_INTERVAL=${KASH_RECONNECT_INTERVAL:-5.0}"
