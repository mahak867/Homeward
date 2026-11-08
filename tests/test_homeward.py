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

from homeward.core import device_key


def KEY(pid="P-101"):
    return {"X-Device-Key": device_key(f"HUB-{pid}")}


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def world():
    clock = SimClock(speed=1)
    clock.set_paused(True)
    chaos = Chaos()
    eng = Engine(":memory:", clock, chaos)
    hubs = {}
    app = create_app(eng, hubs, run_watchdog=False)
    client = TestClient(app)
    hubs.update({p.patient_id: EdgeHub(p, "http://testserver", clock, chaos, seed=i + 1, transport=client)
                 for i, p in enumerate(DEMO_PATIENTS)})
    assert client.post("/api/login", json={"user": "asha", "pin": "1234"}).status_code == 200  # care-desk session
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
    r = client.post("/api/ingest", headers=KEY("P-101"), json={"device_id": "HUB-P-102", "patient_id": "P-101", "readings": []})
    assert r.status_code == 403


def test_idempotent_ingest_under_duplicate_storm(world):
    clock, chaos, eng, client, hubs = world
    chaos.duplicate_storm = True
    count = "SELECT (SELECT COUNT(*) FROM readings) + (SELECT COUNT(*) FROM edge_events)"
    before = eng.db.execute(count).fetchone()[0]
    seq0 = sum(h.seq for h in hubs.values())
    run(world, 2)
    after = eng.db.execute(count).fetchone()[0]
    produced = sum(h.seq for h in hubs.values()) - seq0
    assert produced >= 72 and after - before == produced  # exactly one row per real reading/event
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


# ----------------------------------------------------------------- real hardware path
def test_real_sensor_feeds_pipeline_drives_buzzer_and_button_acks(world):
    clock, chaos, eng, client, hubs = world
    bad = {"hr": 128, "spo2": 88, "temp": 39.3, "finger": True, "sqi": 0.9, "firmware": "test"}
    for _ in range(4):
        r = client.post("/api/devices/P-101/sensor", json=bad, headers=KEY("P-101"))
        assert r.status_code == 200
        run(world, 1 / 12)
    last = eng.db.execute("SELECT hr, spo2, temp, source FROM readings WHERE patient_id='P-101' ORDER BY ts DESC LIMIT 1").fetchone()
    assert (last["hr"], last["spo2"], last["temp"], last["source"]) == (128, 88, 39.3, "sensor")
    assert eng.state["P-101"]["news2"]["total"] >= 7             # 3 (SpO2) + 2 (HR) + 2 (temp)
    cmd = client.get("/api/devices/P-101/command").json()
    assert cmd["alarm"] in ("fast", "slow") and cmd["led"] == "red"
    r = client.post("/api/devices/P-101/sensor", json={**bad, "button": True}, headers=KEY("P-101")).json()
    assert r["acknowledged"]                                     # bedside button acknowledged the alert
    assert all(a["status"] != "OPEN" for a in eng.alerts(0) if a["patient_id"] == "P-101" and a["id"] in r["acknowledged"])
    assert client.post("/api/devices/P-101/sensor", json=bad).status_code == 401


def test_finger_off_real_sensor_is_artifact_not_alarm(world):
    clock, chaos, eng, client, hubs = world
    client.post("/api/devices/P-102/sensor", json={"finger": False, "spo2": 70, "hr": 40}, headers=KEY("P-102"))
    run(world, 0.25)
    assert eng.db.execute("SELECT artifact FROM readings WHERE patient_id='P-102' ORDER BY ts DESC LIMIT 1").fetchone()[0] == 1
    assert not any(a["kind"] == "VITALS" and a["patient_id"] == "P-102" for a in eng.alerts(0))


# ----------------------------------------------------------------- home IoT: pill box + bed/motion
def test_fall_detected_by_bed_and_motion_sensors(world):
    clock, chaos, eng, client, hubs = world
    client.post("/api/sim", json={"patient_id": "P-103", "fall": True})
    run(world, 0.5)
    fall = [a for a in eng.alerts(0) if a["patient_id"] == "P-103" and a["kind"] == "FALL"]
    assert fall and fall[0]["severity"] == "HIGH"
    assert client.get("/api/state").json()["patients"][2]["tier"] in ("HIGH", "CRITICAL")
    client.post("/api/sim", json={"patient_id": "P-103", "back_to_bed": True})
    run(world, 0.25)
    assert not any(a["kind"] == "FALL" and a["status"] != "RESOLVED" for a in eng.alerts(0) if a["patient_id"] == "P-103")


