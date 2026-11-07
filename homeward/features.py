"""Feature extraction shared by training and live inference.

The idea, borrowed from personalised remote-monitoring products: a patient is compared
first with THEMSELVES (a personal baseline learned in their first 24 h at home), then
with population thresholds (NEWS2). Slow drifts - breathing rate creeping up, temperature
rising, BP sagging - are what show up 24-48 h before an obvious emergency.
"""
from __future__ import annotations

import numpy as np

from . import news2

TREND_VITALS = ("hr", "rr", "spo2", "sbp", "temp")
SCALE = {"hr": 10.0, "rr": 3.0, "spo2": 2.0, "sbp": 15.0, "temp": 0.5}
UNITS = {"hr": "bpm", "rr": "/min", "spo2": "%", "sbp": "mmHg", "temp": "°C"}
NAMES = {"hr": "Heart rate", "rr": "Breathing rate", "spo2": "Oxygen level", "sbp": "Systolic BP", "temp": "Temperature"}

FEATURES = (
    [f"dev_{v}" for v in TREND_VITALS]
    + [f"slope6_{v}" for v in TREND_VITALS]
    + [f"slope12_{v}" for v in TREND_VITALS]
    + ["weight_delta", "news2_now", "note_concern"]
)
CALIBRATION_H = 24

# clinically expected direction of each feature's effect on deterioration risk
_DIR = {"hr": 1, "rr": 1, "spo2": -1, "sbp": -1, "temp": 1}
SIGNS = ([_DIR[v] for v in TREND_VITALS] * 3) + [1, 1, 1]


def personal_baseline(t: np.ndarray, vals: dict, fallback: dict) -> dict:
    """Median of the first 24 h of good readings (calibration period)."""
    if len(t) == 0:
        return dict(fallback)
    m = t <= t[0] + CALIBRATION_H * 3600
    out = {}
    for v in list(TREND_VITALS) + ["weight"]:
        arr = vals.get(v)
        if arr is not None and m.sum() >= 24:
            out[v] = float(np.median(arr[m]))
        else:
            out[v] = float(fallback.get(v, np.nan))
    return out


def _slope_per_hour(tt: np.ndarray, yy: np.ndarray) -> float:
    if len(tt) < 6:
        return 0.0
    x = (tt - tt.mean()) / 3600.0
    d = (x * x).sum()
    return float((x * (yy - yy.mean())).sum() / d) if d > 0 else 0.0


def compute(t: np.ndarray, vals: dict, i_end: int, baseline: dict, *, spo2_scale: int, on_oxygen: bool,
            acvpu: str = "A", note_concern: float = 0.0) -> tuple[np.ndarray, dict]:
    """Features at index i_end (inclusive) of time-sorted good-quality readings."""
    now = t[i_end]
    j30 = np.searchsorted(t, now - 30 * 60, side="left")
    j6 = np.searchsorted(t, now - 6 * 3600, side="left")
    j12 = np.searchsorted(t, now - 12 * 3600, side="left")
    sl = slice(j30, i_end + 1)
    cur = {v: float(np.median(vals[v][sl])) for v in TREND_VITALS}
    feats, ctx = {}, {"current": cur, "baseline": baseline, "slopes6": {}, "slopes12": {}}
    for v in TREND_VITALS:
        feats[f"dev_{v}"] = (cur[v] - baseline[v]) / SCALE[v]
        s6 = _slope_per_hour(t[j6:i_end + 1], vals[v][j6:i_end + 1])
        s12 = _slope_per_hour(t[j12:i_end + 1], vals[v][j12:i_end + 1])
        ctx["slopes6"][v], ctx["slopes12"][v] = s6, s12
        feats[f"slope6_{v}"] = s6 / SCALE[v] * 6  # change over 6h in "scale units"
        feats[f"slope12_{v}"] = s12 / SCALE[v] * 12
    w = vals.get("weight")
    cw = float(np.median(w[sl])) if w is not None else baseline.get("weight", 0)
    feats["weight_delta"] = cw - baseline.get("weight", cw) if not np.isnan(baseline.get("weight", np.nan)) else 0.0
    ctx["current"]["weight"] = cw
    s = news2.score({**{k: cur[k] for k in ("hr", "rr", "spo2", "sbp", "temp")},
                     "on_oxygen": on_oxygen, "acvpu": acvpu}, spo2_scale)
    feats["news2_now"] = float(s["total"])
    ctx["news2_now"] = s["total"]
    feats["note_concern"] = float(note_concern)
    x = np.array([feats[f] for f in FEATURES], dtype=float)
    return x, ctx


def describe(feature: str, ctx: dict) -> str:
    """Plain-language sentence for a feature's contribution."""
    cur, base = ctx["current"], ctx["baseline"]
    if feature.startswith("dev_"):
        v = feature[4:]
        d = cur[v] - base[v]
        fmt = "{:.1f}" if v == "temp" else "{:.0f}"
        return (f"{NAMES[v]} {fmt.format(cur[v])} {UNITS[v]} vs this patient's usual {fmt.format(base[v])} "
                f"({'+' if d >= 0 else ''}{fmt.format(d)})")
    if feature.startswith("slope"):
        hrs = 6 if feature.startswith("slope6") else 12
        v = feature.split("_", 1)[1]
        s = ctx["slopes6" if hrs == 6 else "slopes12"][v]
        total = s * hrs
        fmt = "{:+.1f}" if v in ("temp",) else "{:+.0f}"
        trend = "rising" if total > 0 else "falling"
        return f"{NAMES[v]} {trend} {fmt.format(total)} {UNITS[v]} over the last {hrs} h"
    if feature == "weight_delta":
        d = cur.get("weight", 0) - base.get("weight", 0)
        return f"Body weight {d:+.1f} kg vs baseline (possible fluid build-up)" if d > 0 else f"Body weight {d:+.1f} kg vs baseline"
    if feature == "news2_now":
        return f"NEWS2 already {ctx.get('news2_now', '?')} - several vital signs outside normal ranges"
    if feature == "note_concern":
        return "Concerns described in caregiver notes"
    return feature
