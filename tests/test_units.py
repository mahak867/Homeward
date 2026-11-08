"""Unit tests: clinical scoring boundaries, multilingual notes, quality gate, model guarantees,
circuit breaker, feature maths and auth internals."""
import math
import time

import numpy as np
import pytest

from homeward import news2, notes, quality
from homeward.auth import Auth
from homeward.features import FEATURES, SIGNS, _slope_per_hour, compute, personal_baseline
from homeward.model import CircuitBreaker, Predictor, rule_fallback, sigmoid

BASE = {"rr": 16, "spo2": 97, "on_oxygen": False, "sbp": 125, "hr": 72, "acvpu": "A", "temp": 36.8}


def comp(param, value, scale=1, **extra):
    return news2.score({**BASE, param: value, **extra}, scale)["components"][param]


# ----------------------------------------------------------------- NEWS2: every threshold boundary (RCP 2017)
@pytest.mark.parametrize("v,pts", [(8, 3), (9, 1), (11, 1), (12, 0), (20, 0), (21, 2), (24, 2), (25, 3)])
def test_news2_respiration_boundaries(v, pts):
    assert comp("rr", v) == pts


@pytest.mark.parametrize("v,pts", [(91, 3), (92, 2), (93, 2), (94, 1), (95, 1), (96, 0), (100, 0)])
def test_news2_spo2_scale1_boundaries(v, pts):
    assert comp("spo2", v) == pts


@pytest.mark.parametrize("v,o2,pts", [(83, False, 3), (84, False, 2), (85, False, 2), (86, False, 1), (87, False, 1),
                                      (88, False, 0), (92, False, 0), (93, False, 0), (97, False, 0),
                                      (93, True, 1), (94, True, 1), (95, True, 2), (96, True, 2), (97, True, 3)])
def test_news2_spo2_scale2_boundaries(v, o2, pts):
    assert comp("spo2", v, scale=2, on_oxygen=o2) == pts


@pytest.mark.parametrize("v,pts", [(90, 3), (91, 2), (100, 2), (101, 1), (110, 1), (111, 0), (219, 0), (220, 3)])
def test_news2_systolic_boundaries(v, pts):
    assert comp("sbp", v) == pts


@pytest.mark.parametrize("v,pts", [(40, 3), (41, 1), (50, 1), (51, 0), (90, 0), (91, 1), (110, 1), (111, 2), (130, 2), (131, 3)])
def test_news2_pulse_boundaries(v, pts):
    assert comp("hr", v) == pts


@pytest.mark.parametrize("v,pts", [(35.0, 3), (35.1, 1), (36.0, 1), (36.1, 0), (38.0, 0), (38.1, 1), (39.0, 1), (39.1, 2)])
def test_news2_temperature_boundaries(v, pts):
    assert comp("temp", v) == pts


@pytest.mark.parametrize("acvpu,pts", [("A", 0), ("C", 3), ("V", 3), ("P", 3), ("U", 3)])
def test_news2_consciousness(acvpu, pts):
    assert comp("acvpu", acvpu) == pts


def test_news2_supplemental_oxygen_adds_two():
    assert news2.score({**BASE, "on_oxygen": True})["components"]["o2"] == 2


@pytest.mark.parametrize("vitals,band", [
    ({}, "low"), ({"rr": 21}, "low"), ({"sbp": 90}, "low-medium"),
    ({"rr": 21, "hr": 111, "temp": 38.1}, "medium"), ({"rr": 25, "spo2": 91, "hr": 131}, "high")])
def test_news2_bands_and_response(vitals, band):
    s = news2.score({**BASE, **vitals})
    assert s["band"] == band and s["response"]


def test_news2_explanation_rows_cover_all_parameters():
    s = news2.score(BASE)
    rows = news2.explain_components(BASE, s)
    assert {r["param"] for r in rows} == {"rr", "spo2", "o2", "sbp", "hr", "acvpu", "temp"}
    assert sum(r["points"] for r in rows) == s["total"]


