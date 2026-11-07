"""Caregiver-note understanding (English, Hindi, Hinglish, Tamil and Tanglish), with negation.

Family caregivers describe what they see in their own words: "saans phool rahi hai",
"she seems confused today", "bukhar nahi hai", "amma ku kaaichal illai". These observations are often the
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
        "saans lene mein", "saans", "dum ghut", "सांस", "साँस",
        "moochu thinaral", "moochu vida kashtam", "moochu", "swasam", "மூச்சு திணறல்", "மூச்சு"]),
    "confusion": ("New confusion / drowsiness", 1.5, False, [
        "confused", "confusion", "not making sense", "disoriented", "drowsy", "hard to wake", "very sleepy",
        "talking nonsense", "not recognising", "not recognizing", "uljhan", "ulta seedha", "bhool",
        "होश नहीं", "उलझन", "भ्रम",
        "kuzhappam", "kuzhapam", "ninaivu illa", "குழப்பம்"]),
    "fever": ("Fever / chills", 0.8, False, [
        "fever", "feverish", "chills", "shivering", "rigor", "bukhar", "bukhaar", "kaanp", "kapkapi",
        "thand lag", "thandi lag", "thand lagna", "बुखार", "ठंड लग",
        "kaaichal", "kaichal", "kaychal", "kulir", "காய்ச்சல்", "குளிர்"]),
    "intake": ("Poor eating / drinking / vomiting", 0.6, False, [
        "not eating", "not drinking", "refused food", "refusing food", "no appetite", "vomit", "vomiting",
        "khana nahi", "khaana nahi", "pani nahi", "ulti", "उल्टी", "खाना नहीं",
        "saapidala", "saapidavillai", "sapidala", "vaanthi", "vanthi", "சாப்பிடவில்லை", "வாந்தி"]),
    "urine": ("Passing less urine", 0.7, False, [
        "less urine", "no urine", "not passed urine", "not peed", "peshab kam", "peshaab kam", "पेशाब कम",
        "siruneer kuraivu", "urine kammi", "சிறுநீர் குறைவு"]),
    "swelling": ("Swelling (legs / ankles)", 0.7, False, [
        "swelling", "swollen", "puffy", "edema", "oedema", "sujan", "soojan", "सूजन",
        "veekkam", "vekkam", "வீக்கம்"]),
    "cough": ("Cough / more sputum", 0.4, False, [
        "cough", "phlegm", "sputum", "mucus", "khansi", "khaansi", "balgam", "खांसी", "बलगम",
        "irumal", "sali", "இருமல்", "சளி"]),
    "dizzy": ("Dizziness", 0.5, False, ["dizzy", "dizziness", "lightheaded", "chakkar", "चक्कर",
        "thalai suthal", "thalai suttral", "தலைசுற்றல்"]),
    "chest_pain": ("Chest pain", 2.0, True, [
        "chest pain", "chest tightness", "chest heavy", "pain in chest", "seene mein dard", "seene me dard",
        "chhati mein dard", "सीने में दर्द", "छाती में दर्द",
        "nenju vali", "nenjuvali", "நெஞ்சு வலி", "நெஞ்சுவலி"]),
    "fall": ("Fall", 1.5, True, ["fell", "fall", "fallen", "slipped", "gir gaye", "gir gayi", "gir gaya", "गिर",
        "vizhundhu", "vizhunthu", "vizhundhaar", "vizhunthar", "விழுந்து", "விழுந்தார்"]),
    "unresponsive": ("Unresponsive / fainted", 3.0, True, [
        "unresponsive", "not responding", "fainted", "passed out", "unconscious", "behosh", "बेहोश",
        "mayakkam", "mayangi", "மயக்கம்"]),
    "improving": ("Feeling better", -0.5, False, [
        "feeling better", "much better", "improving", "ate well", "accha lag", "behtar", "बेहतर",
        "nalla irukku", "paravaillai", "நன்றாக இருக்கிறார்"]),
}

NEG_BEFORE = {"no", "not", "denies", "without", "never", "na", "nahin", "nahi", "nahī", "नहीं", "न"}
NEG_AFTER = {"nahi", "nahin", "नहीं", "illai", "illa", "illama", "இல்லை"}
_clause = re.compile(r"[,.;!?।\n]| but | lekin | par | aanaa | aana | ஆனா ")

_tok = re.compile(r"[\wऀ-ॿ஀-௿']+", re.UNICODE)  # + Devanagari & Tamil combining marks

ROMAN_HI = {"saans", "dum ghut", "uljhan", "ulta seedha", "bhool", "bukhar", "bukhaar", "kaanp", "kapkapi", "thand", "khana", "khaana",
            "pani", "ulti", "peshab", "peshaab", "sujan", "soojan", "khansi", "khaansi", "balgam", "chakkar", "seene", "chhati",
            "gir", "behosh", "accha", "behtar", "nahi", "nahin"}
ROMAN_TA = {"moochu", "swasam", "kuzhappam", "kuzhapam", "ninaivu", "kaaichal", "kaichal", "kaychal", "kulir", "saapidala",
            "saapidavillai", "sapidala", "vaanthi", "vanthi", "siruneer", "kammi", "veekkam", "vekkam", "irumal", "sali",
            "thalai", "nenju", "nenjuvali", "vizhundhu", "vizhunthu", "vizhundhaar", "vizhunthar", "mayakkam", "mayangi",
            "nalla", "paravaillai", "illai", "illa", "amma", "appa", "irukku"}


def languages(text: str) -> list[str]:
    """Which languages / scripts the caregiver wrote in (shown back to them)."""
    low = text.lower()
    toks = set(_tok.findall(low))
    out = []
    if re.search("[ऀ-ॿ]", text):
        out.append("Hindi")
    if re.search("[஀-௿]", text):
        out.append("Tamil")
    if any(any(w.startswith(r) for r in ROMAN_HI) for w in toks):
        out.append("Hinglish")
    if any(any(w.startswith(r) for r in ROMAN_TA) for w in toks):
        out.append("Tanglish")
    if re.search(r"\b(the|is|and|not|she|he|today|very|no|has|feeling|pain)\b", low) or not out:
        out.append("English")
    return out


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
        "languages": languages(text),
    }
