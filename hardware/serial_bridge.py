"""USB serial bridge: any Arduino/ESP32 running the HomeWard sketch with USE_WIFI=false
(or a Raspberry Pi gateway with sensors on USB) -> HomeWard platform.

    pip install pyserial httpx
    python hardware/serial_bridge.py --port /dev/ttyUSB0 --patient P-101 [--server http://127.0.0.1:8000]
    (Windows: --port COM3)

Each JSON line from the board is POSTed to /api/devices/<patient>/sensor; the platform's
alarm command is written back so the board's buzzer/LED follow the patient's alert state.
If the platform is unreachable the board's own offline safety rules take over.
"""
import argparse
import json
import os
import time

import httpx


def run(port: str, patient: str, server: str, key: str, baud: int = 115200):
    import serial  # pyserial

    ser = serial.Serial(port, baud, timeout=2)
    http = httpx.Client(timeout=3)
    url = f"{server.rstrip('/')}/api/devices/{patient}/sensor"
    print(f"bridging {port} -> {url}")
    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line.startswith("{"):
            continue
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in body:
            print("board:", body["error"])
            continue
        try:
            r = http.post(url, json=body, headers={"X-Device-Key": key})
            r.raise_for_status()
            cmd = r.json()
            ser.write((json.dumps({"alarm": cmd["alarm"], "led": cmd["led"]}) + "\n").encode())
            print(f"{time.strftime('%H:%M:%S')} {body} -> tier {cmd['tier']} alarm {cmd['alarm']}")
        except Exception as e:  # board falls back to its own local rules
            print("platform unreachable:", e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--patient", default="P-101")
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--key", default=os.environ.get("HOMEWARD_DEVICE_KEY", "demo-device-key-change-me"))
    a = ap.parse_args()
    run(a.port, a.patient, a.server, a.key)
