#!/usr/bin/env python3
"""K.A.S.H. Diagnostics backend — live OBD-II via ELM327 USB/BT adapter.
Talks JSON lines over stdin/stdout. No simulation. Real bus only."""
import json, sys, time, logging
from typing import Optional

log = logging.getLogger("kash")
logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(message)s")

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.stderr.write("FATAL: pyserial not installed. Run: pip install pyserial\n")
    sys.exit(2)


# ── Generic OBD-II DTC descriptions (subset, expandable) ────────────────
DTC_DB = {
    "P0010":"A Camshaft Position Actuator Circuit (Bank 1)",
    "P0011":"A Camshaft Position - Timing Over-Advanced (Bank 1)",
    "P0101":"MAF Sensor Circuit Range/Performance",
    "P0102":"MAF Sensor Circuit Low Input",
    "P0103":"MAF Sensor Circuit High Input",
    "P0110":"IAT Sensor Circuit",
    "P0115":"ECT Sensor Circuit",
    "P0120":"TPS / Pedal Position Sensor A Circuit",
    "P0128":"Coolant Below Thermostat Regulating Temp",
    "P0130":"O2 Sensor Circuit (Bank 1 Sensor 1)",
    "P0131":"O2 Sensor Low Voltage (Bank 1 Sensor 1)",
    "P0132":"O2 Sensor High Voltage (Bank 1 Sensor 1)",
    "P0133":"O2 Sensor Slow Response (Bank 1 Sensor 1)",
    "P0134":"O2 Sensor No Activity (Bank 1 Sensor 1)",
    "P0135":"O2 Sensor Heater Circuit (Bank 1 Sensor 1)",
    "P0136":"O2 Sensor Circuit (Bank 1 Sensor 2)",
    "P0137":"O2 Sensor Low Voltage (Bank 1 Sensor 2)",
    "P0138":"O2 Sensor High Voltage (Bank 1 Sensor 2)",
    "P0140":"O2 Sensor No Activity (Bank 1 Sensor 2)",
    "P0141":"O2 Sensor Heater Circuit (Bank 1 Sensor 2)",
    "P0150":"O2 Sensor Circuit (Bank 2 Sensor 1)",
    "P0171":"System Too Lean (Bank 1)",
    "P0172":"System Too Rich (Bank 1)",
    "P0174":"System Too Lean (Bank 2)",
    "P0175":"System Too Rich (Bank 2)",
    "P0200":"Injector Circuit Malfunction",
    "P0300":"Random/Multiple Cylinder Misfire Detected",
    "P0301":"Cylinder 1 Misfire Detected",
    "P0302":"Cylinder 2 Misfire Detected",
    "P0303":"Cylinder 3 Misfire Detected",
    "P0304":"Cylinder 4 Misfire Detected",
    "P0305":"Cylinder 5 Misfire Detected",
    "P0306":"Cylinder 6 Misfire Detected",
    "P0307":"Cylinder 7 Misfire Detected",
    "P0308":"Cylinder 8 Misfire Detected",
    "P0325":"Knock Sensor 1 Circuit (Bank 1)",
    "P0335":"Crankshaft Position Sensor A Circuit",
    "P0340":"Camshaft Position Sensor A Circuit (Bank 1)",
    "P0401":"EGR Insufficient Flow Detected",
    "P0402":"EGR Excessive Flow Detected",
    "P0420":"Catalyst System Efficiency Below Threshold (Bank 1)",
    "P0430":"Catalyst System Efficiency Below Threshold (Bank 2)",
    "P0440":"EVAP System Malfunction",
    "P0441":"EVAP Incorrect Purge Flow",
    "P0442":"EVAP Small Leak Detected",
    "P0446":"EVAP Vent Control Circuit",
    "P0455":"EVAP Large Leak Detected",
    "P0500":"Vehicle Speed Sensor A",
    "P0505":"Idle Air Control System",
    "P0506":"Idle Control System RPM Lower Than Expected",
    "P0507":"Idle Control System RPM Higher Than Expected",
    "P0600":"Serial Communication Link",
    "P0700":"Transmission Control System Malfunction",
}


