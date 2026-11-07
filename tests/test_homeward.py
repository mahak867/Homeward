"""Automated checks for the safety and resilience claims made in the README.

    pytest -q
"""
import time

import pytest
from fastapi.testclient import TestClient

from homeward import news2, notes, quality
from homeward.core import Chaos, SimClock
from homeward.edge import EdgeHub
from homeward.engine import Engine
from homeward.physiology import DEMO_PATIENTS
from homeward.server import create_app

KEY = {"X-Device-Key": "demo-device-key-change-me"}


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def world():
    clock = SimClock(speed=1)
    clock.set_paused(True)
    chaos = Chaos()
    eng = Engine(":memory:", clock, chaos)
    app = create_app(eng, run_watchdog=False)
    client = TestClient(app)
    hubs = {p.patient_id: EdgeHub(p, "http://testserver", clock, chaos, seed=i + 1, transport=client)
            for i, p in enumerate(DEMO_PATIENTS)}
    for h in hubs.values():
        h.backfill(32)
        h.flush(100)
    for p in DEMO_PATIENTS:
        eng.log_round(p.patient_id, p.contacts[0]["name"], "A")
    eng.reset_metrics()
    return clock, chaos, eng, client, hubs


def run(world, hours, every=None):
    clock, chaos, eng, client, hubs = world
    for i in range(int(hours * 12)):
        clock.advance(300)
        for h in hubs.values():
            h.step()
        eng.tick()
        if every:
            every(i)


# ----------------------------------------------------------------- NEWS2
def test_news2_known_values():
    normal = {"rr": 16, "spo2": 97, "on_oxygen": False, "sbp": 125, "hr": 72, "acvpu": "A", "temp": 36.8}
    assert news2.score(normal)["total"] == 0
    sick = {"rr": 26, "spo2": 90, "on_oxygen": True, "sbp": 88, "hr": 135, "acvpu": "C", "temp": 39.5}
    s = news2.score(sick)
    assert s["total"] == 3 + 3 + 2 + 3 + 3 + 3 + 2 and s["band"] == "high"


def test_scale2_avoids_copd_false_alarm():
    copd = {"rr": 19, "spo2": 89, "on_oxygen": True, "sbp": 130, "hr": 85, "acvpu": "A", "temp": 36.8}
    assert news2.score(copd, 1)["total"] >= 5          # standard scale would call an urgent response
    assert news2.score(copd, 2)["total"] == 2          # prescribed target 88-92%: only the oxygen point


def test_single_red_parameter_band():
    r = {"rr": 16, "spo2": 97, "on_oxygen": False, "sbp": 89, "hr": 72, "acvpu": "A", "temp": 36.8}
    assert news2.score(r)["band"] == "low-medium"


# ----------------------------------------------------------------- notes NLP
@pytest.mark.parametrize("text,expect,negated", [
    ("Saans phool rahi hai, bukhar nahi hai", {"breathless"}, {"fever"}),
    ("She seems confused today and not eating", {"confusion", "intake"}, set()),
    ("No chest pain, feeling better", {"improving"}, {"chest_pain"}),
    ("सीने में दर्द हो रहा है", {"chest_pain"}, set()),
    ("papa gir gaye bathroom mein", {"fall"}, set()),
])
def test_notes_multilingual_with_negation(text, expect, negated):
    a = notes.analyse(text)
    assert {s["category"] for s in a["signals"]} == expect
    assert {s["category"] for s in a["negated"]} == negated


# ----------------------------------------------------------------- quality gate
def test_artifact_detection():
    good = {"hr": 80, "rr": 16, "spo2": 96, "sbp": 120, "temp": 36.8, "sqi": 0.95, "motion": 0.05}
    assert quality.assess(good)["ok"]
    assert quality.assess({**good, "sqi": 0.2})["artifact"]
    assert quality.assess({**good, "spo2": 85, "motion": 0.8}, prev_good=good)["artifact"]
    assert quality.assess({**good, "hr": 400})["kind"] == "invalid"


