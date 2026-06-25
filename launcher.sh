#!/bin/bash

################################################################################
# K.A.S.H. DIAGNOSTICS v2.0 — UNIVERSAL LAUNCHER
# Spins up backend, bridge server, and web dashboard in separate terminals
################################################################################

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  K.A.S.H. UNIVERSAL DIAGNOSTICS ENGINE v2.0${NC}"
echo -e "${GREEN}  Launching: Backend | Bridge Server | Web Dashboard${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"

# Check if we're in the repo root
if [ ! -f "kash_diagnostics.py" ]; then
    echo -e "${RED}ERROR: kash_diagnostics.py not found in current directory!${NC}"
    echo -e "Please run from the repo root: cd /path/to/k.a.s.h-diagnostics-v2.0"
    exit 1
fi

# Install Python dependencies if needed
missing_modules=()
python3 -c "import fastapi" 2>/dev/null || missing_modules+=("fastapi")
python3 -c "import serial" 2>/dev/null || missing_modules+=("pyserial")
python3 -c "import uvicorn" 2>/dev/null || missing_modules+=("uvicorn")

if [ ${#missing_modules[@]} -gt 0 ]; then
    echo -e "${YELLOW}[setup] Missing Python modules:${NC} ${missing_modules[*]}"
    echo -e "${YELLOW}[setup] Installing Python dependencies from requirements.txt...${NC}"
    if ! pip3 install -r requirements.txt; then
        echo -e "${RED}ERROR: Failed to install Python dependencies from requirements.txt${NC}"
        echo -e "${RED}Please check network access, pip3 permissions/version, and requirements.txt availability.${NC}"
        exit 1
    fi
fi

echo -e "${YELLOW}[1/3] Spawning Backend API server (Python)...${NC}"
gnome-terminal --tab --title="K.A.S.H. Backend" -- bash -c "
python3 backend_api.py
echo 'Press ENTER to exit'; read" 2>/dev/null || \
xterm -title "K.A.S.H. Backend" -e "python3 backend_api.py; read -p 'Press ENTER to exit'" &

sleep 1

echo -e "${YELLOW}[2/3] Spawning RS-485 Bridge server...${NC}"
gnome-terminal --tab --title="K.A.S.H. Bridge" -- bash -c "
python3 bridge_server.py
echo 'Press ENTER to exit'; read" 2>/dev/null || \
xterm -title "K.A.S.H. Bridge" -e "python3 bridge_server.py; read -p 'Press ENTER to exit'" &

sleep 1

echo -e "${YELLOW}[3/3] Spawning Web Dashboard (HTTP server)...${NC}"
gnome-terminal --tab --title="K.A.S.H. Dashboard" -- bash -c "
python3 -m http.server 8080 --directory .
echo 'Press ENTER to exit'; read" 2>/dev/null || \
xterm -title "K.A.S.H. Dashboard" -e "python3 -m http.server 8080 --directory .; read -p 'Press ENTER to exit'" &

sleep 2

echo -e "${GREEN}✓ All services launched!${NC}"
echo ""
echo -e "${GREEN}SERVICE ENDPOINTS:${NC}"
echo -e "  Backend API:      ${YELLOW}http://localhost:5001${NC}"
echo -e "  RS-485 Bridge:    ${YELLOW}http://localhost:5000/api/live${NC}"
echo -e "  Web Dashboard:    ${YELLOW}http://localhost:8080${NC}"
echo ""
echo -e "${GREEN}NEXT STEPS:${NC}"
echo -e "  1. Open browser: ${YELLOW}http://localhost:8080/web_dashboard.html${NC}"
echo -e "  2. Check Backend logs (Terminal 1) for diagnostics engine status"
echo -e "  3. Monitor Bridge (Terminal 2) for RS-485 data ingestion"
echo -e "  4. Watch Dashboard (Terminal 3) HTTP access logs"
echo ""
echo -e "${GREEN}To stop all services: Kill each terminal (Ctrl+C)${NC}"
echo ""