# ----------------------------------------------------------------- notes: 5 languages, negation, red flags
@pytest.mark.parametrize("text,found,neg,lang", [
    ("She is breathless and has a fever", {"breathless", "fever"}, set(), "English"),
    ("No fever, but very drowsy today", {"confusion"}, {"fever"}, "English"),
    ("Ankles more swollen, less urine", {"swelling", "urine"}, set(), "English"),
    ("Saans phool rahi hai, bukhar nahi hai", {"breathless"}, {"fever"}, "Hinglish"),
    ("Papa gir gaye bathroom mein", {"fall"}, set(), "Hinglish"),
    ("Khana nahi khaya, ulti hui", {"intake"}, set(), "Hinglish"),
    ("बुखार है और चक्कर आ रहे हैं", {"fever", "dizzy"}, set(), "Hindi"),
    ("सीने में दर्द हो रहा है", {"chest_pain"}, set(), "Hindi"),
    ("बुखार नहीं है", set(), {"fever"}, "Hindi"),
    ("Amma ku kaaichal illai, aanaa moochu thinaral irukku", {"breathless"}, {"fever"}, "Tanglish"),
    ("Appa vizhundhutaar, nenju vali irukku", {"fall", "chest_pain"}, set(), "Tanglish"),
    ("Irumal and sali increased, saapidala", {"cough", "intake"}, set(), "Tanglish"),
    ("நெஞ்சு வலி இருக்கு", {"chest_pain"}, set(), "Tamil"),
    ("அம்மாவுக்கு காய்ச்சல் இல்லை", set(), {"fever"}, "Tamil"),
    ("கால் வீக்கம், மூச்சு திணறல்", {"swelling", "breathless"}, set(), "Tamil"),
])
def test_notes_multilingual(text, found, neg, lang):
    a = notes.analyse(text)
    assert {s["category"] for s in a["signals"] if s["weight"] > 0} == found
    assert {s["category"] for s in a["negated"]} == neg
    assert lang in a["languages"]


@pytest.mark.parametrize("text,found", [
    ("saans nahi aa rahi", {"breathless"}),                  # "can't breathe" - must NOT be read as negated
    ("Maa ki saans nahi aa rahi hai", {"breathless"}),
    ("सांस नहीं आ रही", {"breathless"}),
    ("unable to breathe", {"breathless"}),
    ("moochu vida mudiyala", {"breathless"}),
    ("oxygen is falling", set()),                            # "falling" is not a fall
    ("his SpO2 keeps falling slowly", set()),
    ("Papa fell in the bathroom", {"fall"}),
    ("wheezing at night", {"breathless"}),                   # stems still match with suffixes
    ("vomited twice", {"intake"}),
])
def test_notes_judge_reported_phrases(text, found):
    assert {s["category"] for s in notes.analyse(text)["signals"] if s["weight"] > 0} == found


@pytest.mark.parametrize("text,flag", [("chest pain since morning", "Chest pain"), ("she fainted", "Unresponsive / fainted"),
                                       ("mayakkam aagi vizhundhaar", "Fall"), ("बेहोश हो गए", "Unresponsive / fainted")])
def test_notes_red_flags(text, flag):
    assert flag in notes.analyse(text)["red_flags"]


def test_notes_confusion_counts_as_new_confusion():
    assert notes.analyse("very confused and not making sense")["suggests_new_confusion"]
    assert not notes.analyse("not confused, talking normally")["suggests_new_confusion"]


# ----------------------------------------------------------------- quality gate
GOOD = {"hr": 80, "rr": 16, "spo2": 96, "sbp": 120, "temp": 36.8, "sqi": 0.95, "motion": 0.05}


@pytest.mark.parametrize("field,value", [("hr", 10), ("hr", 300), ("rr", 0), ("spo2", 30), ("sbp", 20), ("temp", 45.0)])
def test_quality_rejects_implausible(field, value):
    q = quality.assess({**GOOD, field: value})
    assert q["artifact"] and q["kind"] == "invalid"


def test_quality_missing_value_is_invalid():
    q = quality.assess({k: v for k, v in GOOD.items() if k != "spo2"})
    assert q["artifact"]


def test_quality_motion_drop_vs_real_drop():
    assert quality.assess({**GOOD, "spo2": 88, "motion": 0.9}, prev_good=GOOD)["kind"] == "motion"
    assert quality.assess({**GOOD, "spo2": 88, "motion": 0.1}, prev_good=GOOD)["ok"]  # real drop, still

# ----------------------------------------------------------------- model guarantees
@pytest.fixture(scope="module")
def pred():
    return Predictor()


def test_model_coefficients_respect_clinical_signs(pred):
    assert all(c * s >= 0 for c, s in zip(pred.coef, SIGNS, strict=True))