def test_displaced_sensor_never_raises_vitals_alarm(world):
    clock, chaos, eng, client, hubs = world
    hubs["P-103"].phys.inject_artifact(clock.now(), 25)
    run(world, 1)
    kinds = {(a["patient_id"], a["kind"]): a for a in eng.alerts(100)}
    assert ("P-103", "VITALS") not in kinds
    assert ("P-103", "SENSOR") in kinds              # ...but the caregiver is told to re-seat the probe
    assert kinds[("P-103", "SENSOR")]["status"] == "RESOLVED"  # and it clears itself once the probe is back
    assert eng.m["artifact_alarms_prevented"] >= 1


# ----------------------------------------------------------------- ingestion
def test_ingest_requires_device_key(world):
    client = world[3]
    r = client.post("/api/ingest", json={"device_id": "HUB-P-101", "patient_id": "P-101", "readings": []})
    assert r.status_code == 401
    r = client.post("/api/ingest", headers=KEY, json={"device_id": "HUB-P-102", "patient_id": "P-101", "readings": []})
    assert r.status_code == 403


def test_idempotent_ingest_under_duplicate_storm(world):
    clock, chaos, eng, client, hubs = world
    chaos.duplicate_storm = True
    before = eng.db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    seq0 = sum(h.seq for h in hubs.values())
    run(world, 2)
    after = eng.db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    produced = sum(h.seq for h in hubs.values()) - seq0
    assert produced >= 72 and after - before == produced  # exactly one row per real reading
    assert eng.m["duplicates"] >= produced


def test_out_of_order_delivery_reconciled(world):
    clock, chaos, eng, client, hubs = world
    chaos.out_of_order = True
    run(world, 3)
    chaos.out_of_order = False
    run(world, 0.5)
    seqs = [r[0] for r in eng.db.execute("SELECT seq FROM readings WHERE patient_id='P-101' ORDER BY ts").fetchall()]
    assert seqs == sorted(seqs)                      # stored history is in measurement order
    assert eng.m["late"] > 0                         # late arrivals were seen and accepted


# ----------------------------------------------------------------- outage + edge
def test_outage_buffers_alarms_locally_and_replays(world):
    clock, chaos, eng, client, hubs = world
    hubs["P-101"].phys.start_scenario("sepsis", clock.now() - 22 * 3600)  # already deteriorating
    chaos.network_down = True
    run(world, 4)
    st = eng.state["P-101"]
    assert st["data_state"] == "OFFLINE"             # never shown as stable on old data
    assert hubs["P-101"].pending() >= 40             # hub kept recording
    assert hubs["P-101"].stats["local_alarms"] == 1  # in-room alarm, latched (no repeats)
    chaos.network_down = False
    for h in hubs.values():
        h.next_try = 0
    run(world, 0.25)
    assert hubs["P-101"].pending() == 0
    assert eng.state["P-101"]["data_state"] == "LIVE"
    kinds = [a["kind"] for a in eng.alerts(0) if a["patient_id"] == "P-101"]
    assert "EDGE" in kinds and "VITALS" in kinds
    # device-offline alert auto-resolves once data resumes
    assert not any(a["kind"] == "DEVICE" and a["status"] != "RESOLVED" for a in eng.alerts(0) if a["patient_id"] == "P-101")


def test_dead_hub_goes_unknown_then_alerts(world):
    clock, chaos, eng, client, hubs = world
    chaos.dead_devices.add("P-102")
    run(world, 1)
    assert eng.state["P-102"]["data_state"] == "OFFLINE"
    assert any(a["kind"] == "DEVICE" and a["patient_id"] == "P-102" for a in eng.alerts(0))


# ----------------------------------------------------------------- AI + fallback
def test_ml_circuit_breaker_falls_back_and_recovers(world):
    clock, chaos, eng, client, hubs = world
    chaos.ml_down = True
    run(world, 0.5)
    assert eng.breaker.state in ("open", "half-open")
    assert eng.state["P-101"]["forecast"]["source"] == "fallback-rules"
    chaos.ml_down = False
    eng.breaker.cooldown_s = 0
    run(world, 0.25)
    assert eng.breaker.state == "closed"
    assert eng.state["P-101"]["forecast"]["source"] == "ml"


