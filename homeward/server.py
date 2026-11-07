"""HomeWard monitoring platform API (FastAPI) + static dashboard."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import DEVICE_KEY
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


class Batch(BaseModel):
    device_id: str
    patient_id: str
    readings: list[Reading] = Field(default_factory=list, max_length=2000)
    events: list[dict] = Field(default_factory=list)
    battery: float | None = None
    buffered: int = 0
    offline_seconds: float = 0


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
    speed: float | None = None
    paused: bool | None = None
    patient_id: str | None = None
    scenario: str | None = None
    artifact: bool = False
    reset: bool = False


class CheckIn(BaseModel):
    caregiver: str
    on: bool


def create_app(engine: Engine, hubs: dict | None = None, run_watchdog: bool = True) -> FastAPI:
    app = FastAPI(title="Smart Health HomeWard", version="1.0",
                  description="Prototype home-ward monitoring platform. Synthetic data only - not a medical device.")
    hubs = hubs or {}
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

    # ------------------------------------------------------------ dashboard API
    @app.get("/api/state")
    def state():
        with engine.lock:
            pats = []
            for pid, p in engine.patients.items():
                st = engine.state[pid]
                pats.append({
                    "id": pid, "name": p.name, "age": p.age, "sex": p.sex, "condition": p.condition,
                    "on_oxygen": p.on_oxygen, "spo2_scale": p.spo2_scale, "contacts": p.contacts,
                    "tier": st.get("tier", "UNKNOWN"), "data_state": st.get("data_state", "NO_DATA"),
                    "data_age_min": st.get("data_age_min"), "why": st.get("why", []),
                    "latest": st.get("latest"), "news2": st.get("news2"), "news2_rows": st.get("news2_rows"),
                    "forecast": st.get("forecast"), "baseline": st.get("baseline"), "notes": st.get("notes"),
                    "quality": st.get("quality"), "acvpu_source": st.get("acvpu_source"),
                    "scale1_would_alarm": st.get("scale1_would_alarm"), "device": st.get("device"),
                    "round_due": st.get("round_due"), "on_duty": engine.on_duty.get(pid),
                    "hub": hubs[pid].status() if pid in hubs else None,
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
        engine.check_in(pid, body.caregiver, body.on)
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
