"""Monitoring platform core: ingestion, risk assessment, alerting, escalation, audit.

Sense -> Understand -> Predict -> Alert -> Respond, in one place:

  ingest()      idempotent, order-independent ingestion of edge batches
  assess()      quality gate -> NEWS2 safety floor -> personal baseline + trends
                -> 24 h ML forecast (behind a circuit breaker) -> caregiver context
  _alerting()   deduplicated alerts with hysteresis and severity upgrades
  tick()        escalation ladder, stale-data watchdog, caregiver-round tracking
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time
from pathlib import Path

import numpy as np

from . import news2, notes as notes_mod, quality
from .core import (ESCALATE_AFTER_S, MAX_LEVEL, OFFLINE_AFTER_MIN, ROUND_EVERY_H, ROUND_GRACE_MIN,
                   SEV_RANK, STALE_AFTER_MIN, Chaos, SimClock)
from .features import compute, personal_baseline
from .model import CircuitBreaker, Predictor, rule_fallback
from .physiology import DEMO_PATIENTS, PatientProfile

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS readings(
  id INTEGER PRIMARY KEY, patient_id TEXT, device_id TEXT, seq INTEGER, ts REAL,
  hr REAL, rr REAL, spo2 REAL, sbp REAL, dbp REAL, temp REAL, weight REAL, sqi REAL, motion REAL,
  on_oxygen INTEGER, artifact INTEGER, artifact_reason TEXT, received_real REAL, late INTEGER, source TEXT,
  UNIQUE(device_id, seq));
CREATE INDEX IF NOT EXISTS ix_read ON readings(patient_id, ts);
CREATE TABLE IF NOT EXISTS edge_events(
  id INTEGER PRIMARY KEY, patient_id TEXT, device_id TEXT, eid TEXT, ts REAL, kind TEXT, detail TEXT,
  UNIQUE(device_id, eid));
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, patient_id TEXT, kind TEXT, severity TEXT, status TEXT, title TEXT,
  detail TEXT, created_ts REAL, created_real REAL, level INTEGER, next_escalation_real REAL,
  acked_by TEXT, acked_real REAL, resolved_by TEXT, resolved_real REAL, action TEXT, note TEXT,
  retro INTEGER DEFAULT 0, improving INTEGER DEFAULT 0, clear_since REAL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, real REAL, ts REAL, patient_id TEXT, alert_id INTEGER, actor TEXT, what TEXT);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, patient_id TEXT, ts REAL, author TEXT, text TEXT, analysis TEXT);
CREATE TABLE IF NOT EXISTS rounds(
  id INTEGER PRIMARY KEY, patient_id TEXT, ts REAL, caregiver TEXT, acvpu TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, real REAL, ts REAL, alert_id INTEGER, patient_id TEXT, level INTEGER,
  channel TEXT, to_name TEXT, to_phone TEXT, text TEXT);
"""

CALM_READINGS = 6
SEV_TIER = {"UNKNOWN": -1, "STABLE": 0, "WATCH": 1, "HIGH": 2, "CRITICAL": 3}
COLS = ("hr", "rr", "spo2", "sbp", "dbp", "temp", "weight", "sqi", "motion")


def pct(p: float) -> str:
    """Never display 0% or 100%: a synthetic-data model has no business being certain."""
    return f"{min(99, max(1, round(p * 100)))}%"