def test_early_warning_precedes_urgent_news2(world):
    clock, chaos, eng, client, hubs = world
    hubs["P-101"].phys.start_scenario("sepsis", clock.now())
    first = {}

    def watch(i):
        for a in eng.alerts(0):
            first.setdefault((a["patient_id"], a["kind"], a["severity"]), i)
    run(world, 30, every=watch)
    ew = first.get(("P-101", "EARLY_WARNING", "WATCH"))
    urgent = min(v for (p, k, s), v in first.items() if p == "P-101" and k == "VITALS" and s in ("HIGH", "CRITICAL"))
    assert ew is not None and ew < urgent
    assert (urgent - ew) * 5 / 60 >= 3               # at least 3 h of warning in this scenario
    # stable patient (P-102 COPD) must not get a vitals alarm thanks to Scale 2
    assert not any(p == "P-102" and k == "VITALS" for (p, k, s) in first)


def test_explanations_are_present_and_plain(world):
    clock, chaos, eng, client, hubs = world
    hubs["P-103"].phys.start_scenario("hf_decompensation", clock.now() - 20 * 3600)
    run(world, 2)
    fc = eng.state["P-103"]["forecast"]
    assert fc["factors"] and all(isinstance(f["text"], str) and len(f["text"]) > 10 for f in fc["factors"])
    sbar = client.get("/api/patients/P-103/sbar").text
    assert sbar.startswith("S - Situation") and "R - Recommendation" in sbar and "not a diagnosis" in sbar


# ----------------------------------------------------------------- escalation + response
def test_escalation_ladder_and_acknowledgement(world, monkeypatch):
    clock, chaos, eng, client, hubs = world
    from homeward import engine as engmod
    monkeypatch.setitem(engmod.ESCALATE_AFTER_S, "HIGH", 0.05)
    r = client.post("/api/patients/P-102/notes", json={"author": "Meena", "text": "seene mein dard ho raha hai"})
    assert r.json()["red_flags"] == ["Chest pain"]
    a = next(a for a in eng.alerts(0) if a["patient_id"] == "P-102" and a["kind"] == "NOTE")
    assert a["severity"] == "HIGH" and a["level"] == 1
    for _ in range(3):
        time.sleep(0.06); eng.tick()
    a = next(x for x in eng.alerts(0) if x["id"] == a["id"])
    assert a["level"] == 3                           # caregiver -> family -> care desk
    msgs = eng.db.execute("SELECT level FROM messages WHERE alert_id=? ORDER BY id", (a["id"],)).fetchall()
    assert [m[0] for m in msgs] == [1, 2, 3]
    r = client.post(f"/api/alerts/{a['id']}/ack", json={"by": "Nurse Asha"}).json()
    assert r["status"] == "ACKNOWLEDGED"
    r = client.post(f"/api/alerts/{a['id']}/resolve", json={"by": "Nurse Asha", "action": "Called doctor / tele-consult", "note": "ECG arranged"}).json()
    assert r["status"] == "RESOLVED"
    audit = [x[0] for x in eng.db.execute("SELECT what FROM audit WHERE alert_id=?", (a["id"],)).fetchall()]
    assert any("escalated to level 2" in w for w in audit) and any("resolved by Nurse Asha" in w for w in audit)


def test_new_confusion_in_note_counts_in_news2(world):
    clock, chaos, eng, client, hubs = world
    before = eng.state["P-101"]["news2"]["total"]
    client.post("/api/patients/P-101/notes", json={"author": "Sunita", "text": "Maa ji aaj confused lag rahi hain"})
    st = eng.state["P-101"]
    assert st["latest"]["acvpu"] == "C" and st["news2"]["components"]["acvpu"] == 3
    assert st["news2"]["total"] >= before + 3


def test_missed_round_raises_caregiver_alert(world):
    clock, chaos, eng, client, hubs = world
    run(world, 7)
    assert any(a["kind"] == "CAREGIVER" for a in eng.alerts(0))
    client.post("/api/patients/P-101/rounds", json={"caregiver": "Sunita", "acvpu": "A"})
    assert not any(a["kind"] == "CAREGIVER" and a["patient_id"] == "P-101" and a["status"] != "RESOLVED" for a in eng.alerts(0))
