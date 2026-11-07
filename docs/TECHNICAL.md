# HomeWard: Technical Documentation

Team **Smart Health** · Operation Ward Zero · Prototype using synthetic data only

## 1. Problem understanding

After discharge, an elderly or chronically ill patient at home is watched by a family member who is usually not trained, is often busy and sleeps at night. Three failures repeat:

1. **Late recognition.** Infection, heart-failure fluid build-up or a COPD flare develop over 12–48 h. Early signs such as breathing a bit faster, a slightly higher pulse or a creeping temperature are invisible to a non-clinician until the patient is obviously unwell.
2. **Alarm fatigue.** Home devices alarm on single bad readings, motion and loose probes. They also alarm on patients whose "abnormal" is normal for them, such as COPD patients with an 88–92% oxygen target. Caregivers learn to ignore alarms.
3. **Silent failure.** Wi-Fi drops, power cuts, a dead hub or a crashed cloud service. Most systems simply go quiet and the dashboard keeps showing the last "green" value.

HomeWard is designed around those three failures.

## 2. Solution overview

| Stage | Component | Key idea |
|---|---|---|
| **Sense** | `edge.py` bedside hub (simulated IoT) | Readings every 5 min (HR, RR, SpO₂, BP, temperature, weight, signal quality, motion). Each one is written to a local SQLite outbox before upload. |
| **Understand** | `quality.py`, `news2.py`, `features.py`, `notes.py` | Artifact gate → NEWS2 (with Scale 2) → personal baseline and 6/12 h trends → caregiver-note signals (EN/HI/Hinglish). |
| **Predict** | `model.py`, `train.py` | P(NEWS2 ≥ 5 within 24 h) from an explainable, sign-constrained logistic regression behind a circuit breaker with rule fallback. |
| **Alert** | `engine.py` | Tiers STABLE / WATCH / HIGH / CRITICAL; confirmation, de-duplication, hysteresis, typed alerts (VITALS, EARLY_WARNING, NOTE, SENSOR, DEVICE, CAREGIVER, EDGE). |
| **Respond** | `engine.py`, `web/` | Escalation ladder with timers, acknowledge/resolve with action + note, SBAR, round checks, on-duty check-in, response-time metrics, audit log. |

## 3. Architecture

```
 ┌──────────── Home (one per patient) ────────────┐          ┌──────────────── HomeWard platform ────────────────┐
 │ simulated sensors → EdgeHub                     │  HTTPS   │ FastAPI /api/ingest (device key, idempotent)      │
 │   • SQLite outbox (store-and-forward)           │ ───────► │   ↓                                                │
 │   • ordered batches, back-off + jitter          │  batches │ Engine.assess: quality → NEWS2 → baseline/trend   │
 │   • on-device NEWS2 → in-room alarm if offline  │          │   → ML forecast (circuit breaker) → notes context │
 └─────────────────────────────────────────────────┘          │   ↓                                                │
                                                              │ Alerting (dedupe, confirm, hysteresis)             │
 ┌──── Caregiver phone (/caregiver) ────┐   REST poll          │ Watchdog tick (1 s): escalation, stale data,       │
 │ Hindi/English, ack, notes, rounds    │ ◄──────────────────► │   round schedule                                   │
 └──────────────────────────────────────┘                      │ SQLite (WAL): readings, alerts, audit, notes,      │
 ┌──── Care-desk dashboard (/) ─────────┐                      │   rounds, messages                                 │
 │ ward view, explanations, SBAR, lab   │ ◄──────────────────► │ Notification outbox (push/WhatsApp/SMS/call: sim.) │
 └──────────────────────────────────────┘                      └────────────────────────────────────────────────────┘
```

**Stack:** Python 3, FastAPI, Uvicorn, SQLite (WAL), NumPy, httpx; vanilla JS dashboard with no build step and no CDN; pytest. Everything starts with `python run.py`.

**Simulated time.** A `SimClock` runs at 300× by default (1 real second = 5 simulated minutes; 60× to 1800× from the UI). This lets a 24–48 h deterioration play out live in a short demo, from real computed data rather than a recording. Escalation timers use real seconds, because a human presenter has to react to them.

## 4. Features

