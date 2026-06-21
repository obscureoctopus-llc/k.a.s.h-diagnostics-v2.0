#!/usr/bin/env python3
"""
K.A.S.H. RS-485 HARDWARE BRIDGE
Listens on GPIO serial port, streams vehicle data via HTTP JSON API.
Provides /api/live endpoint for dashboard.
"""

import os
import json
import time
import serial
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import threading

# Configuration
GPIO_PORT = os.getenv('KASH_GPIO_PORT', '/dev/ttyAMA0')
BAUD_RATE = int(os.getenv('KASH_BAUD_RATE', '9600'))
HTTP_PORT = int(os.getenv('KASH_BRIDGE_PORT', '5000'))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(name)s — %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('kash_bridge.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('kash.bridge')


class HardwareBridge:
    """RS-485 serial bridge to vehicle diagnostics hardware."""

    def __init__(self):
        self.ser = None
        self.latest_data = {
            "status": "INITIALIZING",
            "timestamp": datetime.now().isoformat(),
            "raw_frame": "",
            "hardware_state": "disconnected"
        }
        self._connect()

    def _connect(self):
        """Attempt to open serial connection."""
        try:
            self.ser = serial.Serial(
                GPIO_PORT,
                BAUD_RATE,
                timeout=1,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            log.info(f"✓ Serial port connected: {GPIO_PORT} @ {BAUD_RATE} baud")
            self.latest_data["hardware_state"] = "connected"
            self.latest_data["status"] = "KASH READY"
        except Exception as e:
            log.warning(f"⚠️  Could not open {GPIO_PORT}: {e}")
            log.info("    (Hardware optional — API will simulate data)")
            self.latest_data["hardware_state"] = "not_available"
            self.latest_data["status"] = "SIMULATION_MODE"
            self.ser = None

    def read_loop(self):
        """Main thread: read serial data and update latest_data."""
        while True:
            try:
                if self.ser and self.ser.is_open and self.ser.in_waiting > 0:
                    line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        log.debug(f"📥 RX: {line}")
                        self.latest_data = {
                            "status": "KASH READY",
                            "timestamp": datetime.now().isoformat(),
                            "raw_frame": line,
                            "hardware_state": "connected",
                            "frame_type": self._detect_frame_type(line)
                        }
            except Exception as e:
                log.error(f"Serial read error: {e}")
            time.sleep(0.01)

    def _detect_frame_type(self, frame: str) -> str:
        """Detect vehicle protocol from raw frame."""
        if frame.startswith('29'):
            return "CAN_29bit"
        elif frame.startswith('11'):
            return "CAN_11bit"
        elif frame.startswith('J1939'):
            return "J1939"
        elif frame.startswith('KDS'):
            return "KAWASAKI_KDS"
        elif frame.startswith('ISO'):
            return "ISO14230_KWP"
        else:
            return "UNKNOWN"

    def get_status(self) -> dict:
        """Return current hardware status."""
        return self.latest_data.copy()

    def disconnect(self):
        """Close serial connection gracefully."""
        if self.ser and self.ser.is_open:
            self.ser.close()
            log.info("Serial port closed.")


class BridgeHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for bridge data."""

    def do_GET(self):
        """Serve /api/live endpoint."""
        if self.path == '/api/live':
            data = bridge.get_status()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))
        elif self.path == '/api/bridge/status':
            data = {
                "service": "K.A.S.H. RS-485 Bridge",
                "version": "2.0",
                "status": "running",
                "timestamp": datetime.now().isoformat(),
                "hardware": bridge.latest_data["hardware_state"],
                "gpio_port": GPIO_PORT,
                "baud_rate": BAUD_RATE,
                "http_port": HTTP_PORT,
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "Not found",
                "available": ["GET /api/live", "GET /api/bridge/status"]
            }).encode('utf-8'))

    def log_message(self, format, *args):
        """Custom logging."""
        log.info(f"{self.client_address[0]} — {format % args}")


if __name__ == '__main__':
    log.info("="*60)
    log.info("K.A.S.H. RS-485 HARDWARE BRIDGE v2.0")
    log.info("="*60)
    
    # Initialize bridge
    bridge = HardwareBridge()
    
    # Start serial read thread
    serial_thread = threading.Thread(target=bridge.read_loop, daemon=True)
    serial_thread.start()
    log.info("✓ Serial listener thread started.")
    
    # Start HTTP server
    server = HTTPServer(('0.0.0.0', HTTP_PORT), BridgeHTTPHandler)
    log.info(f"\n📡 Bridge API listening on http://localhost:{HTTP_PORT}")
    log.info(f"   Hardware: {bridge.latest_data['hardware_state']}")
    log.info(f"   Status: {bridge.latest_data['status']}\n")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("\n🛑 Shutting down...")
        bridge.disconnect()
        server.shutdown()
        log.info("✓ Bridge stopped.")
