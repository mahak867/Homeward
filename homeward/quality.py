"""Signal-quality gate: stop sensor artifacts from becoming alarms.

Alarm fatigue is one of the most studied failure modes of monitoring systems: most
bedside alarms are false or non-actionable, and caregivers learn to ignore them.
A home caregiver is not a trained nurse, so every false alarm costs trust.

Rules (all transparent):
  * physiologically impossible values are rejected outright;
  * low signal-quality index (SQI) -> artifact (probe displaced / poor perfusion);
  * a big SpO2 drop together with high motion -> artifact;
  * a red-zone value must be CONFIRMED by a second consecutive good reading before
    the system escalates to CRITICAL (handled in risk.py).
"""
from __future__ import annotations

PLAUSIBLE = {"hr": (25, 220), "rr": (4, 50), "spo2": (50, 100), "sbp": (50, 250), "temp": (30.0, 43.0)}


def assess(r: dict, prev_good: dict | None = None) -> dict:
    issues = []
    for k, (lo, hi) in PLAUSIBLE.items():
        v = r.get(k)
        if v is None:
            issues.append(f"{k} missing")
        elif not (lo <= v <= hi):
            issues.append(f"{k}={v} is physiologically implausible")
    if issues:
        return {"ok": False, "artifact": True, "kind": "invalid", "issues": issues}
    sqi = r.get("sqi", 1.0)
    motion = r.get("motion", 0.0)
    if sqi < 0.4:
        return {"ok": False, "artifact": True, "kind": "low_signal",
                "issues": [f"Poor sensor signal (quality {sqi:.2f}) - probe may be displaced or patient moving"]}
    if prev_good is not None and motion > 0.6 and prev_good["spo2"] - r["spo2"] >= 6:
        return {"ok": False, "artifact": True, "kind": "motion",
                "issues": [f"SpO2 fell {prev_good['spo2'] - r['spo2']}% during heavy movement - likely motion artifact"]}
    return {"ok": True, "artifact": False, "kind": None, "issues": []}
