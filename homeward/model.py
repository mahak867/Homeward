"""24-hour deterioration predictor + circuit breaker.

Model: L2-regularised logistic regression (numpy only). Chosen deliberately over a
black-box ensemble: every prediction decomposes EXACTLY into per-feature contributions
(coef x standardised value), so the explanation shown to the caregiver is the model,
not an approximation of it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .features import FEATURES, describe

MODEL_PATH = Path(__file__).with_name("model.json")


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logreg_signed(X: np.ndarray, y: np.ndarray, signs: np.ndarray, l2: float = 1.0):
    """Logistic regression where every coefficient must point in the clinically expected
    direction (e.g. rising breathing rate can only ADD risk). Features whose fitted sign
    contradicts physiology (usually because of collinearity) are dropped and the model refit.
    Result: no explanation ever says 'fever is reassuring'."""
    active = np.ones(X.shape[1], bool)
    for _ in range(X.shape[1]):
        m = fit_logreg(X[:, active], y, l2)
        coef = np.zeros(X.shape[1]); coef[active] = m["coef"]
        bad = active & (np.sign(coef) * signs < 0)
        if not bad.any():
            break
        worst = np.argmax(np.where(bad, np.abs(coef), -1))
        active[worst] = False
    mu, sd = X.mean(0), X.std(0) + 1e-9
    full = {"intercept": m["intercept"], "coef": coef.tolist(), "mean": mu.tolist(), "std": sd.tolist()}
    # re-express intercept for the full standardisation (inactive features have coef 0)
    full["mean"] = [float(v) for v in mu]
    full["std"] = [float(v) for v in sd]
    full["dropped"] = [int(i) for i in np.where(~active)[0]]
    return full


def fit_logreg(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 30):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])
    w = np.zeros(Z.shape[1])
    R = l2 * np.eye(Z.shape[1]); R[0, 0] = 0
    for _ in range(iters):  # Newton / IRLS
        p = sigmoid(Z @ w)
        g = Z.T @ (p - y) + R @ w
        H = (Z * (p * (1 - p))[:, None]).T @ Z + R
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return {"intercept": float(w[0]), "coef": w[1:].tolist(), "mean": mu.tolist(), "std": sd.tolist()}


class Predictor:
    def __init__(self, path: Path = MODEL_PATH):
        m = json.loads(Path(path).read_text())
        self.meta = m
        self.b = m["intercept"]
        self.coef = np.array(m["coef"])
        self.mu = np.array(m["mean"])
        self.sd = np.array(m["std"])
        self.threshold = m["threshold"]

    def predict(self, x: np.ndarray, ctx: dict | None = None) -> dict:
        z = (x - self.mu) / self.sd
        contrib = self.coef * z
        p = float(sigmoid(self.b + contrib.sum()))
        order = np.argsort(-contrib)
        factors, seen = [], set()
        for i in order:  # one line per vital sign (level OR trend, whichever matters most)
            if contrib[i] <= 0.15 or len(factors) >= 4:
                break
            f = FEATURES[i]
            if f.split("_")[-1] in seen:
                continue
            seen.add(f.split("_")[-1])
            factors.append({"feature": f, "impact": round(float(contrib[i]), 2),
                            "text": describe(f, ctx) if ctx else f})
        reassuring, seen = [], set()
        for i in order[::-1]:
            if contrib[i] >= -0.3 or len(reassuring) >= 2:
                break
            f = FEATURES[i]
            if f.split("_")[-1] in seen or f in ("note_concern", "news2_now", "weight_delta"):
                continue
            seen.add(f.split("_")[-1])
            reassuring.append({"feature": f, "impact": round(float(contrib[i]), 2),
                               "text": describe(f, ctx) if ctx else f})
        return {"prob": round(p, 3), "threshold": self.threshold, "factors": factors,
                "reassuring": reassuring, "source": "ml"}


class CircuitBreaker:
    """If the ML component fails repeatedly, stop calling it for a cool-down period and
    run on the rule-based fallback. NEWS2 safety rules are never affected."""

    def __init__(self, fail_threshold: int = 3, cooldown_s: float = 15.0):
        self.fail_threshold, self.cooldown_s = fail_threshold, cooldown_s
        self.failures = 0
        self.opened_at: float | None = None
        self.total_failures = 0
        self.fallback_count = 0

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if time.time() - self.opened_at >= self.cooldown_s else "open"

    def call(self, fn, fallback):
        if self.state == "open":
            self.fallback_count += 1
            return fallback()
        try:
            out = fn()
            self.failures, self.opened_at = 0, None
            return out
        except Exception:
            self.failures += 1
            self.total_failures += 1
            if self.failures >= self.fail_threshold or self.state == "half-open":
                self.opened_at = time.time()
            self.fallback_count += 1
            return fallback()


def rule_fallback(ctx: dict) -> dict:
    """Transparent trend heuristic used when ML is unavailable."""
    s = ctx["slopes6"]
    score = (max(0, s["rr"]) * 6 / 3 + max(0, s["hr"]) * 6 / 10 + max(0, s["temp"]) * 6 / 0.5
             + max(0, -s["spo2"]) * 6 / 2 + max(0, -s["sbp"]) * 6 / 15)
    p = float(min(0.95, score / 4))
    factors = []
    for v, sign in (("rr", 1), ("hr", 1), ("temp", 1), ("spo2", -1), ("sbp", -1)):
        if sign * s[v] > 0:
            factors.append({"feature": f"slope6_{v}", "impact": None, "text": describe(f"slope6_{v}", ctx)})
    return {"prob": round(p, 3), "threshold": 0.5, "factors": factors[:3], "reassuring": [],
            "source": "fallback-rules"}
