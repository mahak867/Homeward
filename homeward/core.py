"""Shared runtime objects: simulated clock, chaos switches, configuration."""
from __future__ import annotations

import os
import threading
import time


class SimClock:
    """Simulated time. `speed` = simulated seconds per real second.

    Default 300x: one real second = 5 simulated minutes, so a 30-hour deterioration
    plays out in ~6 real minutes (and faster with the speed slider) - which is what
    lets a 3-minute demo show a 24-48 h early warning honestly, in real data.
    """

    def __init__(self, speed: float = 300.0, start: float | None = None):
        self._lock = threading.Lock()
        self._real0 = time.time()
        self._sim0 = start if start is not None else self._real0
        self._speed = speed
        self.paused = False

    def now(self) -> float:
        with self._lock:
            if self.paused:
                return self._sim0
            return self._sim0 + (time.time() - self._real0) * self._speed

    @property
    def speed(self) -> float:
        return 0.0 if self.paused else self._speed

    def set_speed(self, speed: float) -> None:
        now = self.now()
        with self._lock:
            self._sim0, self._real0, self._speed = now, time.time(), max(1.0, float(speed))

    def advance(self, seconds: float) -> None:
        """Jump simulated time forward (used by tests and fast-forward scripts)."""
        with self._lock:
            self._sim0 += seconds

    def set_paused(self, paused: bool) -> None:
        now = self.now()
        with self._lock:
            self._sim0, self._real0, self.paused = now, time.time(), paused


class Chaos:
    """Fault-injection switches used by the Resilience Lab panel and the tests."""

    FLAGS = ("network_down", "duplicate_storm", "out_of_order", "ml_down")

    def __init__(self):
        self.network_down = False
        self.duplicate_storm = False
        self.out_of_order = False
        self.ml_down = False
        self.dead_devices: set[str] = set()

    def as_dict(self) -> dict:
        return {f: getattr(self, f) for f in self.FLAGS} | {"dead_devices": sorted(self.dead_devices)}


DEVICE_KEY = os.environ.get("HOMEWARD_DEVICE_KEY", "demo-device-key-change-me")

# Escalation timers are in REAL seconds so a human presenter can react during the demo.
ESCALATE_AFTER_S = {
    "CRITICAL": float(os.environ.get("HOMEWARD_ESC_CRITICAL", 25)),
    "HIGH": float(os.environ.get("HOMEWARD_ESC_HIGH", 45)),
    "WATCH": float(os.environ.get("HOMEWARD_ESC_WATCH", 90)),
    "LOW": float(os.environ.get("HOMEWARD_ESC_LOW", 180)),
}
MAX_LEVEL = {"CRITICAL": 4, "HIGH": 3, "WATCH": 2, "LOW": 1}
SEV_RANK = {"LOW": 0, "WATCH": 1, "HIGH": 2, "CRITICAL": 3}

STALE_AFTER_MIN = 20      # sim minutes without data -> status UNKNOWN (never shown as stable)
OFFLINE_AFTER_MIN = 45    # sim minutes -> device-offline alert
ROUND_EVERY_H = 6         # scheduled caregiver check every 6 sim hours
ROUND_GRACE_MIN = 30