class ELM327:
    def __init__(self):
        self.ser: Optional[serial.Serial] = None
        self.port = None
        self.adapter_id = ""
        self.protocol_num = ""

    @staticmethod
    def list_ports():
        out = []
        for p in list_ports.comports():
            out.append({"device": p.device, "desc": p.description or "", "hwid": p.hwid or ""})
        return out

    def _cmd(self, cmd: str, timeout: float = 1.5) -> str:
        if not self.ser: raise RuntimeError("not open")
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\r").encode("ascii"))
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            n = self.ser.in_waiting
            if n:
                buf += self.ser.read(n)
                if b">" in buf: break
            else:
                time.sleep(0.02)
        return buf.decode("ascii", errors="ignore").replace("\r", "\n").replace(">", "").strip()

    def open(self, port: Optional[str] = None):
        baud_candidates = [38400, 115200, 9600]
        ports = [port] if port else [p["device"] for p in self.list_ports()]
        if not ports:
            raise RuntimeError("No COM ports found — plug in ELM327 USB/BT adapter")
        tried_msgs = []
        for dev in ports:
            for baud in baud_candidates:
                try:
                    self.ser = serial.Serial(dev, baud, timeout=1)
                    time.sleep(0.4)
                    self.ser.reset_input_buffer()
                    r = self._cmd("ATZ", 2.5)
                    if "ELM" not in r.upper():
                        self.ser.close(); self.ser = None
                        tried_msgs.append(f"{dev}@{baud}: no ELM response")
                        continue
                    self._cmd("ATE0"); self._cmd("ATL0"); self._cmd("ATS0")
                    self._cmd("ATH0"); self._cmd("ATSP0")
                    self.adapter_id = self._cmd("ATI", 1.0)
                    self._cmd("0100", 4.0)
                    self.protocol_num = self._cmd("ATDPN", 1.0)
                    self.port = dev
                    return
                except Exception as e:
                    tried_msgs.append(f"{dev}@{baud}: {e}")
                    try:
                        if self.ser: self.ser.close()
                    except Exception: pass
                    self.ser = None
        raise RuntimeError("ELM327 not detected on any port. Tried: " + "; ".join(tried_msgs))

    def close(self):
        try:
            if self.ser: self.ser.close()
        except Exception: pass
        self.ser = None; self.port = None

    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def query(self, mode_pid: str) -> str:
        return self._cmd(mode_pid, 1.5)

    @staticmethod
    def _bytes(resp: str, mode_byte: str):
        """Extract data bytes after the response header (e.g. '410C' for mode 01 PID 0C)."""
        h = resp.replace(" ", "").replace("\n", "").upper()
        i = h.find(mode_byte)
        if i < 0: return None
        return h[i + len(mode_byte):]

    def read_rpm(self):
        d = self._bytes(self.query("010C"), "410C")
        if d and len(d) >= 4:
            a, b = int(d[0:2], 16), int(d[2:4], 16)
            return ((a << 8) + b) // 4
        return None

    def read_speed_kph(self):
        d = self._bytes(self.query("010D"), "410D")
        return int(d[0:2], 16) if d and len(d) >= 2 else None

    def read_coolant_c(self):
        d = self._bytes(self.query("0105"), "4105")
        return int(d[0:2], 16) - 40 if d and len(d) >= 2 else None

    def read_intake_c(self):
        d = self._bytes(self.query("010F"), "410F")
        return int(d[0:2], 16) - 40 if d and len(d) >= 2 else None

    def read_maf_gps(self):
        d = self._bytes(self.query("0110"), "4110")
        if d and len(d) >= 4:
            return ((int(d[0:2], 16) << 8) + int(d[2:4], 16)) / 100.0
        return None

    def read_throttle_pct(self):
        d = self._bytes(self.query("0111"), "4111")
        return round(int(d[0:2], 16) * 100 / 255.0, 1) if d and len(d) >= 2 else None

    def read_engine_load_pct(self):
        d = self._bytes(self.query("0104"), "4104")
        return round(int(d[0:2], 16) * 100 / 255.0, 1) if d and len(d) >= 2 else None

    def read_fuel_pct(self):
        d = self._bytes(self.query("012F"), "412F")
        return round(int(d[0:2], 16) * 100 / 255.0, 1) if d and len(d) >= 2 else None

    def read_voltage(self):
        r = self._cmd("ATRV", 1.0)
        try:
            return float(r.replace("V", "").strip())
        except Exception:
            return None

    def read_vin(self):
        r = self._cmd("0902", 2.5)
        h = r.replace(" ", "").replace("\n", "").upper()
        # strip frame counters / 4902 headers, take ASCII pairs
        h = h.replace("4902", "")
        # remove ISO-TP frame indices (single hex digit at line starts already collapsed)
        out = []
        for i in range(0, len(h) - 1, 2):
            try:
                v = int(h[i:i+2], 16)
                if 32 <= v < 127:
                    out.append(chr(v))
            except Exception:
                continue
        vin = "".join(out)
        # VIN is 17 chars — find a clean run
        for start in range(len(vin)):
            chunk = vin[start:start+17]
            if len(chunk) == 17 and chunk.isalnum():
                return chunk
        return vin[-17:] if len(vin) >= 17 else vin or None

    def read_dtcs(self):
        r = self._cmd("03", 2.5)
        h = r.replace(" ", "").replace("\n", "").upper()
        i = h.find("43")
        if i < 0: return []
        h = h[i+2:]
        # Drop leading count byte if present (ISO-15765-4)
        if len(h) >= 2: h = h[2:]
        codes = []
        for i in range(0, len(h) - 3, 4):
            b1 = int(h[i:i+2], 16); b2 = int(h[i+2:i+4], 16)
            if b1 == 0 and b2 == 0: continue
            prefix = "PCBU"[(b1 >> 6) & 0x3]
            code = f"{prefix}{(b1 >> 4) & 0x3}{b1 & 0x0F:X}{b2:02X}"
            codes.append(code)
        return codes

    def clear_dtcs(self):
        return self._cmd("04", 2.0)


