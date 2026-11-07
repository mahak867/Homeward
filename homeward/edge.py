"""Bedside edge hub (simulated IoT gateway).

One hub per home. It reads the (simulated) sensors, writes EVERY reading to a local
SQLite outbox first (store-and-forward), and uploads in ordered batches with
exponential back-off + jitter. If the internet is down it keeps recording, and if the
patient becomes critical while offline it raises a LOCAL alarm in the room - the
bedroom ward keeps working without the cloud.
"""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from pathlib import Path

import httpx

from . import news2, quality
from .core import DEVICE_KEY, Chaos, SimClock
from .physiology import SAMPLE_MINUTES, PatientPhysiology, PatientProfile


class EdgeHub:
    def __init__(self, profile: PatientProfile, server_url: str, clock: SimClock, chaos: Chaos,
                 db_path: str | Path = ":memory:", seed: int = 0, transport: httpx.Client | None = None):
        self.p = profile
        self.device_id = f"HUB-{profile.patient_id}"
        self.url = server_url.rstrip("/")
        self.clock, self.chaos = clock, chaos
        self.phys = PatientPhysiology(profile, seed=seed)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY, kind TEXT, ts REAL, body TEXT)")
        self.db.commit()
        self.lock = threading.Lock()
        row = self.db.execute("SELECT MAX(seq) FROM outbox").fetchone()
        self.seq = row[0] or 0
        self.http = transport or httpx.Client(timeout=3.0)
        self.backoff = 0.5
        self.next_try = 0.0
        self.offline_since: float | None = None
        self.last_sent_batch: dict | None = None
        self.battery = 100.0
        self.local_alarm_active = False
        self.local_scores: list[int] = []
        self.prev_good = None
        self.next_sample_ts = None
        self.stats = {"sent": 0, "failed_attempts": 0, "max_buffer": 0, "local_alarms": 0}
        self._stop = threading.Event()

    # ------------------------------------------------------------ sensing
    def record(self, ts: float) -> dict:
        r = self.phys.sample(ts)
        with self.lock:
            self.seq += 1
            r.update({"seq": self.seq, "ts": ts})
            self.db.execute("INSERT INTO outbox(seq, kind, ts, body) VALUES(?,?,?,?)", (self.seq, "reading", ts, json.dumps(r)))
            self.db.commit()
        self._local_check(r)
        self.battery = max(0.0, self.battery - 0.002)
        return r

    def _local_check(self, r):
        """Edge-local safety net: NEWS2 on-device, alarm in the room if critical and offline."""
        q = quality.assess(r, self.prev_good)
        if not q["ok"]:
            return
        self.prev_good = r
        s = news2.score(r, self.p.spo2_scale)["total"]
        self.local_scores = (self.local_scores + [s])[-2:]
        critical = len(self.local_scores) == 2 and min(self.local_scores) >= 5
        if critical and self.offline_since is not None and not self.local_alarm_active:
            self.local_alarm_active = True
            self.stats["local_alarms"] += 1
            with self.lock:
                self.seq += 1
                ev = {"eid": f"e{self.seq}", "ts": r["ts"], "kind": "LOCAL_ALARM",
                      "detail": {"news2": s, "message": "Bedside buzzer + light activated; family alerted in-home"}}
                self.db.execute("INSERT INTO outbox(seq, kind, ts, body) VALUES(?,?,?,?)", (self.seq, "event", r["ts"], json.dumps(ev)))
                self.db.commit()
        # latched: once sounding it stays on until the hub is back online and the cloud has taken over

    def backfill(self, hours: float) -> None:
        now = self.clock.now()
        step = SAMPLE_MINUTES * 60
        t = now - hours * 3600
        while t < now:
            self.record(t)
            t += step
        self.next_sample_ts = t

    # ------------------------------------------------------------ uplink
    def pending(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def flush(self, max_batches: int = 20) -> int:
        sent = 0
        for _ in range(max_batches):
            with self.lock:
                rows = self.db.execute("SELECT seq, kind, body FROM outbox ORDER BY seq LIMIT 150").fetchall()
            if not rows:
                break
            if self.chaos.out_of_order:
                # hold back ~30% of readings so they arrive in a LATER batch (truly late data),
                # and shuffle the rest: the server must not care about arrival order
                rows = [r for r in rows if r[1] == "event" or random.random() > 0.4]
                random.shuffle(rows)
            readings = [json.loads(b) for _, k, b in rows if k == "reading"]
            events = [json.loads(b) for _, k, b in rows if k == "event"]
            batch = {"device_id": self.device_id, "patient_id": self.p.patient_id, "readings": readings,
                     "events": events, "battery": round(self.battery, 1), "buffered": len(rows),
                     "offline_seconds": round(time.time() - self.offline_since) if self.offline_since else 0}
            try:
                if self.chaos.network_down:
                    raise httpx.ConnectError("simulated network outage")
                resp = self.http.post(f"{self.url}/api/ingest", json=batch, headers={"X-Device-Key": DEVICE_KEY})
                resp.raise_for_status()
                if self.chaos.duplicate_storm:  # resend the same batch: must be a no-op server-side
                    self.http.post(f"{self.url}/api/ingest", json=batch, headers={"X-Device-Key": DEVICE_KEY})
            except Exception:
                self.stats["failed_attempts"] += 1
                if self.offline_since is None:
                    self.offline_since = time.time()
                self.next_try = time.time() + self.backoff * (0.5 + random.random())
                self.backoff = min(4.0, self.backoff * 2)
                return sent
            with self.lock:
                self.db.executemany("DELETE FROM outbox WHERE seq=?", [(r[0],) for r in rows])
                self.db.commit()
            self.stats["sent"] += len(rows)
            sent += len(rows)
            self.backoff, self.offline_since = 0.5, None
            self.local_alarm_active = False  # cloud is reachable again: alerting is back with the platform
            if self.chaos.out_of_order:
                break  # held-back readings go in a later upload -> genuinely late at the server
        return sent

    # ------------------------------------------------------------ loop
    def step(self) -> None:
        if self.p.patient_id in self.chaos.dead_devices:
            return  # hub powered off / battery dead: no data at all
        now = self.clock.now()
        if self.next_sample_ts is None:
            self.next_sample_ts = now
        while self.next_sample_ts <= now:
            self.record(self.next_sample_ts)
            self.next_sample_ts += SAMPLE_MINUTES * 60
        self.stats["max_buffer"] = max(self.stats["max_buffer"], self.pending())
        if time.time() >= self.next_try:
            self.flush()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:  # never let the hub die
                print(f"[{self.device_id}] error: {e}")
            time.sleep(0.2)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, daemon=True, name=self.device_id)
        th.start()
        return th

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        return {"device_id": self.device_id, "buffered": self.pending(), "online": self.offline_since is None,
                "offline_for_s": round(time.time() - self.offline_since) if self.offline_since else 0,
                "battery": round(self.battery, 1), "local_alarm": self.local_alarm_active,
                "dead": self.p.patient_id in self.chaos.dead_devices,
                "scenario": self.phys.scenario.label if self.phys.scenario else None,
                "progress": round(self.phys.progress(self.clock.now()), 2), **self.stats}
