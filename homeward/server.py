"""HomeWard monitoring platform API (FastAPI) + static dashboard."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import asyncio
import hmac
import json
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import Auth
from .core import SEV_RANK, device_key
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
    events: list[dict] = Field(default_factory=list, max_length=2000)
    battery: float | None = None
    buffered: int = 0
    offline_seconds: float = 0
    equipment: dict | None = None


class LoginIn(BaseModel):
    user: str = Field(min_length=1, max_length=40)
    pin: str = Field(min_length=4, max_length=12)
    cookie: bool = True  # phone app keeps a bearer token instead, so it can't clobber a desk session


class Ack(BaseModel):
    by: str = ""  # ignored: the actor is always the logged-in user


class Resolve(BaseModel):
    by: str = ""
    action: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)


class NoteIn(BaseModel):
    author: str = ""
    text: str = Field(min_length=1, max_length=1000)


class RoundIn(BaseModel):
    caregiver: str = ""
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
    speed: float | None = Field(default=None, ge=1, le=3600)  # bounded: huge speeds would flood the hubs
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
    caregiver: str = ""
    on: bool
    method: str = Field(default="app", pattern="^(app|bedside-tag)$")


def create_app(engine: Engine, hubs: dict | None = None, run_watchdog: bool = True) -> FastAPI:
    app = FastAPI(title="Smart Health HomeWard", version="1.0",
                  description="HomeWard home-ward monitoring platform API (prototype decision support).")
    hubs = hubs if hubs is not None else {}

    def device_ok(key: str, pid: str) -> bool:
        """The key must be the one issued to THIS patient's hub (HMAC of the device id)."""
        return hmac.compare_digest(key.encode(), device_key(f"HUB-{pid}").encode())
    auth = Auth()
    app.state.engine, app.state.hubs, app.state.auth = engine, hubs, auth

    def token_of(request: Request) -> str | None:
        h = request.headers.get("authorization", "")
        return h[7:] if h.lower().startswith("bearer ") else request.cookies.get("hw_session")

    def user_dep(request: Request) -> dict:
        u = auth.resolve(token_of(request))
        if not u:
            raise HTTPException(401, "login required")
        return u

    def desk_dep(user: dict = Depends(user_dep)) -> dict:
        if user["role"] != "desk":
            raise HTTPException(403, "care-desk only")
        return user

    def patient_guard(user: dict, pid: str) -> None:
        if pid not in engine.patients:
            raise HTTPException(404)
        if not auth.can_see(user, pid):
            with engine.lock:
                engine._audit(pid, f"ACCESS DENIED: {user['name']} tried to open this record", user["name"])
                engine.db.commit()
            raise HTTPException(403, "not your patient")

    # ------------------------------------------------------------ sign-in
    @app.get("/api/users")
    def users():
        return auth.directory()

    @app.post("/api/login")
    def login(body: LoginIn, response: Response):
        token, msg = auth.login(body.user, body.pin)
        if not token:
            raise HTTPException(401, msg)
        u = auth.resolve(token)
        if body.cookie:
            response.set_cookie("hw_session", token, httponly=True, samesite="strict", max_age=12 * 3600)
        with engine.lock:
            engine._audit(None, f"{u['name']} signed in ({u['role']})", u["name"])
            engine.db.commit()
        return {"token": token, "user": {k: u[k] for k in ("id", "name", "role", "patients")}}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        auth.logout(token_of(request))
        response.delete_cookie("hw_session")
        return {"ok": True}

    @app.get("/api/me")
    def me(user: dict = Depends(user_dep)):
        return {k: user[k] for k in ("id", "name", "role", "patients")}

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
        if batch.patient_id not in engine.patients or batch.device_id != f"HUB-{batch.patient_id}":
            raise HTTPException(403, "device not registered to this patient")
        if not device_ok(x_device_key, batch.patient_id):
            raise HTTPException(401, "invalid device key")
        return engine.ingest(batch.model_dump())

    # ------------------------------------------------------------ real hardware
    @app.post("/api/devices/{pid}/sensor")
    def sensor(pid: str, body: SensorIn, x_device_key: str = Header(default="")):
        """Real sensors at the bedside push here every few seconds; values flow into the next
        reading of that home's hub. A press of the bedside button acknowledges open alerts."""
        if pid not in engine.patients:
            raise HTTPException(404)
        if not device_ok(x_device_key, pid):
            raise HTTPException(401, "invalid device key")
        hub = hubs.get(pid)
        if hub:
            hub.hw_update(body.model_dump())
        acked = []
        if body.button:
            with engine.lock:
                for a in engine.alerts(0):
                    if a["patient_id"] == pid and a["status"] == "OPEN":
                        engine.ack(a["id"], "Bedside button")
                        acked.append(a["id"])
                if pid in engine.on_duty:
                    engine.on_duty[pid].update({"verified": True, "last_seen": engine.clock.now()})
        return {"ok": True, "acknowledged": acked, **alarm_state(pid)}

    @app.get("/api/devices/{pid}/command")
    def command(pid: str, request: Request, x_device_key: str = Header(default="")):
        """What the bedside buzzer / LED should do right now (device key, or a session allowed to see the patient)."""
        if pid not in engine.patients:
            raise HTTPException(404)
        if not device_ok(x_device_key, pid):
            u = auth.resolve(token_of(request))
            if not u:
                raise HTTPException(401, "device key or login required")
            patient_guard(u, pid)
        return alarm_state(pid)

    def alarm_state(pid: str) -> dict:
        with engine.lock:
            st = engine.state[pid]
            open_ = [a for a in engine.alerts(0) if a["patient_id"] == pid and a["status"] == "OPEN"]
            sev = max((a["severity"] for a in open_), key=lambda x: SEV_RANK[x], default=None)
            pattern = {"CRITICAL": "fast", "HIGH": "slow", "WATCH": "chirp"}.get(sev, "off")
            return {"tier": st.get("tier", "UNKNOWN"), "alarm": pattern, "open_alerts": len(open_),
                    "led": {"CRITICAL": "red", "HIGH": "red", "WATCH": "amber"}.get(sev, "green")}

    # ------------------------------------------------------------ dashboard API
    @app.get("/api/state")
    def state(user: dict = Depends(user_dep)):
        return build_state(user)

    @app.get("/api/stream")
    async def stream(request: Request, user: dict = Depends(user_dep)):
        """Server-Sent Events: the dashboard gets a push every second (falls back to polling)."""
        tok = token_of(request)

        async def gen():
            while not await request.is_disconnected() and auth.resolve(tok):
                state = await asyncio.to_thread(build_state, user)  # keep the event loop free
                yield f"data: {json.dumps(state)}\n\n"
                await asyncio.sleep(1.0)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def build_state(user: dict):
        """Everything the UI needs - filtered to the patients this user is allowed to see."""
        desk = user["role"] == "desk"
        allowed = [pid for pid in engine.patients if auth.can_see(user, pid)] or ["-"]  # "-" keeps SQL valid
        with engine.lock:
            pats = []
            all_alerts = engine.alerts()
            open_alerts = [a for a in all_alerts if a["status"] != "RESOLVED"]
            for pid, p in engine.patients.items():
                if pid not in allowed:
                    continue
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
            q = ",".join("?" * len(allowed))
            msgs = [dict(r) for r in engine.db.execute(f"SELECT * FROM messages WHERE patient_id IN ({q}) ORDER BY id DESC LIMIT 30", allowed).fetchall()]
            audit = [dict(r) for r in engine.db.execute(
                f"SELECT * FROM audit WHERE patient_id IN ({q})" + (" OR patient_id IS NULL" if desk else "") + " ORDER BY id DESC LIMIT 40", allowed).fetchall()]
            return {
                "user": {k: user[k] for k in ("id", "name", "role", "patients")},
                "sim_time": engine.clock.now(), "speed": engine.clock.speed, "paused": engine.clock.paused,
                "patients": pats, "alerts": [a for a in all_alerts if a["patient_id"] in allowed],
                "messages": msgs, "audit": audit,
                "metrics": engine.m | {"ml_breaker": engine.breaker.state, "ml_fallbacks": engine.breaker.fallback_count,
                                       "ml_failures": engine.breaker.total_failures},
                "responses": engine.response_stats(), "chaos": engine.chaos.as_dict(),
                "model": {k: engine.predictor.meta.get(k) for k in ("metrics", "threshold", "trained", "caveat")} if engine.predictor else None,
                "scenarios": {k: v.label for k, v in SCENARIOS.items()},
            }

    @app.get("/api/patients/{pid}/history")
    def history(pid: str, hours: float = 48, user: dict = Depends(user_dep)):
        patient_guard(user, pid)
        with engine.lock:
            seen = user["_session"]["viewed"]
            if pid not in seen:  # record access once per session
                seen.add(pid)
                engine._audit(pid, f"{user['name']} opened {engine.patients[pid].name}'s record", user["name"])
                engine.db.commit()
            h = engine.history(pid, min(hours, 96))
            h["notes"] = [dict(r) for r in engine.db.execute("SELECT ts, author, text, analysis FROM notes WHERE patient_id=? ORDER BY ts DESC LIMIT 20", (pid,)).fetchall()]
            h["rounds"] = [dict(r) for r in engine.db.execute("SELECT ts, caregiver, acvpu, note FROM rounds WHERE patient_id=? ORDER BY ts DESC LIMIT 20", (pid,)).fetchall()]
            h["audit"] = [dict(r) for r in engine.db.execute("SELECT ts, actor, what, alert_id FROM audit WHERE patient_id=? ORDER BY id DESC LIMIT 80", (pid,)).fetchall()]
            h["events"] = [dict(r) for r in engine.db.execute("SELECT ts, kind, detail FROM edge_events WHERE patient_id=? ORDER BY ts DESC LIMIT 60", (pid,)).fetchall()]
            return h

    @app.get("/api/patients/{pid}/sbar", response_class=PlainTextResponse)
    def sbar(pid: str, user: dict = Depends(user_dep)):
        patient_guard(user, pid)
        with engine.lock:
            return engine.sbar(pid)

    def alert_guard(user, aid):
        with engine.lock:
            row = engine.db.execute("SELECT patient_id FROM alerts WHERE id=?", (aid,)).fetchone()
        if not row:
            raise HTTPException(404)
        patient_guard(user, row[0])

    @app.post("/api/alerts/{aid}/ack")
    def ack(aid: int, body: Ack, user: dict = Depends(user_dep)):
        alert_guard(user, aid)
        return engine.ack(aid, user["name"])

    @app.post("/api/alerts/{aid}/resolve")
    def resolve(aid: int, body: Resolve, user: dict = Depends(user_dep)):
        alert_guard(user, aid)
        return engine.resolve(aid, user["name"], body.action, body.note)

    @app.post("/api/patients/{pid}/notes")
    def note(pid: str, body: NoteIn, user: dict = Depends(user_dep)):
        patient_guard(user, pid)
        return engine.add_note(pid, user["name"], body.text)

    @app.post("/api/patients/{pid}/rounds")
    def round_(pid: str, body: RoundIn, user: dict = Depends(user_dep)):
        patient_guard(user, pid)
        engine.log_round(pid, user["name"], body.acvpu, body.note)
        return {"ok": True}

    @app.post("/api/patients/{pid}/checkin")
    def checkin(pid: str, body: CheckIn, user: dict = Depends(user_dep)):
        patient_guard(user, pid)
        engine.check_in(pid, user["name"], body.on, body.method)
        return {"ok": True}

    # ------------------------------------------------------------ demo / resilience lab
    @app.post("/api/chaos")
    def chaos(body: ChaosIn, user: dict = Depends(desk_dep)):
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
    def sim(body: SimIn, user: dict = Depends(desk_dep)):
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
    def index(request: Request):
        if not auth.resolve(request.cookies.get("hw_session")):
            return RedirectResponse("/login" + (f"?next={quote(request.url.query)}" if request.url.query else ""))
        return FileResponse(WEB / "index.html")

    @app.get("/login")
    def login_page():
        return FileResponse(WEB / "login.html")

    @app.get("/caregiver")
    def caregiver():
        return FileResponse(WEB / "caregiver.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
