# K.A.S.H. Diagnostics v2.1

**Universal Vehicle Diagnostic Platform**
*If it has wheels and a computer, K.A.S.H. can diagnose it.*

Copyright © 2026 ObscureOctopus LLC. All Rights Reserved.

---

## What It Does

Real OBD-II/CAN/K-Line/J1939/NMEA2000 diagnostic engine with a web UI.
Runs as a local web server — no Electron, no fake data.

- **Cars & Light Trucks** — OBD-II (1996+), OBD-I (1980–1995)
- **Heavy-Duty Trucks** — SAE J1939, J1708
- **Motorcycles** — KDS, HDS, YDS, SDS (Kawasaki, Honda, Yamaha, Suzuki)
- **Marine** — NMEA 2000, SmartCraft
- **Agriculture** — ISOBUS (ISO 11783)
- **+** ATVs, UTVs, Snowmobiles, Golf Carts, Forklifts, EVs, RVs, Construction

## Quick Start

### Windows
```
Double-click start.bat
```

### Linux / Pi / macOS
```bash
chmod +x start.sh
./start.sh
```

### Manual
```bash
cd <file_path>  # replace <file_path> with your cloned repository path
# example: cd ~/k.a.s.h-diagnostics-v2.0
pip install -r requirements.txt
python kash_diagnostics.py
```

Then open **http://localhost:8000** in your browser.

## Architecture

```
kash_diagnostics.py    ← Backend: FastAPI server + diagnostic engine (single file)
KASH_Diagnostics.html  ← Frontend: served by backend at localhost:8000
```

- `python kash_diagnostics.py` starts the web server on port 8000
- The HTML frontend is served from the same directory
- All data comes from real hardware — no simulation, no fake data
- When no adapter is connected, UI shows "No Adapter" state

## Hardware

Works with any supported adapter:

- **CAN**: USB CAN adapters (python-can compatible)
- **Serial/K-Line**: USB-to-serial, /dev/ttyAMA0, COM ports
- **OBD-II**: Any adapter that exposes serial or CAN interface

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web UI |
| GET | `/health` | Health check |
| GET | `/api/status` | Connection status, detected protocol |
| POST | `/api/connect` | Auto-detect vehicle and connect |
| POST | `/api/disconnect` | Disconnect from vehicle |
| GET | `/api/scan` | Scan all DTCs |
| POST | `/api/clear` | Clear DTCs |
| GET | `/api/vin` | Read VIN |
| GET | `/api/readiness` | I/M readiness monitors |
| GET | `/api/freeze` | Freeze frame data |
| GET | `/api/modules` | Scan connected modules |
| GET | `/api/dtc/{code}` | Look up DTC in database |
| GET | `/api/procedures` | Diagnostic procedures |
| GET | `/api/vehicles` | Vehicle database |
| WS | `/ws/live` | Live PID data stream |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KASH_HOST` | `0.0.0.0` | Listen address |
| `KASH_PORT` | `8000` | Listen port |
| `KASH_LOG_LEVEL` | `INFO` | Log level |

## CLI Mode

```bash
python kash_diagnostics.py --coverage   # Print full vehicle database
python kash_diagnostics.py --dtc P0420  # Look up a DTC
```
