"""NEWS2 (National Early Warning Score 2, Royal College of Physicians, 2017).

NEWS2 is a published, widely-used track-and-trigger score. We use it as the
transparent "safety floor" of the risk engine: the ML model can raise concern
earlier, but can never lower what NEWS2 says.

Includes SpO2 Scale 2 for patients with hypercapnic respiratory failure (e.g. COPD
with a prescribed target of 88-92%), which prevents the constant false alarms a
standard threshold would raise for these patients.
"""
from __future__ import annotations


def _rr(v):
    if v <= 8: return 3
    if v <= 11: return 1
    if v <= 20: return 0
    if v <= 24: return 2
    return 3


def _spo2_scale1(v):
    if v <= 91: return 3
    if v <= 93: return 2
    if v <= 95: return 1
    return 0


def _spo2_scale2(v, on_oxygen):
    if v <= 83: return 3
    if v <= 85: return 2
    if v <= 87: return 1
    if v <= 92: return 0
    if not on_oxygen: return 0  # >=93 on air
    if v <= 94: return 1
    if v <= 96: return 2
    return 3


def _sbp(v):
    if v <= 90: return 3
    if v <= 100: return 2
    if v <= 110: return 1
    if v <= 219: return 0
    return 3


def _hr(v):
    if v <= 40: return 3
    if v <= 50: return 1
    if v <= 90: return 0
    if v <= 110: return 1
    if v <= 130: return 2
    return 3


def _temp(v):
    if v <= 35.0: return 3
    if v <= 36.0: return 1
    if v <= 38.0: return 0
    if v <= 39.0: return 1
    return 2


LABELS = {
    "rr": "Respiration rate",
    "spo2": "Oxygen saturation",
    "o2": "On supplemental oxygen",
    "sbp": "Systolic BP",
    "hr": "Pulse",
    "acvpu": "Consciousness",
    "temp": "Temperature",
}


def score(r: dict, spo2_scale: int = 1) -> dict:
    """Score one reading. r needs rr, spo2, sbp, hr, temp, on_oxygen, acvpu."""
    on_o2 = bool(r.get("on_oxygen"))
    comp = {
        "rr": _rr(round(r["rr"])),
        "spo2": _spo2_scale2(round(r["spo2"]), on_o2) if spo2_scale == 2 else _spo2_scale1(round(r["spo2"])),
        "o2": 2 if on_o2 else 0,
        "sbp": _sbp(round(r["sbp"])),
        "hr": _hr(round(r["hr"])),
        "acvpu": 0 if r.get("acvpu", "A") == "A" else 3,
        "temp": _temp(round(float(r["temp"]), 1)),
    }
    total = sum(comp.values())
    red = [k for k, v in comp.items() if v == 3]
    if total >= 7:
        band, response = "high", "Emergency response: urgent clinical assessment now (call emergency / clinician)."
    elif total >= 5:
        band, response = "medium", "Urgent response: clinician review within the hour."
    elif red:
        band, response = "low-medium", "Single parameter in red zone: prompt clinician review."
    else:
        band, response = "low", "Continue routine monitoring."
    return {"total": total, "components": comp, "red_params": red, "band": band,
            "response": response, "scale": spo2_scale}


def explain_components(r: dict, s: dict) -> list[dict]:
    rows = []
    vals = {"rr": f"{round(r['rr'])} /min", "spo2": f"{round(r['spo2'])} %", "o2": "yes" if r.get("on_oxygen") else "no",
            "sbp": f"{round(r['sbp'])} mmHg", "hr": f"{round(r['hr'])} bpm", "acvpu": r.get("acvpu", "A"),
            "temp": f"{float(r['temp']):.1f} °C"}
    for k, pts in s["components"].items():
        rows.append({"param": k, "label": LABELS[k], "value": vals[k], "points": pts})
    return rows