def test_model_threshold_and_metrics_present(pred):
    m = pred.meta["metrics"]
    assert 0 < pred.threshold < 1
    assert m["ml_median_lead_h"] > m["news2_ge3_median_lead_h"]
    assert m["ml_false_early_warnings_per_stable_patient_day"] < m["news2_ge3_alerts_per_stable_patient_day"]


def _ctx_and_x(drift=0.0):
    t = np.arange(0, 40 * 3600, 300.0)
    rng = np.random.default_rng(1)
    ramp = np.clip((t - 30 * 3600) / (10 * 3600), 0, 1) * drift
    vals = {"hr": 80 + 25 * ramp + rng.normal(0, 1, len(t)), "rr": 16 + 8 * ramp + rng.normal(0, .5, len(t)),
            "spo2": 96 - 4 * ramp + rng.normal(0, .3, len(t)), "sbp": 125 - 20 * ramp + rng.normal(0, 2, len(t)),
            "temp": 36.8 + 1.5 * ramp + rng.normal(0, .05, len(t)), "weight": 70 + 0 * t}
    base = personal_baseline(t, vals, {})
    return compute(t, vals, len(t) - 1, base, spo2_scale=1, on_oxygen=False)


def test_model_explanation_is_exact_decomposition(pred):
    x, ctx = _ctx_and_x(drift=1.0)
    out = pred.predict(x, ctx)
    z = (x - pred.mu) / pred.sd
    assert math.isclose(out["prob"], float(sigmoid(pred.b + (pred.coef * z).sum())), abs_tol=1e-3)
    assert out["factors"] and all(f["impact"] > 0 for f in out["factors"])
    assert len({f["feature"].split("_")[-1] for f in out["factors"]}) == len(out["factors"])  # one per vital


def test_model_risk_rises_with_deterioration(pred):
    stable, _ = _ctx_and_x(0.0)
    sick, _ = _ctx_and_x(1.0)
    assert pred.predict(sick)["prob"] > pred.predict(stable)["prob"] + 0.3


def test_rule_fallback_flags_adverse_trends():
    _, ctx = _ctx_and_x(1.0)
    fb = rule_fallback(ctx)
    assert fb["source"] == "fallback-rules" and fb["prob"] > 0.5 and fb["factors"]


def test_circuit_breaker_open_halfopen_close():
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=0.05)
    def boom():
        raise RuntimeError("down")
    assert cb.call(boom, lambda: "fb") == "fb" and cb.state == "closed"
    assert cb.call(boom, lambda: "fb") == "fb" and cb.state == "open"
    assert cb.call(lambda: "ml", lambda: "fb") == "fb"            # open: model not even called
    time.sleep(0.06)
    assert cb.state == "half-open"
    assert cb.call(lambda: "ml", lambda: "fb") == "ml" and cb.state == "closed"


# ----------------------------------------------------------------- feature maths
def test_slope_of_linear_series_is_exact():
    t = np.arange(0, 6 * 3600, 300.0)
    assert math.isclose(_slope_per_hour(t, 10 + 2.5 * t / 3600), 2.5, rel_tol=1e-9)


def test_personal_baseline_is_median_of_first_24h():
    t = np.arange(0, 48 * 3600, 300.0)
    hr = np.where(t < 24 * 3600, 70.0, 120.0)
    b = personal_baseline(t, {"hr": hr, "rr": hr, "spo2": hr, "sbp": hr, "temp": hr, "weight": hr}, {})
    assert b["hr"] == 70.0


def test_feature_vector_shape():
    x, ctx = _ctx_and_x()
    assert x.shape == (len(FEATURES),) and np.isfinite(x).all() and "news2_now" in ctx


# ----------------------------------------------------------------- auth internals
def test_pins_are_never_stored_in_plain_text():
    a = Auth()
    for u in a.users.values():
        assert "pin" not in u and len(u["hash"]) == 32 and len(u["salt"]) == 16


def test_sessions_are_random_and_expire():
    a = Auth()
    t1, _ = a.login("asha", "1234")
    t2, _ = a.login("asha", "1234")
    assert t1 != t2 and len(t1) >= 40
    a.sessions[t1]["expires"] = time.time() - 1
    assert a.resolve(t1) is None and a.resolve(t2)["role"] == "desk"


def test_family_scope():
    a = Auth()
    tok, _ = a.login("sunita", "1111")
    u = a.resolve(tok)
    assert a.can_see(u, "P-101") and not a.can_see(u, "P-102")
