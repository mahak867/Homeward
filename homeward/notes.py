"""Caregiver-note understanding (English, Hindi and Hinglish), with negation.

Family caregivers describe what they see in their own words: "saans phool rahi hai",
"she seems confused today", "bukhar nahi hai". These observations are often the
earliest sign of deterioration (new confusion is a NEWS2 parameter by itself).

This is a transparent lexicon, not a black-box language model, so every extracted
signal can be shown back to the caregiver ("we understood: breathlessness").
"""
from __future__ import annotations

import re

# category -> (display label, weight for risk features, red_flag, terms)
LEXICON = {
    "breathless": ("Breathlessness", 1.0, False, [
        "breathless", "short of breath", "shortness of breath", "difficulty breathing", "hard to breathe",
        "can't breathe", "cannot breathe", "breathing fast", "wheez", "saans phool", "saans ki taklif",
        "saans lene mein", "saans", "dum ghut", "सांस", "साँस"]),
    "confusion": ("New confusion / drowsiness", 1.5, False, [
        "confused", "confusion", "not making sense", "disoriented", "drowsy", "hard to wake", "very sleepy",
        "talking nonsense", "not recognising", "not recognizing", "uljhan", "ulta seedha", "bhool",
        "होश नहीं", "उलझन", "भ्रम"]),
    "fever": ("Fever / chills", 0.8, False, [
        "fever", "feverish", "chills", "shivering", "rigor", "bukhar", "bukhaar", "kaanp", "kapkapi",
        "thand lag", "thandi lag", "thand lagna", "बुखार", "ठंड लग"]),
    "intake": ("Poor eating / drinking / vomiting", 0.6, False, [
        "not eating", "not drinking", "refused food", "refusing food", "no appetite", "vomit", "vomiting",
        "khana nahi", "khaana nahi", "pani nahi", "ulti", "उल्टी", "खाना नहीं"]),
    "urine": ("Passing less urine", 0.7, False, [
        "less urine", "no urine", "not passed urine", "not peed", "peshab kam", "peshaab kam", "पेशाब कम"]),
    "swelling": ("Swelling (legs / ankles)", 0.7, False, [
        "swelling", "swollen", "puffy", "edema", "oedema", "sujan", "soojan", "सूजन"]),
    "cough": ("Cough / more sputum", 0.4, False, [
        "cough", "phlegm", "sputum", "mucus", "khansi", "khaansi", "balgam", "खांसी", "बलगम"]),
    "dizzy": ("Dizziness", 0.5, False, ["dizzy", "dizziness", "lightheaded", "chakkar", "चक्कर"]),
    "chest_pain": ("Chest pain", 2.0, True, [
        "chest pain", "chest tightness", "chest heavy", "pain in chest", "seene mein dard", "seene me dard",
        "chhati mein dard", "सीने में दर्द", "छाती में दर्द"]),
    "fall": ("Fall", 1.5, True, ["fell", "fall", "fallen", "slipped", "gir gaye", "gir gayi", "gir gaya", "गिर"]),
    "unresponsive": ("Unresponsive / fainted", 3.0, True, [
        "unresponsive", "not responding", "fainted", "passed out", "unconscious", "behosh", "बेहोश"]),
    "improving": ("Feeling better", -0.5, False, [
        "feeling better", "much better", "improving", "ate well", "accha lag", "behtar", "बेहतर"]),
}

NEG_BEFORE = {"no", "not", "denies", "without", "never", "na", "nahin", "nahi", "nahī", "नहीं", "न"}
NEG_AFTER = {"nahi", "nahin", "नहीं"}
_clause = re.compile(r"[,.;!?।\n]| but | lekin | par ")

_tok = re.compile(r"[\wऀ-ॿ']+", re.UNICODE)


def analyse(text: str) -> dict:
    low = text.lower()
    found, negated = {}, {}
    for cat, (label, w, red, terms) in LEXICON.items():
        for term in sorted(terms, key=len, reverse=True):
            idx = low.find(term)
            if idx < 0:
                continue
            pre = _clause.split(low[max(0, idx - 30):idx])[-1]
            post = _clause.split(low[idx + len(term):idx + len(term) + 25])[0]
            before = _tok.findall(pre)[-3:]
            after = _tok.findall(post)[:2]
            term_has_neg = any(t in NEG_BEFORE for t in _tok.findall(term))
            neg = (not term_has_neg) and (any(t in NEG_BEFORE for t in before) or any(t in NEG_AFTER for t in after))
            (negated if neg else found)[cat] = {"label": label, "term": term, "weight": w, "red_flag": red}
            break
    # "no fever" and "fever" both present -> trust positive
    for c in list(negated):
        if c in found:
            negated.pop(c)
    score = sum(v["weight"] for v in found.values())
    return {
        "signals": [{"category": c, **v} for c, v in found.items()],
        "negated": [{"category": c, "label": v["label"], "term": v["term"]} for c, v in negated.items()],
        "red_flags": [v["label"] for v in found.values() if v["red_flag"]],
        "concern_score": round(score, 2),
        "suggests_new_confusion": "confusion" in found or "unresponsive" in found,
    }
