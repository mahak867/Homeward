"""One command to run the whole HomeWard system locally.

    pip install -r requirements.txt
    python run.py                 # http://127.0.0.1:8000  (caregiver view: /caregiver)

Starts the monitoring platform, three simulated bedside hubs (one per home) that talk to
it over real HTTP, backfills 32 h of history so personal baselines exist, then streams.
No Docker, database server or API keys needed.
"""
from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description="Smart Health HomeWard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--speed", type=float, default=300, help="simulated seconds per real second (300 = 5 min/s)")
    ap.add_argument("--autopilot", action="store_true", help="start the demo deteriorations automatically")
    a = ap.parse_args()

    from homeward import model
    if not model.MODEL_PATH.exists():
        print("No trained model found - training on synthetic cohort (about a minute)...")
        from homeward import train
        train.main()

    from homeward.core import Chaos, SimClock
    from homeward.edge import EdgeHub
    from homeward.engine import Engine
    from homeward.physiology import DEMO_PATIENTS
    from homeward.server import create_app

    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    for f in data.glob("*.db*"):  # synthetic demo data is regenerated on every run for a clean story
        f.unlink()
    clock, chaos = SimClock(speed=a.speed), Chaos()
    engine = Engine(data / "platform.db", clock, chaos)
    url = f"http://{a.host}:{a.port}"
    hubs = {p.patient_id: EdgeHub(p, url, clock, chaos, data / f"edge_{p.patient_id}.db", seed=i + 1)
            for i, p in enumerate(DEMO_PATIENTS)}
    app = create_app(engine, hubs)

    def start_hubs():
        while True:  # wait for server
            try:
                import httpx
                httpx.get(url + "/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        clock.set_paused(True)
        for h in hubs.values():
            h.backfill(32)
        clock.set_paused(False)
        for h in hubs.values():
            h.flush(max_batches=100)
        for p in DEMO_PATIENTS:  # each home starts with a completed caregiver round
            engine.log_round(p.patient_id, p.contacts[0]["name"], "A")
        engine.reset_metrics()  # resilience counters should describe the live demo, not the backfill
        for h in hubs.values():
            h.start()
        if a.autopilot:
            now = clock.now()
            hubs["P-103"].phys.start_scenario("hf_decompensation", now - 6 * 3600)
            hubs["P-101"].phys.start_scenario("sepsis", now)
        print(f"\n  HomeWard is running:  {url}   (caregiver view: {url}/caregiver)\n")

    threading.Thread(target=start_hubs, daemon=True).start()
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
