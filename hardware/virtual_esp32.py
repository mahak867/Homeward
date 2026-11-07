"""Virtual ESP32: speaks exactly the same protocol as the real board, so the hardware path
can be demonstrated and tested without the physical sensor.

    python hardware/virtual_esp32.py --patient P-101            # normal readings
    python hardware/virtual_esp32.py --patient P-101 --hypoxia  # SpO2 drifting down
    python hardware/virtual_esp32.py --patient P-101 --finger-off
    python hardware/virtual_esp32.py --patient P-101 --press-button   # acknowledge alerts

Prints the buzzer/LED state the platform commands, like the real board would play it.
"""
import argparse
import sys
from pathlib import Path
import random
import time

import httpx

ICON = {"off": "🔇", "chirp": "🔈 chirp", "slow": "🔉 slow beep", "fast": "🔊 FAST ALARM"}


def readings(hypoxia: bool, finger_off: bool, i: int) -> dict:
    if finger_off:
        return {"finger": False, "temp": 36.9}
    drop = min(12, i * 0.8) if hypoxia else 0
    return {"hr": round(random.gauss(84 + drop * 2.5, 2)), "spo2": round(random.gauss(96 - drop, 0.6)),
            "temp": round(random.gauss(37.0, 0.05), 1), "finger": True, "sqi": 0.9}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patient", default="P-101")
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--key", default=None, help="this device's key (default: derived from HOMEWARD_DEVICE_KEY for the patient's hub)")
    ap.add_argument("--hypoxia", action="store_true")
    ap.add_argument("--finger-off", action="store_true")
    ap.add_argument("--press-button", action="store_true")
    ap.add_argument("--every", type=float, default=4.0)
    a = ap.parse_args()
    if not a.key:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from homeward.core import device_key
        a.key = device_key(f"HUB-{a.patient}")
    url = f"{a.server.rstrip('/')}/api/devices/{a.patient}/sensor"
    with httpx.Client(timeout=3) as http:
        if a.press_button:
            r = http.post(url, json={"finger": True, "button": True}, headers={"X-Device-Key": a.key})
            if r.status_code != 200:
                print(f"rejected by platform ({r.status_code}): {r.json().get('detail')}")
                return
            print("button pressed - acknowledged alerts:", r.json()["acknowledged"])
            return
        i = 0
        while True:
            body = readings(a.hypoxia, a.finger_off, i)
            try:
                r = http.post(url, json={**body, "firmware": "virtual-esp32"}, headers={"X-Device-Key": a.key})
                if r.status_code != 200:
                    print(f"rejected by platform ({r.status_code}): {r.json().get('detail')}", flush=True)
                    time.sleep(a.every)
                    continue
                cmd = r.json()
                print(f"{time.strftime('%H:%M:%S')} sent {body}  ->  LED {cmd['led']:5s} buzzer {ICON[cmd['alarm']]}  (tier {cmd['tier']})", flush=True)
            except Exception as e:
                print("platform unreachable:", e)
            i += 1
            time.sleep(a.every)


if __name__ == "__main__":
    main()