def test_missed_dose_raises_medication_alert(world):
    clock, chaos, eng, client, hubs = world
    hubs["P-103"].p.adherence = 1.0
    client.post("/api/sim", json={"patient_id": "P-103", "skip_dose": True})
    run(world, 14)  # P-103 has doses at 08:00, 14:00 and 20:00 - at least one falls in any 14 h window
    meds = [a for a in eng.alerts(100) if a["patient_id"] == "P-103" and a["kind"] == "MEDICATION"]
    assert len(meds) == 1 and meds[0]["title"].startswith("Dose not taken")


# ----------------------------------------------------------------- equipment, power, risk level, attendance
def test_power_cut_keeps_monitoring_and_flags_oxygen_concentrator(world):
    clock, chaos, eng, client, hubs = world
    n0 = eng.db.execute("SELECT COUNT(*) FROM readings WHERE patient_id='P-102'").fetchone()[0]
    chaos.power_cut = True
    run(world, 1)
    assert eng.db.execute("SELECT COUNT(*) FROM readings WHERE patient_id='P-102'").fetchone()[0] - n0 >= 12  # still monitoring
    eq = [a for a in eng.alerts(0) if a["patient_id"] == "P-102" and a["kind"] == "EQUIPMENT"]
    assert eq and eq[0]["severity"] == "HIGH" and "power cut" in eq[0]["title"]
    assert not any(a["kind"] == "EQUIPMENT" for a in eng.alerts(0) if a["patient_id"] != "P-102")  # only O2 users
    chaos.power_cut = False
    run(world, 0.5)
    assert not any(a["kind"] == "EQUIPMENT" and a["status"] != "RESOLVED" for a in eng.alerts(0))


def test_concentrator_low_purity_alert(world):
    clock, chaos, eng, client, hubs = world
    client.post("/api/sim", json={"patient_id": "P-102", "o2_fault": True})
    run(world, 0.25)
    assert any(a["kind"] == "EQUIPMENT" and a["severity"] == "WATCH" for a in eng.alerts(0) if a["patient_id"] == "P-102")


def test_risk_level_low_medium_high_with_short_reason(world):
    clock, chaos, eng, client, hubs = world
    st = client.get("/api/state").json()["patients"][0]
    assert st["risk_level"] == "LOW" and st["risk_reason"].endswith(".")
    client.post("/api/patients/P-101/notes", json={"author": "Sunita", "text": "She is breathless and has a fever"})
    hubs["P-101"].phys.start_scenario("sepsis", clock.now() - 26 * 3600)
    run(world, 1)
    st = client.get("/api/state").json()["patients"][0]
    assert st["risk_level"] == "HIGH" and "NEWS2" in st["risk_reason"] and "caregiver notes mention" in st["risk_reason"]


def test_verified_attendance_by_bedside_tag(world):
    clock, chaos, eng, client, hubs = world
    client.post("/api/patients/P-101/checkin", json={"caregiver": "Sunita", "on": True, "method": "bedside-tag"})
    p = client.get("/api/state").json()["patients"][0]
    assert p["on_duty"]["verified"] is True
    assert client.post("/api/patients/P-101/checkin", json={"caregiver": "x", "on": True, "method": "fake"}).status_code == 422