class Engine:
    def __init__(self, db_path: str | Path = ":memory:", clock: SimClock | None = None,
                 chaos: Chaos | None = None, patients: list[PatientProfile] | None = None):
        self.clock = clock or SimClock()
        self.chaos = chaos or Chaos()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.patients = {p.patient_id: p for p in (patients or DEMO_PATIENTS)}
        try:
            self.predictor = Predictor()
        except FileNotFoundError:
            self.predictor = None
        self.breaker = CircuitBreaker()
        self.state: dict[str, dict] = {pid: {} for pid in self.patients}
        self.baselines: dict[str, dict] = {}
        self.ml_above_since: dict[str, float | None] = {pid: None for pid in self.patients}
        self.round_started: dict[str, float] = {}
        self._first: dict[str, float] = {}
        self.trail: dict[str, list] = {pid: [] for pid in self.patients}  # risk history for charts
        self.on_duty: dict[str, dict] = {}
        self.m = {"received": 0, "duplicates": 0, "late": 0, "artifacts": 0, "batches": 0,
                  "scale2_prevented": 0, "artifact_alarms_prevented": 0, "unconfirmed_suppressed": 0,
                  "max_replay": 0, "edge_local_alarms": 0, "replayed_readings": 0}

    # ------------------------------------------------------------------ helpers
    def _audit(self, pid, what, actor="system", alert_id=None):
        self.db.execute("INSERT INTO audit(real, ts, patient_id, alert_id, actor, what) VALUES(?,?,?,?,?,?)",
                        (time.time(), self.clock.now(), pid, alert_id, actor, what))

    def profile(self, pid) -> PatientProfile:
        return self.patients[pid]

    # ------------------------------------------------------------------ ingest
    def ingest(self, batch: dict) -> dict:
        """Idempotent: (device_id, seq) is unique, so retries / duplicate storms are harmless.
        Order-independent: everything downstream keys on the measurement timestamp."""
        pid, dev = batch["patient_id"], batch["device_id"]
        if pid not in self.patients:
            raise KeyError(pid)
        now_real = time.time()
        accepted, dup, late = 0, 0, 0
        with self.lock:
            last_ts = self.db.execute("SELECT MAX(ts) FROM readings WHERE patient_id=?", (pid,)).fetchone()[0] or 0
            prev_good = self._last_good(pid)
            new_ts = []
            for r in sorted(batch.get("readings", []), key=lambda r: r["ts"]):
                q = quality.assess(r, prev_good)
                is_late = 1 if r["ts"] < last_ts else 0
                try:
                    self.db.execute(
                        "INSERT INTO readings(patient_id, device_id, seq, ts, hr, rr, spo2, sbp, dbp, temp, weight,"
                        " sqi, motion, on_oxygen, artifact, artifact_reason, received_real, late, source)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, dev, r["seq"], r["ts"], *[r.get(c) for c in COLS], int(bool(r.get("on_oxygen"))),
                         int(q["artifact"]), "; ".join(q["issues"]) or None, now_real, is_late, r.get("source", "sim")))
                except sqlite3.IntegrityError:
                    dup += 1
                    continue
                accepted += 1
                late += is_late
                new_ts.append(r["ts"])
                if q["artifact"]:
                    self.m["artifacts"] += 1
                else:
                    prev_good = r
            for e in batch.get("events", []):
                try:
                    self.db.execute("INSERT INTO edge_events(patient_id, device_id, eid, ts, kind, detail) VALUES(?,?,?,?,?,?)",
                                    (pid, dev, e["eid"], e["ts"], e["kind"], json.dumps(e.get("detail", {}))))
                    if e["kind"] in ("PILLBOX_OPEN", "BED_EXIT", "BED_RETURN", "ROOM_STILL"):
                        self._iot_event(pid, e)
                    if e["kind"] == "LOCAL_ALARM":
                        self.m["edge_local_alarms"] += 1
                        self._raise(pid, "EDGE", "HIGH", "Bedside hub sounded a local alarm while offline",
                                    {"why": [f"Edge hub detected NEWS2 {e['detail'].get('news2')} locally at "
                                             f"{time.strftime('%H:%M', time.localtime(e['ts']))} while the internet was down "
                                             "and alerted people in the home directly."]},
                                    e["ts"], retro=True)
                except sqlite3.IntegrityError:
                    dup += 1
            self.db.commit()
            self.m["received"] += accepted
            self.m["duplicates"] += dup
            self.m["late"] += late
            self.m["batches"] += 1
            st = self.state[pid]
            st["last_received_real"] = now_real
            st["device"] = {"id": dev, "battery": batch.get("battery"), "buffered": batch.get("buffered", 0),
                            "edge_offline_s": batch.get("offline_seconds", 0)}
            if batch.get("equipment"):
                st["equipment"] = batch["equipment"]
                self._equipment_check(pid, batch["equipment"])
            if new_ts:
                now = self.clock.now()
                old = [t for t in new_ts if t < now - 15 * 60]
                if len(old) > 1:
                    self.m["replayed_readings"] += len(old)
                    self.m["max_replay"] = max(self.m["max_replay"], len(old))
                # evaluate through replayed history (every ~30 min) so events that happened during a
                # connectivity gap are still detected and labelled as such, then evaluate "now".
                pts = new_ts[::6] if len(new_ts) > 6 else []
                for t in pts:
                    self.assess(pid, at=t, retro=t < now - 15 * 60)
                self.assess(pid, at=new_ts[-1], retro=new_ts[-1] < now - 15 * 60)
        return {"accepted": accepted, "duplicates": dup, "late": late}

    def _first_ts(self, pid):
        if pid not in self._first:
            v = self.db.execute("SELECT MIN(ts) FROM readings WHERE patient_id=?", (pid,)).fetchone()[0]
            if v is None:
                return float("inf")
            self._first[pid] = v
        return self._first[pid]

    def reset_metrics(self):
        for k in self.m:
            self.m[k] = 0

    def _last_good(self, pid, before=None):
        q = "SELECT * FROM readings WHERE patient_id=? AND artifact=0"
        args = [pid]
        if before is not None:
            q += " AND ts<?"; args.append(before)
        row = self.db.execute(q + " ORDER BY ts DESC LIMIT 1", args).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ context
    def _acvpu(self, pid, at):
        """Consciousness: latest caregiver round or a note describing new confusion (12 h window)."""
        r = self.db.execute("SELECT ts, acvpu, caregiver FROM rounds WHERE patient_id=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                            (pid, at, at - 12 * 3600)).fetchone()
        n = self.db.execute("SELECT ts, analysis, author FROM notes WHERE patient_id=? AND ts<=? AND ts>=? ORDER BY ts DESC",
                            (pid, at, at - 12 * 3600)).fetchall()
        for row in n:
            a = json.loads(row["analysis"])
            if a.get("suggests_new_confusion") and (r is None or row["ts"] >= r["ts"]):
                return "C", f"caregiver note by {row['author']}"
        if r:
            return r["acvpu"], f"round check by {r['caregiver']}"
        return "A", "assumed Alert (no caregiver check in last 12 h)"

    def _note_context(self, pid, at):
        rows = self.db.execute("SELECT ts, author, text, analysis FROM notes WHERE patient_id=? AND ts<=? AND ts>=? ORDER BY ts DESC",
                               (pid, at, at - 12 * 3600)).fetchall()
        conc, sig, red = 0.0, [], []
        for row in rows:
            a = json.loads(row["analysis"])
            conc = max(conc, a["concern_score"])
            sig += [s["label"] for s in a["signals"] if s["weight"] > 0]
            red += a["red_flags"]
        return min(conc, 3.0), sorted(set(sig)), sorted(set(red))

    # ------------------------------------------------------------------ assess
    def assess(self, pid: str, at: float | None = None, retro: bool = False) -> dict:
        prof = self.profile(pid)
        at = at if at is not None else self.clock.now()
        rows = self.db.execute(
            "SELECT ts, hr, rr, spo2, sbp, dbp, temp, weight, artifact, artifact_reason, on_oxygen FROM readings "
            "WHERE patient_id=? AND ts<=? AND ts>=? ORDER BY ts", (pid, at, at - 48 * 3600)).fetchall()
        good = [r for r in rows if not r["artifact"]]
        st = self.state[pid]
        if len(good) < 12:
            st.update({"tier": "UNKNOWN", "reason": "Collecting data"})
            return st
        if pid not in self.baselines:
            all_t = self.db.execute("SELECT ts, hr, rr, spo2, sbp, temp, weight FROM readings WHERE patient_id=? AND artifact=0 ORDER BY ts LIMIT 400", (pid,)).fetchall()
            ta = np.array([r["ts"] for r in all_t])
            if len(ta) and ta[-1] - ta[0] >= 20 * 3600:
                self.baselines[pid] = personal_baseline(ta, {k: np.array([r[k] for r in all_t], float) for k in ("hr", "rr", "spo2", "sbp", "temp", "weight")}, prof.baseline)
        base = self.baselines.get(pid, {**prof.baseline})
        t = np.array([r["ts"] for r in good])
        vals = {k: np.array([r[k] for r in good], float) for k in ("hr", "rr", "spo2", "sbp", "temp", "weight")}
        acvpu, acvpu_src = self._acvpu(pid, at)
        conc, note_sig, note_red = self._note_context(pid, at)

        last, prev = dict(good[-1]), dict(good[-2])
        for r in (last, prev):
            r["acvpu"] = acvpu
        s_last, s_prev = news2.score(last, prof.spo2_scale), news2.score(prev, prof.spo2_scale)
        confirmed = min(s_last["total"], s_prev["total"])  # two consecutive good readings
        red_conf = sorted(set(s_last["red_params"]) & set(s_prev["red_params"]))
        unconf = s_last["total"] >= 5 and confirmed < 5 and s_prev["total"] < 5
        if unconf and not retro:
            self.m["unconfirmed_suppressed"] += 1
        scale1_alarm = False
        if prof.spo2_scale == 2:
            s1 = news2.score(last, 1)
            scale1_alarm = s1["total"] >= 5 and s_last["total"] < 5

        # artifacts in the last 3 raw readings
        recent_raw = rows[-3:]
        artifact_now = bool(rows) and rows[-1]["artifact"]
        persistent_artifact = len(recent_raw) == 3 and all(r["artifact"] for r in recent_raw)
        if not retro:  # count each artifact EPISODE whose raw values would have alarmed
            if artifact_now and not st.get("_art"):
                raw = dict(rows[-1]); raw["acvpu"] = acvpu
                s_raw = news2.score(raw, prof.spo2_scale)
                if s_raw["total"] >= 5 or s_raw["red_params"]:
                    self.m["artifact_alarms_prevented"] += 1
            st["_art"] = bool(artifact_now)

        # ---- ML forecast (behind circuit breaker)
        x, ctx = compute(t, vals, len(t) - 1, base, spo2_scale=prof.spo2_scale, on_oxygen=prof.on_oxygen,
                         acvpu=acvpu, note_concern=conc)

        def ml():
            if self.chaos.ml_down or self.predictor is None:
                raise RuntimeError("ML service unavailable")
            return self.predictor.predict(x, ctx)

        span_h = (t[-1] - self._first_ts(pid)) / 3600
        if span_h < 30:  # model needs the personal baseline + 6 h of trend, exactly as in training
            fc = {"prob": 0.0, "threshold": 1.0, "factors": [], "reassuring": [], "source": "calibrating",
                  "calibrating_h_left": round(30 - span_h, 1)}
        else:
            fc = self.breaker.call(ml, lambda: rule_fallback(ctx))
        thr = fc["threshold"]
        if fc["prob"] >= thr:
            if self.ml_above_since[pid] is None or self.ml_above_since[pid] > at:
                self.ml_above_since[pid] = at
        elif fc["prob"] < 0.8 * thr:  # hysteresis
            self.ml_above_since[pid] = None
        early = self.ml_above_since[pid] is not None and at - self.ml_above_since[pid] >= 55 * 60

        # ---- tier
        why = []
        if confirmed >= 7:
            tier = "CRITICAL"; why.append(f"NEWS2 {s_last['total']} (confirmed on 2 consecutive readings) - {s_last['response']}")
        elif confirmed >= 5:
            tier = "HIGH"; why.append(f"NEWS2 {s_last['total']} (confirmed) - {s_last['response']}")
        elif red_conf:
            tier = "WATCH"; why.append("Red-zone value: " + ", ".join(news2.LABELS[p] for p in red_conf))
        else:
            tier = "STABLE"
        if note_red:
            if "Unresponsive / fainted" in note_red:
                tier = "CRITICAL"
            elif SEV_RANK.get(tier, 0) < SEV_RANK["HIGH"]:
                tier = "HIGH"
            why.append("Caregiver reported: " + ", ".join(note_red))
        if early and tier == "STABLE":
            tier = "WATCH"
        if early:
            why.append(f"Early warning: {pct(fc['prob'])} estimated chance of reaching NEWS2 >= 5 within 24 h")
        if conc >= 1.5 and tier == "STABLE":
            tier = "WATCH"; why.append("Caregiver notes describe: " + ", ".join(note_sig))

        # display hysteresis: escalate immediately; step down one level only after 30 min of calmer readings
        prev_tier = st.get("tier")
        if prev_tier in SEV_TIER and SEV_TIER[tier] < SEV_TIER[prev_tier] and at >= st.get("at", 0):
            st["calm_count"] = st.get("calm_count", 0) + 1
            if st["calm_count"] < CALM_READINGS:
                why.append(f"Holding {prev_tier} until improvement is sustained ({st['calm_count']}/{CALM_READINGS} calmer readings)")
                tier = prev_tier
            else:  # step down one level at a time
                st["calm_count"] = 0
                tier = [k for k, v in SEV_TIER.items() if v == SEV_TIER[prev_tier] - 1][0]
        else:
            st["calm_count"] = 0

        level = {"STABLE": "LOW", "WATCH": "MEDIUM", "HIGH": "HIGH", "CRITICAL": "HIGH"}[tier]
        res = {
            "tier": tier, "at": at, "why": why, "risk_level": level,
            "risk_reason": self._short_reason(tier, s_last, last, fc, early, note_sig, note_red, ctx),
            "news2": s_last, "news2_rows": news2.explain_components(last, s_last), "news2_confirmed": confirmed,
            "latest": {k: last[k] for k in ("hr", "rr", "spo2", "sbp", "dbp", "temp", "weight")} | {"acvpu": acvpu, "ts": last["ts"]},
            "acvpu_source": acvpu_src,
            "forecast": fc | {"early_warning": early}, "baseline": {k: round(v, 1) for k, v in base.items()},
            "notes": {"concern": conc, "signals": note_sig, "red_flags": note_red},
            "quality": {"artifact_now": bool(artifact_now), "persistent_artifact": persistent_artifact,
                        "last_issue": rows[-1]["artifact_reason"] if artifact_now else None},
            "scale1_would_alarm": scale1_alarm,
        }
        tr = self.trail[pid]
        tr.append({"at": at, "news2": s_last["total"], "tier": tier,
                   "prob": None if fc["source"] == "calibrating" else fc["prob"]})
        if len(tr) > 3000:
            del tr[:500]
        if not retro or at >= st.get("at", 0):
            st.update(res)
        if not retro:  # count EPISODES (rising edges), not readings
            if scale1_alarm and not st.get("_s1"):
                self.m["scale2_prevented"] += 1
            st["_s1"] = scale1_alarm
        self._alerting(pid, res, retro)
        return res

    @staticmethod
    def _short_reason(tier, s, last, fc, early, note_sig, note_red, ctx):
        """One plain-English sentence: why Low / Medium / High."""
        names = {"rr": "breathing {:.0f}/min", "spo2": "oxygen {:.0f}%", "sbp": "BP {:.0f}", "hr": "pulse {:.0f}",
                 "temp": "temperature {:.1f}°C", "acvpu": "new confusion", "o2": "on oxygen"}
        abn = [k for k, v in sorted(s["components"].items(), key=lambda kv: -kv[1]) if v > 0 and k != "o2"][:3]
        parts = []
        if tier in ("HIGH", "CRITICAL") and abn:
            parts.append(f"NEWS2 {s['total']}: " + ", ".join(names[k].format(last[k]) if k != "acvpu" else names[k] for k in abn))
        elif early and fc["factors"]:
            f = [x["text"] for x in fc["factors"] if x["feature"] != "news2_now"][:2]
            parts.append("Trending worse: " + "; ".join(f).lower() if f else "Trending worse")
        elif abn:
            parts.append("Mild changes: " + ", ".join(names[k].format(last[k]) if k != "acvpu" else names[k] for k in abn))
        else:
            parts.append("Vital signs within this patient's usual range")
        if note_red:
            parts.append("caregiver reports " + ", ".join(note_red).lower())
        elif note_sig:
            parts.append("caregiver notes mention " + ", ".join(note_sig[:3]).lower())
        return "; ".join(parts) + "."

    # ------------------------------------------------------------------ alerts
    def _open(self, pid, kind):
        row = self.db.execute("SELECT * FROM alerts WHERE patient_id=? AND kind=? AND status!='RESOLVED' ORDER BY id DESC LIMIT 1",
                              (pid, kind)).fetchone()
        return dict(row) if row else None

    def _raise(self, pid, kind, sev, title, detail, ts, retro=False):
        cur = self._open(pid, kind)
        now = time.time()
        if cur:
            if SEV_RANK[sev] > SEV_RANK[cur["severity"]]:
                self.db.execute("UPDATE alerts SET severity=?, title=?, detail=?, improving=0, clear_since=NULL,"
                                " next_escalation_real=? WHERE id=?",
                                (sev, title, json.dumps(detail), now + ESCALATE_AFTER_S[sev] if cur["status"] == "OPEN" else None, cur["id"]))
                self._audit(pid, f"Alert #{cur['id']} upgraded {cur['severity']} -> {sev}: {title}", alert_id=cur["id"])
                self._notify(cur["id"], pid, max(1, cur["level"]), sev, title, upgrade=True)
                if sev == "CRITICAL":
                    self._notify(cur["id"], pid, 3, sev, title, parallel=True)
            else:  # same or lower severity: refresh the explanation (and the % in the title) silently
                self.db.execute("UPDATE alerts SET detail=?, title=?, improving=0, clear_since=NULL WHERE id=?",
                                (json.dumps(detail), title if SEV_RANK[sev] == SEV_RANK[cur["severity"]] else cur["title"], cur["id"]))
            return cur["id"]
        title2 = ("[During connectivity gap] " + title) if retro and kind != "EDGE" else title
        c = self.db.execute(
            "INSERT INTO alerts(patient_id, kind, severity, status, title, detail, created_ts, created_real, level,"
            " next_escalation_real, retro) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pid, kind, sev, "OPEN", title2, json.dumps(detail), ts, now, 1, now + ESCALATE_AFTER_S[sev], int(retro)))
        aid = c.lastrowid
        self._audit(pid, f"Alert #{aid} raised [{sev}] {title2}", alert_id=aid)
        self._notify(aid, pid, 1, sev, title2)
        if sev == "CRITICAL":  # critical: caregiver AND care desk at the same time, no waiting
            self._notify(aid, pid, 3, sev, title2, parallel=True)
        return aid

    def _clear(self, pid, kind, auto_resolve_after_min=None, ts=None):
        cur = self._open(pid, kind)
        if not cur:
            return
        ts = ts or self.clock.now()
        if not cur["clear_since"]:
            self.db.execute("UPDATE alerts SET improving=1, clear_since=? WHERE id=?", (ts, cur["id"]))
            self._audit(pid, f"Alert #{cur['id']}: condition improving", alert_id=cur["id"])
        elif auto_resolve_after_min is not None and ts - cur["clear_since"] >= auto_resolve_after_min * 60 and cur["status"] == "OPEN":
            self.db.execute("UPDATE alerts SET status='RESOLVED', resolved_by='system', resolved_real=?, action='self-resolved',"
                            " note='Condition cleared and stayed clear (hysteresis window)' WHERE id=?", (time.time(), cur["id"]))
            self._audit(pid, f"Alert #{cur['id']} auto-resolved: condition cleared for {auto_resolve_after_min} min", alert_id=cur["id"])

    def _alerting(self, pid, r, retro):
        tier, at = r["tier"], r["at"]
        lv = r["latest"]
        detail = {"why": r["why"], "news2": r["news2"]["total"], "vitals": lv,
                  "factors": r["forecast"]["factors"], "prob": r["forecast"]["prob"],
                  "recommendation": r["news2"]["response"]}
        if tier in ("CRITICAL", "HIGH") and r["news2_confirmed"] >= 5:
            self._raise(pid, "VITALS", tier, f"NEWS2 {r['news2']['total']} - {r['news2']['response'].split(':')[0]}", detail, at, retro)
        elif tier == "WATCH" and r["news2"]["red_params"] and r["news2_confirmed"] < 5:
            names = ", ".join(news2.LABELS[p] for p in r["news2"]["red_params"])
            self._raise(pid, "VITALS", "WATCH", f"{names} in red zone", detail, at, retro)
        else:
            self._clear(pid, "VITALS", ts=at)
        if r["notes"]["red_flags"]:
            sev = "CRITICAL" if "Unresponsive / fainted" in r["notes"]["red_flags"] else "HIGH"
            self._raise(pid, "NOTE", sev, "Caregiver reported: " + ", ".join(r["notes"]["red_flags"]), detail, at, retro)
        if r["forecast"]["early_warning"] and r["news2_confirmed"] < 5:
            detail = {**detail, "recommendation": "Not an emergency yet. Look in on the patient now (breathing, alertness, drinking/urine), "
                      "add a note in the app, and the care desk will review the trend. If anything looks worse, call."}
            self._raise(pid, "EARLY_WARNING", "WATCH", f"Early warning: {pct(r['forecast']['prob'])} risk of deterioration in next 24 h", detail, at, retro)
        elif not r["forecast"]["early_warning"]:
            self._clear(pid, "EARLY_WARNING", auto_resolve_after_min=120, ts=at)
        if not retro:
            if r["quality"]["persistent_artifact"]:
                self._raise(pid, "SENSOR", "LOW", "Check the finger sensor - readings unreliable",
                            {"why": [r["quality"]["last_issue"] or "poor signal"],
                             "recommendation": "Re-seat the SpO2 probe and keep the hand still. Alarms are paused on bad data, not on the patient."},
                            at)
            elif not r["quality"]["artifact_now"]:
                self._clear(pid, "SENSOR", auto_resolve_after_min=10, ts=at)

    def _notify(self, aid, pid, level, sev, title, upgrade=False, parallel=False):
        prof = self.profile(pid)
        c = next((c for c in prof.contacts if c["level"] == level), prof.contacts[-1])
        channel = {1: "App push + WhatsApp", 2: "SMS + auto voice call", 3: "Care-desk console + call",
                   4: "Emergency call (108) - suggested, requires human confirmation"}[c["level"]]
        prefix = "UPGRADED " if upgrade else ("PARALLEL " if parallel else "")
        text = f"{prefix}[{sev}] {prof.name}: {title}. Open HomeWard to acknowledge."
        self.db.execute("INSERT INTO messages(real, ts, alert_id, patient_id, level, channel, to_name, to_phone, text)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (time.time(), self.clock.now(), aid, pid, c["level"], channel, c["name"], c["phone"], text))

    # ------------------------------------------------------------------ responses
    def ack(self, aid: int, by: str) -> dict:
        with self.lock:
            a = self.db.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
            if not a:
                raise KeyError(aid)
            if a["status"] == "OPEN":
                self.db.execute("UPDATE alerts SET status='ACKNOWLEDGED', acked_by=?, acked_real=?, next_escalation_real=? WHERE id=?",
                                (by, time.time(), time.time() + 3 * ESCALATE_AFTER_S[a["severity"]], aid))
                self._audit(a["patient_id"], f"Alert #{aid} acknowledged by {by} after {time.time() - a['created_real']:.0f}s", by, aid)
                self.db.commit()
            return dict(self.db.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone())

    def resolve(self, aid: int, by: str, action: str, note: str = "") -> dict:
        with self.lock:
            a = self.db.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
            if not a:
                raise KeyError(aid)
            if a["status"] != "RESOLVED":
                if a["status"] == "OPEN":
                    self.db.execute("UPDATE alerts SET acked_by=?, acked_real=? WHERE id=?", (by, time.time(), aid))
                self.db.execute("UPDATE alerts SET status='RESOLVED', resolved_by=?, resolved_real=?, action=?, note=? WHERE id=?",
                                (by, time.time(), action, note, aid))
                self._audit(a["patient_id"], f"Alert #{aid} resolved by {by}: {action}" + (f" - {note}" if note else ""), by, aid)
                if note:
                    self.add_note(a["patient_id"], by, note)
                self.db.commit()
            return dict(self.db.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone())

    def add_note(self, pid: str, author: str, text: str) -> dict:
        a = notes_mod.analyse(text)
        with self.lock:
            ts = self.clock.now()
            self.db.execute("INSERT INTO notes(patient_id, ts, author, text, analysis) VALUES(?,?,?,?,?)",
                            (pid, ts, author, text, json.dumps(a, ensure_ascii=False)))
            self._audit(pid, f"Note by {author}: understood {[s['label'] for s in a['signals']] or 'no concern'}", author)
            self.db.commit()
            self.assess(pid)
            self.db.commit()
        return a

    def log_round(self, pid: str, caregiver: str, acvpu: str = "A", note: str = "", ts: float | None = None) -> None:
        with self.lock:
            ts = ts or self.clock.now()
            self.db.execute("INSERT INTO rounds(patient_id, ts, caregiver, acvpu, note) VALUES(?,?,?,?,?)",
                            (pid, ts, caregiver, acvpu, note))
            self._audit(pid, f"Round check by {caregiver}: consciousness {acvpu}", caregiver)
            cur = self._open(pid, "CAREGIVER")
            if cur:
                self.db.execute("UPDATE alerts SET status='RESOLVED', resolved_by=?, resolved_real=?, action='round completed' WHERE id=?",
                                (caregiver, time.time(), cur["id"]))
            self.db.commit()
            if note:
                self.add_note(pid, caregiver, note)
            else:
                self.assess(pid)
                self.db.commit()

    def check_in(self, pid: str, caregiver: str, on: bool, method: str = "app") -> None:
        """method 'bedside-tag' = caregiver scanned the QR/NFC tag fixed at the bedside (verified presence)."""
        with self.lock:
            if on:
                self.on_duty[pid] = {"caregiver": caregiver, "since": self.clock.now(), "verified": method == "bedside-tag",
                                     "method": method, "last_seen": self.clock.now()}
            else:
                self.on_duty.pop(pid, None)
            how = " via bedside tag (verified presence)" if method == "bedside-tag" else ""
            self._audit(pid, f"{caregiver} {'checked in' + how if on else 'checked out'}", caregiver)
            self.db.commit()

    # ------------------------------------------------------------------ home IoT
    def _iot_event(self, pid, e):
        st = self.state[pid]
        if e["kind"] == "PILLBOX_OPEN":
            st["last_dose"] = {"ts": e["ts"], "med": e["detail"].get("med")}
            cur = self._open(pid, "MEDICATION")
            if cur:
                self.db.execute("UPDATE alerts SET status='RESOLVED', resolved_by='system', resolved_real=?, action='dose taken',"
                                " note='Pill box opened' WHERE id=?", (time.time(), cur["id"]))
                self._audit(pid, f"Alert #{cur['id']} auto-resolved: pill box opened", alert_id=cur["id"])
        elif e["kind"] == "ROOM_STILL":
            bed = st.get("bed")
            if bed and not bed["in_bed"]:
                self._raise(pid, "FALL", "HIGH", "Possible fall: out of bed, no movement in the room",
                            {"why": [f"Bed sensor: left bed at {time.strftime('%H:%M', time.localtime(bed['since']))}.",
                                     "Room motion sensor: no movement for 10 min since then."],
                             "recommendation": "Check on the patient now. If they are on the floor, don't lift alone; call 108 if injured, confused or not responding."},
                            e["ts"])
        elif e["kind"] == "BED_EXIT":
            if st.get("bed", {}).get("since", 0) <= e["ts"]:
                st["bed"] = {"in_bed": False, "since": e["ts"]}
        elif e["kind"] == "BED_RETURN":
            if st.get("bed", {}).get("since", 0) <= e["ts"]:
                out_min = (e["ts"] - st.get("bed", {}).get("since", e["ts"])) / 60
                st["bed"] = {"in_bed": True, "since": e["ts"]}
                cur = self._open(pid, "FALL")
                if cur:
                    self.db.execute("UPDATE alerts SET status='RESOLVED', resolved_by='system', resolved_real=?, action='back in bed',"
                                    " note=? WHERE id=?", (time.time(), f"Bed sensor: back in bed after {out_min:.0f} min", cur["id"]))
                    self._audit(pid, f"Alert #{cur['id']} auto-resolved: patient back in bed", alert_id=cur["id"])

    def _equipment_check(self, pid, eq):
        now = self.clock.now()
        conc = eq.get("concentrator")
        if conc and not conc["running"]:
            self._raise(pid, "EQUIPMENT", "HIGH", "Oxygen concentrator stopped" + (" - power cut" if eq.get("hub_power") == "battery" else ""),
                        {"why": [f"Prescribed {conc['prescribed_lpm']} L/min oxygen; concentrator delivering 0 L/min.",
                                 "Patient is oxygen-dependent (COPD)."],
                         "recommendation": "Switch to the backup oxygen cylinder now and keep the patient sitting up. Call the care desk if breathless."}, now)
        elif conc and conc["purity_pct"] < 85:
            self._raise(pid, "EQUIPMENT", "WATCH", f"Oxygen concentrator output low ({conc['purity_pct']:.0f}% O₂)",
                        {"why": [f"Concentrator purity {conc['purity_pct']:.0f}% (normal ≥ 90%)."],
                         "recommendation": "Check / clean the filter, then call the equipment vendor. Use the backup cylinder if SpO₂ drops."}, now)
        else:
            self._clear(pid, "EQUIPMENT", auto_resolve_after_min=5, ts=now)
        if eq.get("hub_power") == "battery" and eq.get("hub_battery", 100) < 30:
            self._raise(pid, "POWER", "WATCH", f"Power cut: bedside hub battery {eq['hub_battery']:.0f}%",
                        {"why": ["Mains power is off; monitoring continues on the hub's battery."],
                         "recommendation": "Plug the hub into a power bank / inverter to keep monitoring running."}, now)
        elif eq.get("hub_power") == "mains":
            self._clear(pid, "POWER", auto_resolve_after_min=0, ts=now)

    def _dose_check(self, pid, now):
        prof = self.profile(pid)
        st = self.state[pid]
        due_list = []
        day0 = now - (now - time.timezone) % 86400
        for d in (day0 - 86400, day0):
            for dose in prof.schedule:
                due_list.append((d + dose["hour"] * 3600, dose["med"]))
        past = [x for x in due_list if x[0] <= now]
        future = [x for x in due_list if x[0] > now] + [(t + 86400, m) for t, m in due_list]
        nxt = min(future) if future else None
        st["next_dose"] = {"ts": nxt[0], "med": nxt[1]} if nxt else None
        if not past:
            return
        due, med = max(past)
        if now - due < 60 * 60 or now - due > 10 * 3600:
            return
        taken = self.db.execute("SELECT 1 FROM edge_events WHERE patient_id=? AND kind='PILLBOX_OPEN' AND ts>=? LIMIT 1",
                                (pid, due - 30 * 60)).fetchone()
        st["dose_missed"] = None if taken else {"due": due, "med": med}
        if not taken and st.get("_dose_alerted") != due:
            st["_dose_alerted"] = due
            self._raise(pid, "MEDICATION", "WATCH", f"Dose not taken: {med}",
                        {"why": [f"Smart pill box not opened since the {time.strftime('%H:%M', time.localtime(due))} dose was due."]
                         + (["For heart failure, a missed water tablet can lead to fluid build-up within 1-2 days."] if "Furosemide" in med else []),
                         "recommendation": "Remind the patient / give the dose if it is still appropriate, or ask the care desk."}, now)

    def _fall_check(self, pid, now):
        bed = self.state[pid].get("bed")
        if not bed or bed["in_bed"]:
            return
        hour = time.localtime(bed["since"]).tm_hour
        out_min = (now - bed["since"]) / 60
        if (hour >= 22 or hour < 6) and out_min >= 20:
            self._raise(pid, "FALL", "HIGH", f"Out of bed {out_min:.0f} min at night - possible fall",
                        {"why": [f"Bed sensor: left bed at {time.strftime('%H:%M', time.localtime(bed['since']))} and has not returned.",
                                 "Normal night-time toilet trips in this home last 4-12 min."],
                         "recommendation": "Check on the patient now. If they are on the floor, don't lift alone; call 108 if injured, confused or not responding."}, now)

    # ------------------------------------------------------------------ watchdog
    def tick(self) -> None:
        """Run every real second: escalation ladder, stale-data watchdog, round schedule."""
        with self.lock:
            now_r, now = time.time(), self.clock.now()
            for a in self.db.execute("SELECT * FROM alerts WHERE status!='RESOLVED' AND next_escalation_real IS NOT NULL AND next_escalation_real<=?",
                                     (now_r,)).fetchall():
                if a["status"] == "OPEN" and a["level"] < MAX_LEVEL[a["severity"]]:
                    lvl = a["level"] + 1
                    self.db.execute("UPDATE alerts SET level=?, next_escalation_real=? WHERE id=?",
                                    (lvl, now_r + ESCALATE_AFTER_S[a["severity"]], a["id"]))
                    self._audit(a["patient_id"], f"Alert #{a['id']} not acknowledged - escalated to level {lvl}", alert_id=a["id"])
                    self._notify(a["id"], a["patient_id"], lvl, a["severity"], a["title"])
                elif a["status"] == "ACKNOWLEDGED":
                    self.db.execute("UPDATE alerts SET next_escalation_real=? WHERE id=?",
                                    (now_r + 3 * ESCALATE_AFTER_S[a["severity"]], a["id"]))
                    self._notify(a["id"], a["patient_id"], 1, a["severity"], "Reminder: acknowledged but not yet resolved - " + a["title"])
                else:
                    self.db.execute("UPDATE alerts SET next_escalation_real=NULL WHERE id=?", (a["id"],))
            for pid in self.patients:
                st = self.state[pid]
                last = self.db.execute("SELECT MAX(ts) FROM readings WHERE patient_id=?", (pid,)).fetchone()[0]
                gap_min = (now - last) / 60 if last else None
                st["data_age_min"] = round(gap_min, 1) if gap_min is not None else None
                if last is None:
                    st["data_state"] = "NO_DATA"
                elif gap_min > OFFLINE_AFTER_MIN:
                    st["data_state"] = "OFFLINE"
                    was = st.get("tier", "STABLE")
                    sev = "HIGH" if was in ("HIGH", "CRITICAL", "WATCH") else "WATCH"
                    self._raise(pid, "DEVICE", sev, "No data from bedside hub - patient status unknown",
                                {"why": [f"No readings for {gap_min:.0f} min (last risk level: {was}).",
                                         "The dashboard never shows 'stable' on old data."],
                                 "recommendation": "Check on the patient in person and check the hub's power / Wi-Fi."}, now)
                elif gap_min > STALE_AFTER_MIN:
                    st["data_state"] = "STALE"
                else:
                    st["data_state"] = "LIVE"
                    cur = self._open(pid, "DEVICE")
                    if cur:
                        self.db.execute("UPDATE alerts SET status='RESOLVED', resolved_by='system', resolved_real=?,"
                                        " action='data resumed', note='Hub reconnected and buffered readings were synced' WHERE id=?",
                                        (now_r, cur["id"]))
                        self._audit(pid, f"Alert #{cur['id']} auto-resolved: data resumed", alert_id=cur["id"])
                if st.get("data_state") in ("LIVE", "STALE"):
                    self._dose_check(pid, now)
                    self._fall_check(pid, now)
                # scheduled caregiver rounds
                lr = self.db.execute("SELECT MAX(ts) FROM rounds WHERE patient_id=?", (pid,)).fetchone()[0]
                start = self.round_started.setdefault(pid, now)
                due = (lr or start) + ROUND_EVERY_H * 3600
                st["round_due"] = due
                if now > due + ROUND_GRACE_MIN * 60:
                    self._raise(pid, "CAREGIVER", "WATCH", "Scheduled caregiver check overdue",
                                {"why": [f"Routine check (vitals look, medicines, consciousness) was due at "
                                         f"{time.strftime('%H:%M', time.localtime(due))}."],
                                 "recommendation": "Log a round check in the app."}, now)
            self.db.commit()

    # ------------------------------------------------------------------ views
    def alerts(self, include_resolved=40) -> list[dict]:
        rows = self.db.execute("SELECT * FROM alerts WHERE status!='RESOLVED' ORDER BY "
                               "CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'WATCH' THEN 2 ELSE 3 END, id DESC").fetchall()
        res = self.db.execute("SELECT * FROM alerts WHERE status='RESOLVED' ORDER BY resolved_real DESC LIMIT ?", (include_resolved,)).fetchall()
        out = []
        for r in list(rows) + list(res):
            d = dict(r); d["detail"] = json.loads(d["detail"] or "{}")
            d["patient_name"] = self.patients[d["patient_id"]].name
            c = next((c for c in self.patients[d["patient_id"]].contacts if c["level"] == d["level"]), None)
            d["current_contact"] = c
            d["escalates_in_s"] = round(d["next_escalation_real"] - time.time()) if d["next_escalation_real"] and d["status"] == "OPEN" and d["level"] < MAX_LEVEL[d["severity"]] else None
            out.append(d)
        return out

    def response_stats(self) -> dict:
        rows = self.db.execute("SELECT acked_by, acked_real-created_real AS dt, level FROM alerts WHERE acked_real IS NOT NULL AND acked_by!='system'").fetchall()
        by = {}
        for r in rows:
            by.setdefault(r["acked_by"], []).append(r["dt"])
        tot = self.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        esc = self.db.execute("SELECT COUNT(*) FROM alerts WHERE level>1").fetchone()[0]
        return {"per_responder": {k: {"n": len(v), "median_ack_s": round(statistics.median(v))} for k, v in by.items()},
                "alerts_total": tot, "escalated": esc,
                "median_ack_s": round(statistics.median([r["dt"] for r in rows])) if rows else None}

    def history(self, pid: str, hours: float = 48) -> dict:
        now = self.clock.now()
        rows = self.db.execute("SELECT ts, hr, rr, spo2, sbp, dbp, temp, weight, artifact, late, source FROM readings WHERE patient_id=? AND ts>=? ORDER BY ts",
                               (pid, now - hours * 3600)).fetchall()
        trail = sorted((x for x in self.trail[pid] if x["at"] >= now - hours * 3600), key=lambda x: x["at"])
        return {"readings": [dict(r) for r in rows], "trail": trail}

    def sbar(self, pid: str) -> str:
        """Situation-Background-Assessment-Recommendation handover for a phone call to a clinician."""
        p, st = self.profile(pid), self.state[pid]
        if "latest" not in st:
            return "No data yet."
        lv, n2, fc = st["latest"], st["news2"], st["forecast"]
        trend = "; ".join(f["text"] for f in fc.get("factors", []) if f["feature"] != "news2_now") or "no strong adverse trend"
        notes_txt = ", ".join(st["notes"]["signals"]) or "none"
        return (
            f"S - Situation: I am calling about {p.name}, {p.age}{p.sex}, monitored at home by HomeWard. "
            f"Current NEWS2 is {n2['total']} ({n2['band']}). Latest: HR {lv['hr']:.0f}, RR {lv['rr']:.0f}, SpO2 {lv['spo2']:.0f}%"
            f"{' on oxygen' if p.on_oxygen else ' on air'}, BP {lv['sbp']:.0f}/{lv['dbp']:.0f}, Temp {lv['temp']:.1f} °C, "
            f"consciousness {lv['acvpu']}.\n"
            f"B - Background: {p.condition}. Medicines: {', '.join(p.meds)}."
            f"{' NEWS2 SpO2 scale 2 (target 88-92%) in use.' if p.spo2_scale == 2 else ''}\n"
            f"A - Assessment (decision support, not a diagnosis): {'; '.join(st['why']) or 'no current trigger'}. "
            f"Trend vs personal baseline: {trend}. Caregiver observations: {notes_txt}. "
            f"Estimated 24 h deterioration risk {pct(fc['prob'])} ({fc['source']}).\n"
            f"R - Recommendation / request: {n2['response']} Please advise on review and next steps."
        )
