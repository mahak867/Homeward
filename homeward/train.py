"""Train and honestly evaluate the 24 h deterioration predictor on a synthetic cohort.

    python -m homeward.train            # writes homeward/model.json + docs/model_metrics.json

Evaluation is patient-level hold-out (no patient appears in both train and test) and
reports event-level numbers a clinician would ask about: how many deteriorations were
flagged, how many hours early, and how many false early-warnings per stable patient-day,
side by side with the NEWS2-only baseline.

ALL DATA IS SYNTHETIC. These numbers describe how well the model learns our simulator,
not clinical accuracy. That caveat is printed in the model card and in the UI.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from . import news2, quality
from .features import FEATURES, SIGNS, compute, personal_baseline
from .model import MODEL_PATH, fit_logreg_signed, sigmoid
from .physiology import SAMPLE_MINUTES, PatientPhysiology, random_profile

HOURS = 96
STEP = SAMPLE_MINUTES * 60
EVAL_EVERY_H = 1
TARGET_FA = 0.15  # false early-warnings per stable patient-day
EARLY_WINDOW_H = 48


def persistent(flags, k=2):
    """True only where the last k evaluations were all positive (live system uses the same rule)."""
    flags = np.asarray(flags, bool)
    out = flags.copy()
    for j in range(1, k):
        out[j:] &= flags[:-j]
        out[:j] = False
    return out


def simulate(idx: int, rng: np.random.Generator) -> dict:
    prof = random_profile(rng, idx)
    phys = PatientPhysiology(prof, seed=int(rng.integers(1 << 31)))
    t0 = 1_700_000_000.0
    times = t0 + np.arange(0, HOURS * 3600, STEP)
    r = rng.random()
    choices = ["sepsis", "hf_decompensation", "acute_hypoxia"] + (["copd_exacerbation"] * 2 if prof.spo2_scale == 2 else [])
    scenario = None if r < 0.45 else str(rng.choice(choices))
    onset = t0 + rng.uniform(26, 70) * 3600 if scenario else None
    transient = None
    if scenario is None and rng.random() < 0.35:  # benign confounder: mild self-limiting upset
        ts = t0 + rng.uniform(26, 80) * 3600
        transient = (ts, ts + rng.uniform(4, 9) * 3600, str(rng.choice(["sepsis", "hf_decompensation"])))
    artifacts = t0 + rng.uniform(0, HOURS, size=int(rng.integers(0, 6))) * 3600
    gaps = [(g, g + rng.uniform(0.5, 3) * 3600) for g in t0 + rng.uniform(0, HOURS, size=int(rng.integers(0, 3))) * 3600]
    # caregiver notes
    note_at, note_w = None, 0.0
    if scenario and rng.random() < 0.5:
        note_at = onset + rng.uniform(0.3, 0.75) * phys_duration(scenario)
        note_w = float(rng.choice([0.6, 1.0, 1.5]))
    elif scenario is None and rng.random() < 0.15:
        note_at, note_w = t0 + rng.uniform(24, 90) * 3600, float(rng.uniform(0.4, 0.8))

    rows = []
    for t in times:
        if scenario and phys.scenario is None and t >= onset:
            phys.start_scenario(scenario, onset)
        if transient:
            if transient[0] <= t < transient[1] and phys.scenario is None:
                phys.start_scenario(transient[2], transient[0])
            elif t >= transient[1] and phys.scenario is not None:
                phys.clear_scenario()
        if any(abs(t - a) < 600 for a in artifacts):
            phys.inject_artifact(t, 10)
        s = phys.sample(t)
        if any(a <= t < b for a, b in gaps):
            continue
        s["t"] = t
        rows.append(s)
    return {"profile": prof, "rows": rows, "scenario": scenario, "onset": onset,
            "note_at": note_at, "note_w": note_w}


def phys_duration(name):
    from .physiology import SCENARIOS
    return SCENARIOS[name].duration_h * 3600


def prepare(sim: dict):
    prof = sim["profile"]
    good, prev = [], None
    for r in sim["rows"]:
        q = quality.assess(r, prev)
        if q["ok"]:
            good.append(r); prev = r
    t = np.array([r["t"] for r in good])
    vals = {k: np.array([r[k] for r in good], dtype=float) for k in ("hr", "rr", "spo2", "sbp", "temp", "weight")}
    n2 = np.array([news2.score(r, prof.spo2_scale)["total"] for r in good])
    sustained = (n2[1:] >= 5) & (n2[:-1] >= 5)
    ev_idx = np.argmax(sustained) + 1 if sustained.any() else None
    event_t = float(t[ev_idx]) if ev_idx is not None else None
    n3 = (n2[1:] >= 3) & (n2[:-1] >= 3)
    base = personal_baseline(t, vals, prof.baseline)

    def note_conc(tt):
        na = sim["note_at"]
        return sim["note_w"] if (na is not None and na <= tt <= na + 12 * 3600) else 0.0

    X, y, ts = [], [], []
    start = t[0] + 30 * 3600
    for tt in np.arange(start, t[-1], EVAL_EVERY_H * 3600):
        i = int(np.searchsorted(t, tt, side="right") - 1)
        if i < 10 or t[i] < tt - 1800:
            continue
        acv = good[i]["acvpu"]
        x, _ = compute(t, vals, i, base, spo2_scale=prof.spo2_scale, on_oxygen=prof.on_oxygen,
                       acvpu=acv, note_concern=note_conc(tt))
        X.append(x); ts.append(tt)
        y.append(1 if (event_t is not None and tt < event_t <= tt + 24 * 3600) else 0)
    return {"X": np.array(X), "y": np.array(y), "t": np.array(ts), "event_t": event_t,
            "n3_t": float(t[np.argmax(n3) + 1]) if n3.any() else None, "end": float(t[-1]),
            "scenario": sim["scenario"], "n2": n2, "tt": t}


def auroc(y, p):
    order = np.argsort(p)
    ranks = np.empty(len(p)); ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    n1, n0 = pos.sum(), (~pos).sum()
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def episodes(times, flags, refractory_h=12):
    """count rising edges of an alert signal, with a refractory period (deduplication)"""
    out, last = [], -1e18
    prev = False
    for tt, f in zip(times, flags):
        if f and not prev and tt - last > refractory_h * 3600:
            out.append(tt); last = tt
        prev = f
    return out


def main(n_patients: int = 900, seed: int = 7):
    t0 = time.time()
    rng = np.random.default_rng(seed)
    data = [prepare(simulate(i, rng)) for i in range(n_patients)]
    data = [d for d in data if len(d["X"])]
    idx = rng.permutation(len(data))
    n_tr = int(0.7 * len(data))
    tr, te = [data[i] for i in idx[:n_tr]], [data[i] for i in idx[n_tr:]]
    Xtr = np.vstack([d["X"][d["t"] < (d["event_t"] or 1e18)] for d in tr])
    ytr = np.concatenate([d["y"][d["t"] < (d["event_t"] or 1e18)] for d in tr])
    m = fit_logreg_signed(Xtr, ytr, np.array(SIGNS, float), l2=2.0)
    coef, b, mu, sd = np.array(m["coef"]), m["intercept"], np.array(m["mean"]), np.array(m["std"])
    prob = lambda X: sigmoid(b + ((X - mu) / sd) @ coef)

    # threshold: lowest threshold whose false early-warning rate on STABLE training patients
    # stays <= TARGET_FA per patient-day (alarm-fatigue budget), using the same
    # 2-consecutive-hours persistence rule the live system applies.
    def fa_rate(ds, thr):
        n, days = 0, 0.0
        for d in ds:
            if d["event_t"] is None:
                days += (d["end"] - d["t"][0]) / 86400
                n += len(episodes(d["t"], persistent(prob(d["X"]) >= thr)))
        return n / max(days, 1e-9)
    thr = 0.95
    for cand in np.arange(0.10, 0.95, 0.01):
        if fa_rate(tr, cand) <= TARGET_FA:
            thr = float(cand); break

    # ---- held-out evaluation
    Xte = np.vstack([d["X"][d["t"] < (d["event_t"] or 1e18)] for d in te])
    yte = np.concatenate([d["y"][d["t"] < (d["event_t"] or 1e18)] for d in te])
    pte = prob(Xte)
    res = {"auroc_sample_level": round(auroc(yte, pte), 3)}
    det_ml, det_n3, lead_ml, lead_n3 = 0, 0, [], []
    fa_ml, fa_n3, stable_days = 0, 0, 0.0
    n_event = 0
    per_scn = {}
    for d in te:
        p = prob(d["X"])
        if d["event_t"] is not None and d["event_t"] > d["t"][0]:
            n_event += 1
            pre = (d["t"] < d["event_t"]) & (d["t"] >= d["event_t"] - EARLY_WINDOW_H * 3600)
            hits = d["t"][pre][persistent(p >= thr)[pre]]
            sc = per_scn.setdefault(d["scenario"] or "unprompted", {"n": 0, "flagged_early": 0, "lead_h": []})
            sc["n"] += 1
            if len(hits):
                det_ml += 1; L = (d["event_t"] - hits[0]) / 3600; lead_ml.append(L)
                sc["flagged_early"] += 1; sc["lead_h"].append(L)
            if d["n3_t"] is not None and d["event_t"] - EARLY_WINDOW_H * 3600 <= d["n3_t"] < d["event_t"]:
                det_n3 += 1; lead_n3.append((d["event_t"] - d["n3_t"]) / 3600)
        elif d["event_t"] is None:
            days = (d["end"] - d["t"][0]) / 86400
            stable_days += days
            fa_ml += len(episodes(d["t"], persistent(p >= thr)))
            # NEWS2>=3 sustained episodes over the same period
            tt, n2 = d["tt"], d["n2"]
            msk = tt >= d["t"][0]
            flags = np.r_[False, (n2[1:] >= 3) & (n2[:-1] >= 3)][msk]
            fa_n3 += len(episodes(tt[msk], flags))
    res.update({
        "test_patients": len(te), "test_deteriorations": n_event,
        "ml_flagged_before_news2_5": f"{det_ml}/{n_event}",
        "ml_median_lead_h": round(float(np.median(lead_ml)), 1) if lead_ml else None,
        "news2_ge3_flagged_before_news2_5": f"{det_n3}/{n_event}",
        "news2_ge3_median_lead_h": round(float(np.median(lead_n3)), 1) if lead_n3 else None,
        "ml_false_early_warnings_per_stable_patient_day": round(fa_ml / max(stable_days, 1e-9), 3),
        "news2_ge3_alerts_per_stable_patient_day": round(fa_n3 / max(stable_days, 1e-9), 3),
        "per_scenario": {k: {"n": v["n"], "flagged_early": v["flagged_early"],
                             "median_lead_h": round(float(np.median(v["lead_h"])), 1) if v["lead_h"] else None}
                         for k, v in per_scn.items()},
    })
    model = {**m, "features": FEATURES, "threshold": round(thr, 3),
             "trained": time.strftime("%Y-%m-%d"), "n_train_samples": int(len(ytr)),
             "positive_rate": round(float(ytr.mean()), 3), "metrics": res,
             "caveat": "Synthetic data only. Not clinically validated."}
    MODEL_PATH.write_text(json.dumps(model, indent=1))
    out = Path(__file__).resolve().parent.parent / "docs" / "model_metrics.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"threshold": model["threshold"], "metrics": res,
                               "dropped_for_wrong_sign": [FEATURES[i] for i in m.get("dropped", [])],
                               "coefficients": dict(zip(FEATURES, [round(c, 3) for c in m["coef"]]))}, indent=1))
    print(json.dumps(res, indent=1))
    print(f"threshold={thr:.3f}  trained in {time.time() - t0:.1f}s on {len(ytr)} samples")


if __name__ == "__main__":
    main()