- **Ward overview**: one card per home, with tier, NEWS2, 24 h risk, SpO₂, RR and device state.
- **Patient view**: live vital tiles with NEWS2 points and the personal baseline; 12/24/48 h charts with the normal band, baseline line, ignored artifacts (✕) and late-arriving data (●); a NEWS2 and AI-risk trend chart.
- **"Why?" panels**: the full NEWS2 table, the response recommendation and the consciousness source; the AI risk gauge with the factors pushing risk up and reassuring factors, written in plain language.
- **Caregiver notes**: EN/HI/Hinglish lexicon with negation. Red flags (chest pain, fall, unresponsive) raise immediate alerts. A note describing new confusion sets ACVPU = C (3 NEWS2 points).
- **Alerts**: typed, de-duplicated and severity-upgraded. The escalation ladder shows a live countdown. CRITICAL alerts notify the caregiver and the care desk in parallel. Each alert carries a recommended next step.
- **Response tracking**: acknowledge/resolve with action and note (the note feeds back into the patient's context), median acknowledgement time, on-duty check-in, overdue round alerts and an immutable audit log.
- **SBAR generator**: a structured phone handover built from live data.
- **Caregiver phone app**: Hindi/English, big status, "I'm with the patient" / "Done" buttons, voice notes (Web Speech API, `hi-IN`), 108 and care-desk call buttons, and a plain-language ACVPU round check.
- **Resilience Lab**: live fault injection plus counters (see §6).

## 5. Data processing and risk assessment

1. **Quality gate** (`quality.py`). Physiologically impossible values are rejected. Low signal quality (< 0.4) or a large SpO₂ drop during heavy motion is marked as an artifact. Artifacts are stored and shown, but never scored. If artifacts persist for 3 readings, the caregiver gets a low-priority "re-seat the probe" message instead of a clinical alarm.
2. **NEWS2** (`news2.py`). Full RCP 2017 tables, including Scale 2 for prescribed 88–92% targets and the supplemental-oxygen point. Consciousness comes from the latest caregiver round or note. If neither exists, ACVPU is shown as *assumed* Alert, with a clear label.
3. **Confirmation**. A tier is set from `min(NEWS2 of the last two good readings)`, so a single spike can't trigger an emergency. Display hysteresis steps down one level only after six calmer readings (30 min).
4. **Personal baseline** (`features.py`). Median of the first 24 h of good readings.
5. **Features (18)**. For each of HR, RR, SpO₂, systolic BP and temperature: deviation from baseline, 6 h slope and 12 h slope. Plus weight change, current NEWS2 and caregiver-note concern.
6. **Model**. Logistic regression (Newton/IRLS, L2), trained with **clinical sign constraints**: any feature whose coefficient contradicts physiology (usually from collinearity) is removed and the model is refit. Contributions `coef × z` are therefore exact explanations, and none of them can say "fever is reassuring". The model is gated during calibration (< 30 h of data), as it was in training.
7. **Early warning**. Probability ≥ threshold, sustained for 1 h, with hysteresis at 0.8 × threshold. The threshold is chosen to keep false early-warnings ≤ 0.15 per stable patient-day (an alarm-fatigue budget).
8. **Fallback**. If the model fails three times, the circuit breaker opens. A transparent slope heuristic is used and labelled *rule-based fallback* in the UI. NEWS2 alerting is unaffected.

### Evaluation (synthetic, patient-level hold-out; see MODEL_CARD.md)

| | AI early warning | NEWS2 ≥ 3 alone |
|---|---|---|
| Deteriorations flagged before NEWS2 ≥ 5 | 106/144 | 118/144 |
| Median warning time | **10.2 h** | 6.0 h |
| False alerts per stable patient-day | **0.115** | 0.265 |
| Gradual (sepsis / heart failure / COPD) flagged | 99/103 | n/a |
| Sudden events (acute hypoxia) flagged early | 6/38 | n/a |

The two systems are complementary. NEWS2 catches the sudden events, and the AI adds hours of warning for gradual ones at half the false-alarm cost. Both run together.

## 6. Reliability and resilience

| Failure | What HomeWard does | Verified by |
|---|---|---|
| Internet down | Hub keeps recording to its outbox, runs NEWS2 on-device and sounds an **in-room alarm** if the patient becomes critical (latched, once). On reconnect it replays in order; events during the gap are evaluated and labelled *"[During connectivity gap]"*. | `test_outage_buffers_alarms_locally_and_replays` |
| Duplicate delivery / retries | `UNIQUE(device_id, seq)`: replays are no-ops and counted. | `test_idempotent_ingest_under_duplicate_storm` |
| Late / out-of-order data | Analysis keys on measurement time. Late rows are flagged and shown on the chart. | `test_out_of_order_delivery_reconciled` |
| Hub dead / no data | After 20 sim-min the status becomes **UNKNOWN** (never stable). After 45 min a DEVICE alert escalates. It auto-resolves when data resumes. | `test_dead_hub_goes_unknown_then_alerts` |
| AI service crash | Circuit breaker (3 failures → open, cool-down, half-open probe). Rule fallback is labelled in the UI. | `test_ml_circuit_breaker_falls_back_and_recovers` |
| Sensor artifact | Quality gate, no clinical alarm, "re-seat probe" hint. | `test_displaced_sensor_never_raises_vitals_alarm` |
| Nobody responds | Timed escalation L1 → L4; CRITICAL notifies the care desk in parallel; reminders for acknowledged-but-unresolved alerts. | `test_escalation_ladder_and_acknowledgement` |
| Caregiver absent | Scheduled round checks; an overdue round raises a CAREGIVER alert. | `test_missed_round_raises_caregiver_alert` |
| Dashboard loses the server | A red banner says "do not rely on this screen", and polling retries with back-off. | manual / Resilience Lab |

## 7. Safety and privacy

- Synthetic patients only; no real personal data.
- Device ingestion needs a device key and a device↔patient binding check.
- No diagnosis language anywhere: risk is described as patterns and recommended response levels.
- Probabilities are clamped to 1–99% in the UI; a synthetic model has no business being certain.
- The "Call 108" action is a suggestion that needs human confirmation; there is no autonomous emergency dialling.
- Every alert transition, note, round and fault injection is written to the audit log.

## 8. Assumptions and limitations

- Physiology and the deterioration scenarios are stylised simulations. Model metrics describe how well the model learns the simulator, not clinical accuracy. Clinical validation on real, consented, labelled data would be required.
- In the synthetic cohort, the note-concern feature was dropped by the sign constraint, because simulated notes carried no independent signal. In the live system notes still act through NEWS2 (new confusion), red flags and the WATCH rule.
- Notifications (push, WhatsApp, SMS, calls) are simulated as an outbox log.
- Authentication for human users is simplified to named actors for the demo; production needs role-based login and TLS.
- One patient per hub; no multi-tenant isolation; a single SQLite instance.

## 9. Future improvements

Real BLE devices (pulse oximeter, BP cuff, smart scale) via a Raspberry Pi gateway; FHIR export to hospital EHRs; WhatsApp Business API delivery; GRU/temporal models trained on real home data, with calibration monitoring and drift alerts; Indic speech-to-text for notes beyond Hindi; role-based auth; PostgreSQL/Timescale; on-device model inference; medication adherence (smart pill box).