# ----------------------------------------------------------------- privacy: role-based access
def _family(client, user, pin):
    tok = client.post("/api/login", json={"user": user, "pin": pin, "cookie": False}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_requires_login(world):
    clock, chaos, eng, client, hubs = world
    anon = TestClient(client.app)
    assert anon.get("/api/state").status_code == 401
    assert anon.get("/api/patients/P-101/history").status_code == 401
    assert anon.post("/api/alerts/1/ack", json={}).status_code == 401
    assert anon.get("/", follow_redirects=False).status_code in (302, 307)   # dashboard -> /login
    assert anon.get("/api/health").status_code == 200                        # health stays public


def test_family_sees_only_their_patient(world):
    clock, chaos, eng, client, hubs = world
    h = _family(client, "sunita", "1111")
    st = client.get("/api/state", headers=h).json()
    assert [p["id"] for p in st["patients"]] == ["P-101"] and st["user"]["role"] == "family"
    assert all(a["patient_id"] == "P-101" for a in st["alerts"])
    assert all(m["patient_id"] == "P-101" for m in st["messages"])
    assert client.get("/api/patients/P-102/history", headers=h).status_code == 403
    assert client.get("/api/patients/P-102/sbar", headers=h).status_code == 403
    assert client.post("/api/patients/P-102/notes", json={"text": "hi"}, headers=h).status_code == 403
    denied = eng.db.execute("SELECT what FROM audit WHERE what LIKE 'ACCESS DENIED%'").fetchall()
    assert denied and "Sunita" in denied[0][0]
    assert client.post("/api/chaos", json={"flag": "ml_down", "value": True}, headers=h).status_code == 403
    assert client.post("/api/sim", json={"speed": 60}, headers=h).status_code == 403


def test_actor_comes_from_session_not_request(world):
    clock, chaos, eng, client, hubs = world
    h = _family(client, "meena", "2222")
    client.post("/api/patients/P-102/notes", json={"author": "Dr. Fake", "text": "seene mein dard"}, headers=h)
    a = next(a for a in eng.alerts(0) if a["patient_id"] == "P-102" and a["kind"] == "NOTE")
    r = client.post(f"/api/alerts/{a['id']}/ack", json={"by": "Dr. Fake"}, headers=h).json()
    assert r["acked_by"] == "Meena (wife)"
    author = eng.db.execute("SELECT author FROM notes WHERE patient_id='P-102' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert author == "Meena (wife)"


def test_record_access_is_audited_and_lockout(world):
    clock, chaos, eng, client, hubs = world
    h = _family(client, "joseph", "3333")
    client.get("/api/patients/P-103/history", headers=h)
    client.get("/api/patients/P-103/history", headers=h)
    opened = eng.db.execute("SELECT COUNT(*) FROM audit WHERE what LIKE 'Home nurse Joseph opened%'").fetchone()[0]
    assert opened == 1                                       # once per session, not per refresh
    for _ in range(5):
        assert client.post("/api/login", json={"user": "meena", "pin": "0000"}).status_code == 401
    r = client.post("/api/login", json={"user": "meena", "pin": "2222"})
    assert r.status_code == 401 and "Too many attempts" in r.json()["detail"]   # locked even with right PIN


def test_logout_invalidates_session(world):
    clock, chaos, eng, client, hubs = world
    h = _family(client, "sunita", "1111")
    assert client.get("/api/me", headers=h).status_code == 200
    client.post("/api/logout", headers=h)
    assert client.get("/api/me", headers=h).status_code == 401



# ----------------------------------------------------------------- device identity + review fixes
def test_device_key_is_per_patient(world):
    clock, chaos, eng, client, hubs = world
    # Kamla's hub key cannot post for, or press the button of, Rajesh's home
    r = client.post("/api/devices/P-102/sensor", json={"finger": True, "button": True}, headers=KEY("P-101"))
    assert r.status_code == 401
    r = client.post("/api/ingest", headers=KEY("P-101"), json={"device_id": "HUB-P-102", "patient_id": "P-102", "readings": []})
    assert r.status_code == 401
    assert client.post("/api/devices/P-102/sensor", json={"finger": True}, headers=KEY("P-102")).status_code == 200


def test_device_command_requires_device_key_or_permitted_session(world):
    clock, chaos, eng, client, hubs = world
    anon = TestClient(client.app)
    assert anon.get("/api/devices/P-101/command").status_code == 401
    assert anon.get("/api/devices/P-101/command", headers=KEY("P-101")).status_code == 200
    h = _family(client, "meena", "2222")
    assert client.get("/api/devices/P-101/command", headers=h).status_code == 403
    assert client.get("/api/devices/P-102/command", headers=h).status_code == 200


def test_unknown_usernames_are_not_tracked(world):
    clock, chaos, eng, client, hubs = world
    auth = client.app.state.auth
    for i in range(50):
        client.post("/api/login", json={"user": f"ghost{i}", "pin": "0000"})
    assert not any(k.startswith("ghost") for k in auth.fails)



def test_acknowledged_alert_reopens_when_it_gets_worse(world):
    clock, chaos, eng, client, hubs = world
    client.post("/api/patients/P-102/notes", json={"text": "seene mein dard"})          # HIGH (chest pain)
    a = next(a for a in eng.alerts(0) if a["patient_id"] == "P-102" and a["kind"] == "NOTE")
    client.post(f"/api/alerts/{a['id']}/ack", json={})
    assert client.get("/api/devices/P-102/command", headers=KEY("P-102")).json()["alarm"] == "off"
    client.post("/api/patients/P-102/notes", json={"text": "he fainted, not responding"})  # now CRITICAL
    b = next(x for x in eng.alerts(0) if x["id"] == a["id"])
    assert b["severity"] == "CRITICAL" and b["status"] == "OPEN" and b["level"] == 1
    assert b["escalates_in_s"] is not None                                              # ladder restarted
    assert client.get("/api/devices/P-102/command", headers=KEY("P-102")).json()["alarm"] == "fast"
    audit = [r[0] for r in eng.db.execute("SELECT what FROM audit WHERE alert_id=?", (a["id"],)).fetchall()]
    assert any("RE-OPENED" in w for w in audit)
