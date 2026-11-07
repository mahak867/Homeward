"""Synthetic physiology engine.

One generator drives BOTH the live bedside simulator and the ML training cohort, so
the model is trained on the same kind of signal it sees in the demo.

Every value here is synthetic. The deterioration "scenarios" are stylised trajectories
loosely shaped after how home patients are described to deteriorate in the
remote-monitoring literature (gradual drift of RR/HR/temperature before an obvious
event). They are NOT clinical models.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SAMPLE_MINUTES = 5  # bedside device samples every 5 (simulated) minutes

VITALS = ("hr", "rr", "spo2", "sbp", "dbp", "temp")


@dataclass
class Scenario:
    name: str
    label: str
    duration_h: float
    effects: dict  # vital -> delta at full progression
    confusion_at: float | None = None  # progression fraction where new confusion appears
    shape: float = 1.6  # >1 = slow start, accelerating later (typical deterioration)


SCENARIOS: dict[str, Scenario] = {
    "sepsis": Scenario(
        "sepsis", "Infection / sepsis-like deterioration", 30,
        {"hr": 38, "rr": 11, "temp": 2.1, "sbp": -38, "dbp": -20, "spo2": -5},
        confusion_at=0.85,
    ),
    "copd_exacerbation": Scenario(
        "copd_exacerbation", "COPD exacerbation", 28,
        {"hr": 26, "rr": 11, "temp": 0.9, "sbp": 6, "dbp": 3, "spo2": -6},
        confusion_at=0.95,
    ),
    "hf_decompensation": Scenario(
        "hf_decompensation", "Heart-failure decompensation (fluid build-up)", 40,
        {"hr": 24, "rr": 10, "spo2": -6, "sbp": -14, "dbp": -6, "weight": 2.8, "temp": 0.1},
    ),
    "acute_hypoxia": Scenario(
        "acute_hypoxia", "Sudden breathing emergency", 1.5,
        {"spo2": -15, "rr": 12, "hr": 32, "sbp": -10},
        shape=1.0,
    ),
}


@dataclass
class PatientProfile:
    patient_id: str
    name: str
    age: int
    sex: str
    condition: str
    baseline: dict
    on_oxygen: bool = False
    spo2_scale: int = 1  # NEWS2 SpO2 scale 2 = target 88-92% (hypercapnic respiratory failure)
    default_scenario: str | None = None
    meds: list = field(default_factory=list)
    contacts: list = field(default_factory=list)
    language: str = "en"


# Synthetic, fictional patients (no real people).
DEMO_PATIENTS: list[PatientProfile] = [
    PatientProfile(
        "P-101", "Kamla Devi", 74, "F",
        "Discharged 3 days ago after pneumonia; type 2 diabetes",
        {"hr": 82, "rr": 17, "spo2": 95, "sbp": 128, "dbp": 76, "temp": 37.0, "weight": 58.0},
        default_scenario="sepsis",
        meds=["Amoxicillin-clavulanate 625 mg", "Metformin 500 mg"],
        contacts=[
            {"level": 1, "role": "Primary caregiver", "name": "Sunita (daughter-in-law)", "phone": "+91-90000-00101"},
            {"level": 2, "role": "Backup family", "name": "Ramesh (son)", "phone": "+91-90000-00102"},
            {"level": 3, "role": "Tele-nurse care desk", "name": "HomeWard Care Desk", "phone": "+91-90000-00999"},
            {"level": 4, "role": "Emergency", "name": "Ambulance 108 + Dr. Rao", "phone": "108"},
        ],
        language="hi",
    ),
    PatientProfile(
        "P-102", "Rajesh Iyer", 67, "M",
        "COPD (severe) on home oxygen 2 L/min - target SpO2 88-92%",
        {"hr": 84, "rr": 18, "spo2": 90, "sbp": 134, "dbp": 80, "temp": 36.8, "weight": 64.0},
        on_oxygen=True, spo2_scale=2, default_scenario="copd_exacerbation",
        meds=["Tiotropium inhaler", "Salbutamol PRN", "Home O2 2 L/min"],
        contacts=[
            {"level": 1, "role": "Primary caregiver", "name": "Meena (wife)", "phone": "+91-90000-00201"},
            {"level": 2, "role": "Backup family", "name": "Karthik (son)", "phone": "+91-90000-00202"},
            {"level": 3, "role": "Tele-nurse care desk", "name": "HomeWard Care Desk", "phone": "+91-90000-00999"},
            {"level": 4, "role": "Emergency", "name": "Ambulance 108 + Dr. Menon", "phone": "108"},
        ],
    ),
    PatientProfile(
        "P-103", "Anil Mehta", 61, "M",
        "Heart failure (reduced EF); daily weight on smart scale",
        {"hr": 76, "rr": 16, "spo2": 96, "sbp": 112, "dbp": 70, "temp": 36.7, "weight": 78.0},
        default_scenario="hf_decompensation",
        meds=["Furosemide 40 mg", "Sacubitril-valsartan", "Bisoprolol 2.5 mg"],
        contacts=[
            {"level": 1, "role": "Primary caregiver", "name": "Home nurse Joseph", "phone": "+91-90000-00301"},
            {"level": 2, "role": "Backup family", "name": "Priya (daughter)", "phone": "+91-90000-00302"},
            {"level": 3, "role": "Tele-nurse care desk", "name": "HomeWard Care Desk", "phone": "+91-90000-00999"},
            {"level": 4, "role": "Emergency", "name": "Ambulance 108 + Dr. Shah", "phone": "108"},
        ],
    ),
]

NOISE_SD = {"hr": 2.2, "rr": 0.9, "spo2": 0.7, "sbp": 4.0, "dbp": 2.5, "temp": 0.08}
DRIFT_SD = {"hr": 2.5, "rr": 0.8, "spo2": 0.5, "sbp": 4.0, "dbp": 2.5, "temp": 0.12}


def _progress(t: float, onset: float | None, sc: Scenario | None) -> float:
    if sc is None or onset is None or t <= onset:
        return 0.0
    p = min(1.0, (t - onset) / (sc.duration_h * 3600.0))
    return p ** sc.shape


class PatientPhysiology:
    """Stateful generator of realistic-looking vital-sign streams for one patient."""

    def __init__(self, profile: PatientProfile, seed: int = 0):
        self.p = profile
        self.rng = np.random.default_rng(seed)
        self.drift = {v: 0.0 for v in VITALS}
        self.scenario: Scenario | None = None
        self.onset: float | None = None
        self.artifact_until = 0.0  # finger-off / motion artifact window (sim seconds)
        self.last_t: float | None = None

    # --- scenario control -------------------------------------------------
    def start_scenario(self, name: str, t: float) -> None:
        self.scenario = SCENARIOS[name]
        self.onset = t

    def clear_scenario(self) -> None:
        self.scenario = None
        self.onset = None

    def progress(self, t: float) -> float:
        return _progress(t, self.onset, self.scenario)

    def inject_artifact(self, t: float, minutes: float = 15) -> None:
        self.artifact_until = t + minutes * 60

    # --- sampling ----------------------------------------------------------
    def sample(self, t: float) -> dict:
        """Return one bedside reading at sim time t (unix seconds)."""
        dt_min = SAMPLE_MINUTES if self.last_t is None else max(0.5, (t - self.last_t) / 60.0)
        self.last_t = t
        # slow AR(1) drift: individual day-to-day variation
        a = math.exp(-dt_min / 240.0)
        for v in VITALS:
            self.drift[v] = a * self.drift[v] + math.sqrt(1 - a * a) * self.rng.normal(0, DRIFT_SD[v])

        hour = (t / 3600.0) % 24
        circ = math.cos((hour - 4) / 24 * 2 * math.pi)  # ~ -1 at 4pm, +1 at 4am
        night = 1.0 if (hour >= 22 or hour < 6) else 0.0

        f = self.progress(t)
        eff = self.scenario.effects if self.scenario else {}
        b = self.p.baseline
        out = {}
        out["hr"] = b["hr"] - 5 * night - 2 * circ + self.drift["hr"] + f * eff.get("hr", 0) + self.rng.normal(0, NOISE_SD["hr"])
        out["rr"] = b["rr"] - 0.8 * night + self.drift["rr"] + f * eff.get("rr", 0) + self.rng.normal(0, NOISE_SD["rr"])
        out["spo2"] = b["spo2"] + 0.3 * self.drift["spo2"] + f * eff.get("spo2", 0) + self.rng.normal(0, NOISE_SD["spo2"])
        out["sbp"] = b["sbp"] - 6 * night + self.drift["sbp"] + f * eff.get("sbp", 0) + self.rng.normal(0, NOISE_SD["sbp"])
        out["dbp"] = b["dbp"] - 4 * night + self.drift["dbp"] + f * eff.get("dbp", 0) + self.rng.normal(0, NOISE_SD["dbp"])
        out["temp"] = b["temp"] - 0.25 * circ + self.drift["temp"] + f * eff.get("temp", 0) + self.rng.normal(0, NOISE_SD["temp"])
        # weight: smart scale, effectively once a day but we report a smoothed value
        out["weight"] = b.get("weight", 70) + f * eff.get("weight", 0) + self.rng.normal(0, 0.15)

        # fever / infection pushes HR (temperature-pulse coupling)
        out["hr"] += max(0.0, out["temp"] - 37.2) * 6 * (1 if self.scenario and self.scenario.name == "sepsis" else 0.3)

        sqi = float(np.clip(self.rng.normal(0.93, 0.04), 0.6, 1.0))
        motion = float(np.clip(abs(self.rng.normal(0.08, 0.08)), 0, 1))
        if t < self.artifact_until:
            # probe displaced / patient moving: SpO2 plunges, HR erratic, poor signal quality
            sqi = float(self.rng.uniform(0.12, 0.35))
            motion = float(self.rng.uniform(0.6, 0.95))
            out["spo2"] = self.rng.uniform(68, 82)
            out["hr"] = out["hr"] + self.rng.normal(0, 25)

        out["hr"] = int(round(np.clip(out["hr"], 25, 220)))
        out["rr"] = int(round(np.clip(out["rr"], 5, 50)))
        out["spo2"] = int(round(np.clip(out["spo2"], 50, 100)))
        out["sbp"] = int(round(np.clip(out["sbp"], 60, 230)))
        out["dbp"] = int(round(np.clip(min(out["dbp"], out["sbp"] - 15), 30, 140)))
        out["temp"] = round(float(np.clip(out["temp"], 33.0, 42.5)), 1)
        out["weight"] = round(float(out["weight"]), 1)
        out["sqi"] = round(sqi, 2)
        out["motion"] = round(motion, 2)
        out["on_oxygen"] = self.p.on_oxygen
        sc = self.scenario
        out["acvpu"] = "C" if (sc and sc.confusion_at is not None and f >= sc.confusion_at) else "A"
        return out


def random_profile(rng: np.random.Generator, idx: int) -> PatientProfile:
    """Random synthetic patient for the training cohort."""
    copd = rng.random() < 0.25
    base = {
        "hr": float(rng.normal(80, 8)),
        "rr": float(rng.normal(18.5 if copd else 16.5, 1.3)),
        "spo2": float(rng.normal(90, 1.0) if copd else min(99, rng.normal(96, 1.2))),
        "sbp": float(rng.normal(128, 12)),
        "dbp": float(rng.normal(76, 7)),
        "temp": float(rng.normal(36.8, 0.2)),
        "weight": float(rng.normal(68, 10)),
    }
    return PatientProfile(
        f"T-{idx}", f"train-{idx}", int(rng.integers(50, 90)), "X", "synthetic",
        base, on_oxygen=copd and rng.random() < 0.7, spo2_scale=2 if copd else 1,
    )
