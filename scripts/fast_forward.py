"""Fast-forward the full pipeline in-process (no network) and print an alert timeline.

    python scripts/fast_forward.py [hours]

Useful to verify the story before recording the demo video.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient

from homeward.core import Chaos, SimClock
from homeward.edge import EdgeHub
from homeward.engine import Engine
from homeward.physiology import DEMO_PATIENTS
from homeward.server import create_app

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 40
clock, chaos = SimClock(speed=1), Chaos()
clock.set_paused(True)
eng = Engine(":memory:", clock, chaos)
app = create_app(eng, run_watchdog=False)
client = TestClient(app)
hubs = {p.patient_id: EdgeHub(p, "http://testserver", clock, chaos, seed=i + 1, transport=client)
        for i, p in enumerate(DEMO_PATIENTS)}
for h in hubs.values():
    h.backfill(32); h.flush(100)
for p in DEMO_PATIENTS:
    eng.log_round(p.patient_id, p.contacts[0]["name"], "A")
eng.reset_metrics()
t0 = clock.now()
hubs["P-101"].phys.start_scenario("sepsis", t0)
hubs["P-103"].phys.start_scenario("hf_decompensation", t0)
seen = set()
last_tier = {}
for step in range(int(HOURS * 12)):
    clock.advance(300)
    for h in hubs.values():
        h.step()
    eng.tick()
    hr = (clock.now() - t0) / 3600
    for pid, st in eng.state.items():
        if st.get("tier") != last_tier.get(pid):
            fc = st.get("forecast", {})
            print(f"+{hr:5.1f}h {pid} tier {last_tier.get(pid)} -> {st.get('tier')}  NEWS2={st.get('news2', {}).get('total')} p24={fc.get('prob')}")
            last_tier[pid] = st.get("tier")
    for a in eng.alerts(0):
        if a["id"] not in seen:
            seen.add(a["id"])
            print(f"+{hr:5.1f}h   ALERT #{a['id']} {a['patient_id']} [{a['severity']}] {a['title']}")
print("metrics:", eng.m)
print(eng.sbar("P-101"))
