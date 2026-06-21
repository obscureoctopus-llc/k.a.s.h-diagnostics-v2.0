#!/usr/bin/env python3
"""
K.A.S.H. BACKEND API SERVER
REST interface to the universal diagnostics engine.
Listens on port 5001.
"""

import os
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading
from datetime import datetime

# Import diagnostics engine
from kash_diagnostics import (
    UniversalDiagnosticEngine, VehicleType, VEHICLE_DATABASE,
    GENERIC_DTCS, DIAGNOSTIC_PROCEDURES, MODULE_INIT_PROCEDURES
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(name)s — %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('kash_backend.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('kash.backend')

# Global diagnostics engine
engine = None


class KASHAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for diagnostic API endpoints."""

    def do_GET(self):
        """Handle GET requests."""
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        query = parse_qs(parsed_path.query)

        try:
            # ─ Status ─
            if path == '/api/status':
                self._json_response(200, {
                    "service": "K.A.S.H. Backend API",
                    "version": "2.0",
                    "status": "running",
                    "timestamp": datetime.now().isoformat(),
                    "engine_state": "ready" if engine else "not_initialized",
                })

            # ─ Vehicle database ─
            elif path == '/api/vehicles':
                vehicles = {
                    vtype.name: list(makes.keys())
                    for vtype, makes in VEHICLE_DATABASE.items()
                }
                self._json_response(200, {"vehicles": vehicles})

            # ─ Vehicle details ─
            elif path.startswith('/api/vehicles/'):
                vtype_name = path.split('/')[-1]
                try:
                    vtype = VehicleType[vtype_name.upper()]
                    makes = VEHICLE_DATABASE.get(vtype, {})
                    self._json_response(200, {
                        "vehicle_type": vtype.name,
                        "makes": {k: v for k, v in makes.items()}
                    })
                except KeyError:
                    self._json_response(404, {"error": f"Vehicle type '{vtype_name}' not found"})

            # ─ DTC lookup ─
            elif path.startswith('/api/dtc/'):
                code = path.split('/')[-1].upper()
                if code in GENERIC_DTCS:
                    dtc_info = GENERIC_DTCS[code]
                    self._json_response(200, {"code": code, **dtc_info})
                else:
                    self._json_response(404, {"error": f"DTC '{code}' not found"})

            # ─ Procedures for symptom ─
            elif path == '/api/procedures/symptom':
                symptom = query.get('q', [''])[0]
                if not symptom:
                    self._json_response(400, {"error": "Missing query parameter 'q'"})
                    return
                
                procs = engine.get_procedures_for_symptom(symptom) if engine else []
                self._json_response(200, {
                    "symptom": symptom,
                    "count": len(procs),
                    "procedures": [{
                        "id": p.id,
                        "title": p.title,
                        "difficulty": p.difficulty,
                        "time_est": p.time_est,
                        "symptoms": p.symptoms,
                    } for p in procs]
                })

            # ─ Diagnostic procedures list ─
            elif path == '/api/procedures':
                self._json_response(200, {
                    "count": len(DIAGNOSTIC_PROCEDURES),
                    "procedures": [{
                        "id": p.id,
                        "title": p.title,
                        "difficulty": p.difficulty,
                    } for p in DIAGNOSTIC_PROCEDURES]
                })

            # ─ Module init procedures ─
            elif path == '/api/init-procedures':
                self._json_response(200, {
                    "count": len(MODULE_INIT_PROCEDURES),
                    "procedures": list(MODULE_INIT_PROCEDURES.keys())
                })

            # ─ 404 ─
            else:
                self._json_response(404, {
                    "error": "Endpoint not found",
                    "available": [
                        "/api/status",
                        "/api/vehicles",
                        "/api/vehicles/{type}",
                        "/api/dtc/{code}",
                        "/api/procedures",
                        "/api/procedures/symptom?q=query",
                        "/api/init-procedures",
                    ]
                })
        except Exception as e:
            log.error(f"Error processing request: {e}", exc_info=True)
            self._json_response(500, {"error": str(e)})

    def do_POST(self):
        """Handle POST requests (diagnostics scan)."""
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        try:
            # ─ Run diagnostic scan ─
            if path == '/api/scan':
                if not engine:
                    self._json_response(503, {"error": "Diagnostics engine not initialized"})
                    return
                
                # This would run actual vehicle scan
                result = {
                    "status": "scan_initiated",
                    "message": "Diagnostic scan would run here (connect to CAN/serial)",
                    "timestamp": datetime.now().isoformat(),
                }
                self._json_response(200, result)
            else:
                self._json_response(404, {"error": "Endpoint not found"})
        except Exception as e:
            log.error(f"Error in POST: {e}", exc_info=True)
            self._json_response(500, {"error": str(e)})

    def _json_response(self, status_code: int, data: dict):
        """Send a JSON response."""
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode('utf-8'))

    def log_message(self, format, *args):
        """Override to use custom logger."""
        log.info(f"{self.client_address[0]} — {format % args}")


if __name__ == '__main__':
    # Initialize diagnostics engine
    log.info("🚀 Initializing K.A.S.H. Diagnostics Engine...")
    engine = UniversalDiagnosticEngine()
    log.info("✓ Engine ready. Diagnostics database loaded.")

    # Start HTTP server
    HOST = '0.0.0.0'
    PORT = 5001
    server = HTTPServer((HOST, PORT), KASHAPIHandler)
    log.info(f"📡 Backend API listening on {HOST}:{PORT}")
    log.info("   Dashboard API available at http://localhost:5001")
    log.info("\n" + "="*60)
    log.info("K.A.S.H. Backend ready. Waiting for requests...")
    log.info("="*60 + "\n")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("\n🛑 Shutting down...")
        server.shutdown()
        log.info("✓ Backend API stopped.")
