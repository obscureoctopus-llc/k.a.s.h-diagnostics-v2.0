#!/usr/bin/env python3
"""
K.A.S.H. RS-485 HARDWARE BRIDGE (PRODUCTION)
Strict mode: NO simulation/fake data.
If hardware is unavailable, status reports NOT_CONNECTED.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import serial


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "3.0"
HARDWARE_CONNECTED = "CONNECTED"
HARDWARE_NOT_CONNECTED = "NOT_CONNECTED"
GPIO_PORT = os.getenv('KASH_GPIO_PORT', '/dev/ttyAMA0')
BAUD_RATE = int(os.getenv('KASH_BAUD_RATE', '9600'))
HTTP_PORT = int(os.getenv('KASH_BRIDGE_PORT', '5000'))
DISCONNECTED_POLL_INTERVAL = 0.25


def _get_reconnect_interval() -> float:
    raw_value = os.getenv('KASH_RECONNECT_INTERVAL', '5')
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError("KASH_RECONNECT_INTERVAL must be a numeric value.") from exc


RECONNECT_INTERVAL = _get_reconnect_interval()

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(name)s — %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, 'kash_bridge.log')),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger('kash.bridge')


class HardwareBridge:
    """RS-485 serial bridge to vehicle diagnostics hardware."""

    def __init__(self):
        self.ser = None
        self._latest_data_lock = threading.Lock()
        self.latest_data = {
            "status": "NOT_CONNECTED",
            "timestamp": datetime.now().isoformat(),
            "raw_frame": "",
            "hardware_state": HARDWARE_NOT_CONNECTED,
            "frame_type": "UNKNOWN",
        }
        self._last_connect_attempt = 0.0
        self._connect()

    def _set_not_connected(self, reason: str):
        with self._latest_data_lock:
            self.latest_data.update({
                "status": "NOT_CONNECTED",
                "timestamp": datetime.now().isoformat(),
                "raw_frame": "",
                "hardware_state": HARDWARE_NOT_CONNECTED,
                "frame_type": "UNKNOWN",
            })
        log.warning("Hardware not connected: %s", reason)

    def _connect(self):
        """Attempt to open serial connection."""
        try:
            serial_conn = serial.Serial(
                GPIO_PORT,
                BAUD_RATE,
                timeout=1,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
            with self._latest_data_lock:
                self.ser = serial_conn
                self.latest_data.update({
                    "status": "KASH READY",
                    "timestamp": datetime.now().isoformat(),
                    "hardware_state": HARDWARE_CONNECTED,
                })
            log.info("✓ Serial port connected: %s @ %s baud", GPIO_PORT, BAUD_RATE)
        except Exception as exc:
            self._last_connect_attempt = time.time()
            self.ser = None
            self._set_not_connected(str(exc))

    def _should_retry_connect(self) -> bool:
        return (time.time() - self._last_connect_attempt) >= RECONNECT_INTERVAL

    def read_loop(self):
        """Main thread: read serial data and update latest_data."""
        while True:
            try:
                with self._latest_data_lock:
                    serial_conn = self.ser

                if not serial_conn or not serial_conn.is_open:
                    if self._should_retry_connect():
                        self._connect()
                    time.sleep(DISCONNECTED_POLL_INTERVAL)
                    continue

                if serial_conn.in_waiting <= 0:
                    time.sleep(0.01)
                    continue

                line = serial_conn.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    time.sleep(0.01)
                    continue

                log.debug("📥 RX: %s", line)
                with self._latest_data_lock:
                    self.latest_data.update({
                        "status": "KASH READY",
                        "timestamp": datetime.now().isoformat(),
                        "raw_frame": line,
                        "hardware_state": HARDWARE_CONNECTED,
                        "frame_type": self._detect_frame_type(line),
                    })
            except Exception as exc:
                log.error("Serial read error: %s", exc)
                self.disconnect()
                self._set_not_connected(str(exc))
                time.sleep(0.5)

    def _detect_frame_type(self, frame: str) -> str:
        """Detect vehicle protocol from raw frame."""
        if frame.startswith('29'):
            return "CAN_29bit"
        if frame.startswith('11'):
            return "CAN_11bit"
        if frame.startswith('J1939'):
            return "J1939"
        if frame.startswith('KDS'):
            return "KAWASAKI_KDS"
        if frame.startswith('ISO'):
            return "ISO14230_KWP"
        return "UNKNOWN"

    def get_status(self) -> dict:
        """Return current hardware status."""
        with self._latest_data_lock:
            return self.latest_data.copy()

    def disconnect(self):
        """Close serial connection gracefully."""
        with self._latest_data_lock:
            serial_conn = self.ser
            self.ser = None
        if serial_conn and serial_conn.is_open:
            serial_conn.close()
            log.info("Serial port closed.")


class BridgeHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for bridge data."""

    def do_GET(self):
        """Serve bridge endpoints."""
        if self.path == '/api/live':
            data = bridge.get_status()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))
            return

        if self.path == '/api/bridge/status':
            data = {
                "service": "K.A.S.H. RS-485 Bridge",
                "version": VERSION,
                "status": "running",
                "timestamp": datetime.now().isoformat(),
                "hardware": bridge.get_status()["hardware_state"],
                "gpio_port": GPIO_PORT,
                "baud_rate": BAUD_RATE,
                "http_port": HTTP_PORT,
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))
            return

        self.send_response(404)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({
            "error": "Not found",
            "available": ["GET /api/live", "GET /api/bridge/status"],
        }).encode('utf-8'))

    def log_message(self, format, *args):
        """Custom logging."""
        log.info("%s — %s", self.client_address[0], format % args)


if __name__ == '__main__':
    log.info("=" * 60)
    log.info("K.A.S.H. RS-485 HARDWARE BRIDGE v%s", VERSION)
    log.info("=" * 60)

    bridge = HardwareBridge()

    serial_thread = threading.Thread(target=bridge.read_loop, daemon=True)
    serial_thread.start()
    log.info("✓ Serial listener thread started.")

    server = HTTPServer(('0.0.0.0', HTTP_PORT), BridgeHTTPHandler)
    log.info("📡 Bridge API listening on http://localhost:%s", HTTP_PORT)
    log.info("   Hardware: %s", bridge.get_status()["hardware_state"])
    log.info("   Status: %s", bridge.get_status()["status"])

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("🛑 Shutting down...")
    finally:
        bridge.disconnect()
        server.shutdown()
        log.info("✓ Bridge stopped.")
