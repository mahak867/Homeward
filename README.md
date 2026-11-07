# Smart Health · HomeWard

**A hospital ward that fits inside a bedroom.** Built by Team **Smart Health** for *Operation Ward Zero*.

HomeWard watches a patient at home the way a good ward nurse would. It knows what is *normal for this patient*, notices slow drifts hours before an emergency, ignores sensor glitches, and makes sure a human responds. It keeps working when the Wi-Fi, the AI or the bedside hub fails.

```
Sense ──────────► Understand ─────────────► Predict ──────────► Alert ──────────────► Respond
bedside hub        quality gate               24 h early warning   deduplicated,        caregiver app (Hindi/English),
(simulated IoT)    NEWS2 safety floor         (explainable AI)     confirmed alerts     care-desk dashboard, SBAR call,
store-and-forward  personal baseline          rule fallback        escalation ladder    audit trail, round checks
```

> ⚠️ **Prototype. All patients and data are synthetic.** This is decision support, not a diagnosis, and not a medical device. The numbers below describe performance on a synthetic cohort only.

![Dashboard](docs/screenshots/dashboard.png)

---

## Run it in 60 seconds

No Docker, no database server and no API keys. You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
python run.py
```

Then open:

- **http://127.0.0.1:8000**: care-desk dashboard
- **http://127.0.0.1:8000/caregiver**: family-caregiver phone app (try `?lang=hi`)
- **http://127.0.0.1:8000/docs**: live API documentation

`python run.py --autopilot` starts the demo deteriorations automatically. `pytest -q` runs the 21 safety and resilience tests.

---

## What makes HomeWard different

| | What we did | Why it matters at home |
|---|---|---|
| 🩺 **Clinical safety floor** | Every reading is scored with **NEWS2**, the published early-warning score used on hospital wards, including **SpO₂ Scale 2** for COPD patients on oxygen. The AI can raise concern earlier, but it can never lower what NEWS2 says. | Uses a score clinicians already trust. Without Scale 2, a COPD patient sitting at their prescribed 88–92% would set off an urgent alarm all day. |
| 🧍 **Personal baseline** | The first 24 h at home teach the system each patient's *own* normal. Everything after that is compared with that baseline as well as with population thresholds. | "HR 92" means little on its own. "HR 92 when she is usually 81" is a signal. |
| 🔮 **24 h early warning, honestly measured** | An explainable model predicts the chance of reaching NEWS2 ≥ 5 within 24 h. On held-out synthetic patients: **median 10.2 h of warning vs 6.0 h for NEWS2 alone**, with **less than half the false alarms** (0.115 vs 0.265 per stable patient-day). Sudden events can't be predicted, and we say so. | Families get hours, not minutes. Caregivers keep trusting the alerts. |
| 💡 **Exact explanations** | Logistic regression with **clinically signed coefficients**, so every bar shown *is* the model's own maths. Example: *"Breathing rate 21/min vs her usual 17"*. A factor can never push risk in a medically impossible direction. | No black box. A caregiver or doctor can check every reason. |
| 🔇 **Alarm-fatigue engineering** | A signal-quality gate drops sensor artifacts. CRITICAL needs **two consecutive good readings**. Alerts are de-duplicated, upgrade instead of repeating, and step down only after 30 min of calm. | The #1 reason real monitoring fails is ignored alarms. |
| 📶 **Works when things break** | Edge hub with a SQLite outbox, ordered replay, back-off with jitter, an **in-room alarm while offline**, idempotent and order-independent ingestion, a stale-data watchdog (**never shows "stable" on old data**), and an AI circuit breaker with rule fallback. | Power and internet at home are unreliable. A monitor that silently stops is worse than none. |
| 🧪 **Resilience Lab** | Live switches inject real faults into the running system: outage, duplicate storm, late or out-of-order data, AI crash, hub power loss. Counters show what was absorbed. | Judges can break it themselves. |
| 👨‍👩‍👧 **Built for Indian families** | Caregiver notes in **English, Hindi and Hinglish**, with negation handling ("bukhar *nahi* hai"). A Hindi phone app with big buttons, a voice note mic, 108 and care-desk call buttons, and a plain-language ACVPU check. A note saying *"confused"* counts as new confusion in NEWS2. | The person at the bedside is usually a daughter-in-law, not a nurse. |
| 🔔 **Guaranteed response** | Escalation ladder: caregiver → family → tele-nurse → emergency, with timers. CRITICAL alerts notify the care desk **in parallel**. The system tracks acknowledgement times, on-duty check-ins and overdue routine rounds. | Someone always owns the alert. |
| 📞 **SBAR handover** | One click produces a Situation-Background-Assessment-Recommendation script to read to a doctor. | Turns a panicked phone call into a clinical handover. |

---

## Mapping to the judging criteria

| Criterion (weight) | Where to look |
|---|---|
| **End-to-end integration (25%)** | One command runs the hubs, the platform, the AI and both UIs. Hubs talk to the platform over real HTTP. The full sense-to-respond loop is exercised in `tests/test_homeward.py::test_outage_buffers_alarms_locally_and_replays` and `test_escalation_ladder_and_acknowledgement`. |
| **Technical skills (20%)** | `homeward/` modules, each focused on one job. The ML is numpy-only, with sign-constrained logistic regression and patient-level hold-out evaluation (`homeward/train.py`). Typed FastAPI contracts. 21 tests. |
| **Reliability & resilience (20%)** | `homeward/edge.py` (store-and-forward, local alarm), `engine.ingest` (idempotent, order-independent), `engine.tick` (watchdog, escalation), `model.CircuitBreaker`. Resilience Lab in the UI. |
| **Practical implementation (20%)** | Hindi caregiver app, round checks, on-duty tracking, SBAR, response-time stats, zero-setup install, runs on a laptop. |
| **Explainability & clinical safety (15%)** | NEWS2 breakdown table, exact per-factor contributions, "reassuring" factors, data-quality reasons, model card with honest limits, synthetic-only data, no diagnosis language, audit log, device-key auth. |

Full details: **[docs/TECHNICAL.md](docs/TECHNICAL.md)** · Model card: **[docs/MODEL_CARD.md](docs/MODEL_CARD.md)** · Demo script: **[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)**

## Repository layout

```
run.py                  one-command launcher (platform + 3 simulated homes)
homeward/
  physiology.py         synthetic patients + deterioration scenarios (shared by demo and training)
  edge.py               bedside hub: sensors, SQLite outbox, back-off, in-room alarm
  quality.py            signal-quality / artifact gate
  news2.py              NEWS2 incl. SpO2 Scale 2
  features.py           personal baseline, trends, model features
  model.py              explainable predictor + circuit breaker + rule fallback
  train.py              synthetic cohort, training, held-out evaluation
  notes.py              English/Hindi/Hinglish caregiver-note understanding
  engine.py             ingestion, risk assessment, alerts, escalation, watchdog, SBAR
  server.py             FastAPI API + static UI
  model.json            trained model (regenerate: python -m homeward.train)
web/                    dashboard (index.html, app.js) + caregiver app (caregiver.html), no build step
tests/                  pytest suite
scripts/fast_forward.py prints the alert timeline of a 40 h simulation in seconds
docs/                   technical doc, model card, demo script, screenshots
```

## Tech stack

Python 3 · FastAPI · SQLite (WAL) · NumPy · httpx · vanilla HTML/CSS/JS with hand-drawn canvas charts (no CDN, so it works offline) · pytest · Playwright for screenshots.

## Acknowledgements and licences

NEWS2 thresholds come from the Royal College of Physicians *National Early Warning Score (NEWS) 2* (2017). FastAPI, Uvicorn, NumPy, httpx and pytest are used under their open-source licences. AI coding assistants were used during development; the team understands and can explain every component.