# ── Command dispatcher ─────────────────────────────────────────────────
elm = ELM327()


def handle(cmd: str, args: dict) -> dict:
    if cmd == "ping":
        return {"pong": True, "version": "2.0"}

    if cmd == "list_ports":
        return {"ports": elm.list_ports()}

    if cmd == "connect":
        elm.open(args.get("port"))
        return {
            "connected": True, "port": elm.port,
            "adapter": elm.adapter_id, "protocol_num": elm.protocol_num,
        }

    if cmd == "disconnect":
        elm.close()
        return {"connected": False}

    if cmd == "status":
        return {"connected": elm.is_open(), "port": elm.port,
                "adapter": elm.adapter_id, "protocol_num": elm.protocol_num}

    if cmd == "scan":
        if not elm.is_open(): raise RuntimeError("not connected")
        codes = elm.read_dtcs()
        vin = elm.read_vin()
        dtcs = [{"code": c, "desc": DTC_DB.get(c, "Unknown DTC")} for c in codes]
        return {"vin": vin, "dtcs": dtcs, "count": len(dtcs)}

    if cmd == "clear_dtcs":
        if not elm.is_open(): raise RuntimeError("not connected")
        return {"raw": elm.clear_dtcs(), "ok": True}

    if cmd == "live_metrics":
        if not elm.is_open(): raise RuntimeError("not connected")
        return {
            "rpm":          elm.read_rpm(),
            "speed_kph":    elm.read_speed_kph(),
            "coolant_c":    elm.read_coolant_c(),
            "intake_c":     elm.read_intake_c(),
            "maf_gps":      elm.read_maf_gps(),
            "throttle_pct": elm.read_throttle_pct(),
            "load_pct":     elm.read_engine_load_pct(),
            "fuel_pct":     elm.read_fuel_pct(),
            "voltage":      elm.read_voltage(),
        }

    if cmd == "get_pid":
        if not elm.is_open(): raise RuntimeError("not connected")
        pid = (args.get("pid") or "").strip()
        if not pid: raise RuntimeError("missing pid")
        return {"raw": elm.query(pid)}

    if cmd == "lookup_dtc":
        code = (args.get("code") or "").upper().strip()
        return {"code": code, "desc": DTC_DB.get(code), "found": code in DTC_DB}

    raise RuntimeError(f"unknown cmd: {cmd}")


def main():
    if "--serve" not in sys.argv:
        print("K.A.S.H. Diagnostics backend\nUsage: python kash_diagnostics.py --serve")
        return
    sys.stderr.write("KASH-READY\n"); sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        rid = None
        try:
            req = json.loads(line); rid = req.get("id")
            result = handle(req.get("cmd", ""), req.get("args") or {})
            resp = {"id": rid, "ok": True, "result": result}
        except Exception as e:
            resp = {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(resp) + "\n"); sys.stdout.flush()


if __name__ == "__main__":
    main()
