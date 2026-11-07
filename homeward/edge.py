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
from .core import Chaos, SimClock, device_key
from .physiology import SAMPLE_MINUTES, PatientPhysiology, PatientProfile


class EdgeHub:
    def __init__(self, profile: PatientProfile, server_url: str, clock: SimClock, chaos: Chaos,
                 db_path: str | Path = ":memory:", seed: int = 0, transport: httpx.Client | None = None):
        self.p = profile
        self.device_id = f"HUB-{profile.patient_id}"
        self.key = device_key(self.device_id)
        self.url = server_url.rstrip("/")
        self.clock, self.chaos = clock, chaos
        self.phys = PatientPhysiology(profile, seed=seed)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY, kind TEXT, ts REAL, body TEXT)")
        self.db.commit()
        self.lock = threading.RLock()
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
        # extra home IoT: smart pill box + under-mattress bed-occupancy sensor
        self.iot_rng = random.Random(seed * 7919 + 1)
        self.last_iot_ts: float | None = None
        self.pending_opens: list[tuple[float, str]] = []
        self.skip_next_dose = False
        self.in_bed = True
        self.return_at: float | None = None
        self.fall_pending = False
        self.fallen_since: float | None = None
        self.o2_fault = False  # concentrator output purity drops (clogged filter / sieve failure)  # room PIR motion sensor sees no movement after a fall
        # real hardware (ESP32 + MAX30102 + temp probe) can override simulated channels
        self.hw: dict | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ sensing
    def _event(self, ts: float, kind: str, detail: dict) -> None:
        with self.lock:
            self.seq += 1
            ev = {"eid": f"e{self.seq}", "ts": ts, "kind": kind, "detail": detail}
            self.db.execute("INSERT INTO outbox(seq, kind, ts, body) VALUES(?,?,?,?)", (self.seq, "event", ts, json.dumps(ev)))
            self.db.commit()

    def _iot(self, ts: float) -> None:
        """Smart pill box (opened = dose taken) and bed sensor (exits/returns at night)."""
        prev = self.last_iot_ts if self.last_iot_ts is not None else ts - SAMPLE_MINUTES * 60
        self.last_iot_ts = ts
        day0 = ts - (ts - time.timezone) % 86400  # local midnight
        for d in (day0 - 86400, day0):
            for dose in self.p.schedule:
                due = d + dose["hour"] * 3600
                if prev < due <= ts:
                    if self.skip_next_dose:
                        self.skip_next_dose = False
                    elif self.iot_rng.random() < self.p.adherence:
                        self.pending_opens.append((due + self.iot_rng.uniform(2, 35) * 60, dose["med"]))
        for t_open, med in [x for x in self.pending_opens if x[0] <= ts]:
            self.pending_opens.remove((t_open, med))
            self._event(t_open, "PILLBOX_OPEN", {"med": med})
        hour = time.localtime(ts).tm_hour
        night = hour >= 22 or hour < 6
        if self.in_bed and (self.fall_pending or (night and self.iot_rng.random() < 0.03)):
            self.in_bed = False
            self.return_at = None if self.fall_pending else ts + self.iot_rng.uniform(4, 12) * 60
            if self.fall_pending:
                self.fallen_since = ts
            self.fall_pending = False
            self._event(ts, "BED_EXIT", {"night": night})
        elif not self.in_bed and self.return_at is not None and ts >= self.return_at:
            self.in_bed, self.return_at, self.fallen_since = True, None, None
            self._event(ts, "BED_RETURN", {})
        if self.fallen_since is not None and ts - self.fallen_since >= 10 * 60:
            self._event(ts, "ROOM_STILL", {"minutes": 10, "since": self.fallen_since})
            self.fallen_since = None  # report once

    def equipment(self) -> dict:
        """Medical-equipment + hub power telemetry sent with every upload."""
        eq = {"hub_power": "battery" if self.chaos.power_cut else "mains", "hub_battery": round(self.battery, 1)}
        if self.p.on_oxygen:
            running = not self.chaos.power_cut
            purity = (78 if self.o2_fault else 93) + self.iot_rng.uniform(-1, 1) if running else 0
            eq["concentrator"] = {"running": running, "flow_lpm": 2.0 if running else 0.0,
                                  "prescribed_lpm": 2.0, "purity_pct": round(purity, 1)}
        return eq

    def hw_update(self, values: dict) -> None:
        self.hw = {"values": values, "real": time.time()}

    def hw_connected(self) -> bool:
        return self.hw is not None and time.time() - self.hw["real"] < 30

    def simulate_fall(self) -> None:
        self.fall_pending = True

    def help_back_to_bed(self) -> None:
        if not self.in_bed:
            self.return_at = self.clock.now()

    def record(self, ts: float) -> dict:
        self._iot(ts)
        r = self.phys.sample(ts)
        if self.hw_connected():
            hw = self.hw["values"]
            for k in ("hr", "spo2", "temp"):
                if hw.get(k) is not None:
                    r[k] = round(float(hw[k]), 1) if k == "temp" else int(round(float(hw[k])))
            if not hw.get("finger", True):  # finger removed from the real probe -> bad signal
                r["sqi"], r["motion"] = 0.1, 0.5
            elif hw.get("sqi") is not None:
                r["sqi"] = float(hw["sqi"])
            r["source"] = "sensor"
        with self.lock:
            self.seq += 1
            r.update({"seq": self.seq, "ts": ts})
            self.db.execute("INSERT INTO outbox(seq, kind, ts, body) VALUES(?,?,?,?)", (self.seq, "reading", ts, json.dumps(r)))
            self.db.commit()
        self._local_check(r)
        if self.chaos.power_cut:  # mains off: hub runs on its internal battery
            self.battery = max(0.0, self.battery - 0.35)
        else:
            self.battery = min(100.0, self.battery + 0.5)
        return r

    def _local_check(self, r):
        """Edge-local safety net: NEWS2 on-device, alarm in the room if critical and offline."""
        q = quality.assess(r, self.prev_good)
        if not q["ok"]:
            return
        self.prev_good = r
        s = news2.score(r, self.p.spo2_scale)["total"]
        self.local_scores = [*self.local_scores, s][-2:]
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
        with self.lock:  # the API thread reads status while the hub thread writes
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
                     "equipment": self.equipment(),
                     "offline_seconds": round(time.time() - self.offline_since) if self.offline_since else 0}
            try:
                if self.chaos.network_down:
                    raise httpx.ConnectError("simulated network outage")
                resp = self.http.post(f"{self.url}/api/ingest", json=batch, headers={"X-Device-Key": self.key})
                resp.raise_for_status()
                if self.chaos.duplicate_storm:  # resend the same batch: must be a no-op server-side
                    self.http.post(f"{self.url}/api/ingest", json=batch, headers={"X-Device-Key": self.key})
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
                "progress": round(self.phys.progress(self.clock.now()), 2),
                "in_bed": self.in_bed, "real_sensor": self.hw_connected(), "equipment": self.equipment(),
                "real_sensor_values": self.hw["values"] if self.hw_connected() else None, **self.stats}
