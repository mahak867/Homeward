"""HomeWard monitoring platform API (FastAPI) + static dashboard."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import asyncio
import json

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import DEVICE_KEY, SEV_RANK
from .engine import Engine
from .physiology import SCENARIOS

WEB = Path(__file__).resolve().parent.parent / "web"


class Reading(BaseModel):
    seq: int
    ts: float
    hr: float
    rr: float
    spo2: float
    sbp: float
    dbp: float
    temp: float
    weight: float | None = None
    sqi: float = 1.0
    motion: float = 0.0
    on_oxygen: bool = False
    acvpu: str | None = None
    source: str = "sim"


class Batch(BaseModel):
    device_id: str
    patient_id: str
    readings: list[Reading] = Field(default_factory=list, max_length=2000)
    events: list[dict] = Field(default_factory=list)
    battery: float | None = None
    buffered: int = 0
    offline_seconds: float = 0
    equipment: dict | None = None


class Ack(BaseModel):
    by: str = Field(min_length=1, max_length=80)


class Resolve(BaseModel):
    by: str = Field(min_length=1, max_length=80)
    action: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)


class NoteIn(BaseModel):
    author: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=1000)


class RoundIn(BaseModel):
    caregiver: str = Field(min_length=1, max_length=80)
    acvpu: str = Field(default="A", pattern="^[ACVPU]$")
    note: str = Field(default="", max_length=1000)


class ChaosIn(BaseModel):
    flag: str
    value: bool
    patient_id: str | None = None


class SimIn(BaseModel):
    o2_fault: bool | None = None
    fall: bool = False
    skip_dose: bool = False
    back_to_bed: bool = False
    speed: float | None = None
    paused: bool | None = None
    patient_id: str | None = None
    scenario: str | None = None
    artifact: bool = False
    reset: bool = False


class SensorIn(BaseModel):
    """What a real bedside board measures (ESP32 + MAX30102 + temperature probe)."""
    hr: float | None = Field(default=None, ge=20, le=250)
    spo2: float | None = Field(default=None, ge=50, le=100)
    temp: float | None = Field(default=None, ge=30, le=43)
    finger: bool = True
    sqi: float | None = Field(default=None, ge=0, le=1)
    button: bool = False
    firmware: str | None = None


class CheckIn(BaseModel):
    caregiver: str
    on: bool
    method: str = Field(default="app", pattern="^(app|bedside-tag)$")


def create_app(engine: Engine, hubs: dict | None = None, run_watchdog: bool = True) -> FastAPI:
    app = FastAPI(title="Smart Health HomeWard", version="1.0",
                  description="HomeWard home-ward monitoring platform API (prototype decision support).")
    hubs = hubs if hubs is not None else {}
    app.state.engine, app.state.hubs = engine, hubs

    if run_watchdog:
        def loop():
            while True:
                try:
                    engine.tick()
                except Exception as e:
                    print("watchdog error:", e)
                time.sleep(1.0)
        threading.Thread(target=loop, daemon=True, name="watchdog").start()

    # ------------------------------------------------------------ device API
    @app.post("/api/ingest")
    def ingest(batch: Batch, x_device_key: str = Header(default="")):
        if x_device_key != DEVICE_KEY:
            raise HTTPException(401, "invalid device key")
        if batch.patient_id not in engine.patients or batch.device_id != f"HUB-{batch.patient_id}":
            raise HTTPException(403, "device not registered to this patient")
        return engine.ingest(batch.model_dump())

    # ------------------------------------------------------------ real hardware
    @app.post("/api/devices/{pid}/sensor")
    def sensor(pid: str, body: SensorIn, x_device_key: str = Header(default="")):
        """Real sensors at the bedside push here every few seconds; values flow into the next
        reading of that home's hub. A press of the bedside button acknowledges open alerts."""
        if x_device_key != DEVICE_KEY:
            raise HTTPException(401, "invalid device key")
        if pid not in engine.patients:
            raise HTTPException(404)
        hub = hubs.get(pid)
        if hub:
            hub.hw_update(body.model_dump())
        acked = []
        if body.button:
            for a in engine.alerts(0):
                if a["patient_id"] == pid and a["status"] == "OPEN":
                    engine.ack(a["id"], "Bedside button")
                    acked.append(a["id"])
            if pid in engine.on_duty:
                engine.on_duty[pid].update({"verified": True, "last_seen": engine.clock.now()})
        return {"ok": True, "acknowledged": acked, **command(pid)}

    @app.get("/api/devices/{pid}/command")
    def command(pid: str):
        """What the bedside buzzer / LED should do right now."""
        if pid not in engine.patients:
            raise HTTPException(404)
        with engine.lock:
            st = engine.state[pid]
            open_ = [a for a in engine.alerts(0) if a["patient_id"] == pid and a["status"] == "OPEN"]
            sev = max((a["severity"] for a in open_), key=lambda x: SEV_RANK[x], default=None)
            pattern = {"CRITICAL": "fast", "HIGH": "slow", "WATCH": "chirp"}.get(sev, "off")
            return {"tier": st.get("tier", "UNKNOWN"), "alarm": pattern, "open_alerts": len(open_),
                    "led": {"CRITICAL": "red", "HIGH": "red", "WATCH": "amber"}.get(sev, "green")}

    # ------------------------------------------------------------ dashboard API
    @app.get("/api/state")
    def state():
        return build_state()

    @app.get("/api/stream")
    async def stream(request: Request):
        """Server-Sent Events: the dashboard gets a push every second (falls back to polling)."""
        async def gen():
            while not await request.is_disconnected():
                yield f"data: {json.dumps(build_state())}\n\n"
                await asyncio.sleep(1.0)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def build_state():
        with engine.lock:
            pats = []
            open_alerts = engine.alerts(0)
            for pid, p in engine.patients.items():
                st = engine.state[pid]
                # home events (fall, red-flag note, missed dose) also lift the patient's displayed status
                tier, why = st.get("tier", "UNKNOWN"), list(st.get("why", []))
                reason = st.get("risk_reason")
                rank = {"UNKNOWN": -1, "STABLE": 0, "WATCH": 1, "HIGH": 2, "CRITICAL": 3}
                for a in open_alerts:
                    if a["patient_id"] == pid and a["kind"] in ("FALL", "NOTE", "MEDICATION", "EDGE", "EQUIPMENT"):
                        if rank.get(a["severity"], 0) > rank.get(tier, 0) and tier != "UNKNOWN":
                            tier = a["severity"]
                            reason = f"{a['title']}. " + (reason or "")
                        why.insert(0, a["title"])
                pats.append({
                    "id": pid, "name": p.name, "age": p.age, "sex": p.sex, "condition": p.condition,
                    "on_oxygen": p.on_oxygen, "spo2_scale": p.spo2_scale, "contacts": p.contacts,
                    "tier": tier, "data_state": st.get("data_state", "NO_DATA"),
                    "risk_level": {"STABLE": "LOW", "WATCH": "MEDIUM", "HIGH": "HIGH", "CRITICAL": "HIGH"}.get(tier),
                    "risk_reason": reason, "equipment": st.get("equipment"),
                    "data_age_min": st.get("data_age_min"), "why": why,
                    "latest": st.get("latest"), "news2": st.get("news2"), "news2_rows": st.get("news2_rows"),
                    "forecast": st.get("forecast"), "baseline": st.get("baseline"), "notes": st.get("notes"),
                    "quality": st.get("quality"), "acvpu_source": st.get("acvpu_source"),
                    "scale1_would_alarm": st.get("scale1_would_alarm"), "device": st.get("device"),
                    "round_due": st.get("round_due"), "on_duty": engine.on_duty.get(pid),
                    "bed": st.get("bed"), "last_dose": st.get("last_dose"), "next_dose": st.get("next_dose"),
                    "dose_missed": st.get("dose_missed"), "schedule": p.schedule, "meds": p.meds,
                    "hub": hubs[pid].status() if pid in hubs else None,
                    "spark": [x["prob"] for x in engine.trail[pid][-288::4] if x["prob"] is not None],
                })
            msgs = [dict(r) for r in engine.db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 30").fetchall()]
            audit = [dict(r) for r in engine.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 40").fetchall()]
            return {
                "sim_time": engine.clock.now(), "speed": engine.clock.speed, "paused": engine.clock.paused,
                "patients": pats, "alerts": engine.alerts(), "messages": msgs, "audit": audit,
                "metrics": engine.m | {"ml_breaker": engine.breaker.state, "ml_fallbacks": engine.breaker.fallback_count,
                                       "ml_failures": engine.breaker.total_failures},
                "responses": engine.response_stats(), "chaos": engine.chaos.as_dict(),
                "model": {k: engine.predictor.meta.get(k) for k in ("metrics", "threshold", "trained", "caveat")} if engine.predictor else None,
                "scenarios": {k: v.label for k, v in SCENARIOS.items()},
            }

    @app.get("/api/patients/{pid}/history")
    def history(pid: str, hours: float = 48):
        if pid not in engine.patients:
            raise HTTPException(404)
        with engine.lock:
            h = engine.history(pid, min(hours, 96))
            h["notes"] = [dict(r) for r in engine.db.execute("SELECT ts, author, text, analysis FROM notes WHERE patient_id=? ORDER BY ts DESC LIMIT 20", (pid,)).fetchall()]
            h["rounds"] = [dict(r) for r in engine.db.execute("SELECT ts, caregiver, acvpu, note FROM rounds WHERE patient_id=? ORDER BY ts DESC LIMIT 20", (pid,)).fetchall()]
            h["audit"] = [dict(r) for r in engine.db.execute("SELECT ts, actor, what, alert_id FROM audit WHERE patient_id=? ORDER BY id DESC LIMIT 80", (pid,)).fetchall()]
            h["events"] = [dict(r) for r in engine.db.execute("SELECT ts, kind, detail FROM edge_events WHERE patient_id=? ORDER BY ts DESC LIMIT 60", (pid,)).fetchall()]
            return h

    @app.get("/api/patients/{pid}/sbar", response_class=PlainTextResponse)
    def sbar(pid: str):
        if pid not in engine.patients:
            raise HTTPException(404)
        with engine.lock:
            return engine.sbar(pid)

    @app.post("/api/alerts/{aid}/ack")
    def ack(aid: int, body: Ack):
        try:
            return engine.ack(aid, body.by)
        except KeyError:
            raise HTTPException(404)

    @app.post("/api/alerts/{aid}/resolve")
    def resolve(aid: int, body: Resolve):
        try:
            return engine.resolve(aid, body.by, body.action, body.note)
        except KeyError:
            raise HTTPException(404)

    @app.post("/api/patients/{pid}/notes")
    def note(pid: str, body: NoteIn):
        if pid not in engine.patients:
            raise HTTPException(404)
        return engine.add_note(pid, body.author, body.text)

    @app.post("/api/patients/{pid}/rounds")
    def round_(pid: str, body: RoundIn):
        if pid not in engine.patients:
            raise HTTPException(404)
        engine.log_round(pid, body.caregiver, body.acvpu, body.note)
        return {"ok": True}

    @app.post("/api/patients/{pid}/checkin")
    def checkin(pid: str, body: CheckIn):
        if pid not in engine.patients:
            raise HTTPException(404)
        engine.check_in(pid, body.caregiver, body.on, body.method)
        return {"ok": True}

    # ------------------------------------------------------------ demo / resilience lab
    @app.post("/api/chaos")
    def chaos(body: ChaosIn):
        c = engine.chaos
        if body.flag == "device_dead":
            if not body.patient_id:
                raise HTTPException(400, "patient_id required")
            (c.dead_devices.add if body.value else c.dead_devices.discard)(body.patient_id)
        elif body.flag in c.FLAGS:
            setattr(c, body.flag, body.value)
        else:
            raise HTTPException(400, "unknown flag")
        with engine.lock:
            engine._audit(body.patient_id, f"Resilience lab: {body.flag} = {body.value}", "demo")
            engine.db.commit()
        return c.as_dict()

    @app.post("/api/sim")
    def sim(body: SimIn):
        if body.speed is not None:
            engine.clock.set_speed(body.speed)
        if body.paused is not None:
            engine.clock.set_paused(body.paused)
        if body.patient_id:
            hub = hubs.get(body.patient_id)
            if not hub:
                raise HTTPException(404, "no simulated hub for patient")
            now = engine.clock.now()
            if body.scenario:
                if body.scenario not in SCENARIOS:
                    raise HTTPException(400, "unknown scenario")
                hub.phys.start_scenario(body.scenario, now)
            if body.artifact:
                hub.phys.inject_artifact(now, 20)
            if body.reset:
                hub.phys.clear_scenario()
            if body.fall:
                hub.simulate_fall()
            if body.back_to_bed:
                hub.help_back_to_bed()
            if body.skip_dose:
                hub.skip_next_dose = True
            if body.o2_fault is not None:
                hub.o2_fault = body.o2_fault
            if body.reset:
                hub.o2_fault = False
            with engine.lock:
                engine._audit(body.patient_id, f"Simulator: scenario={body.scenario} artifact={body.artifact} reset={body.reset}", "demo")
                engine.db.commit()
        return {"speed": engine.clock.speed, "paused": engine.clock.paused}

    @app.get("/api/health")
    def health():
        return {"ok": True, "ml": engine.breaker.state, "model_loaded": engine.predictor is not None,
                "patients": len(engine.patients)}

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/caregiver")
    def caregiver():
        return FileResponse(WEB / "caregiver.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
