# HomeWard: Technical Documentation

Team **Smart Health** · Operation Ward Zero

## 1. Problem understanding

After discharge, an elderly or chronically ill patient at home is watched by a family member who is usually not trained, is often busy and sleeps at night. Three failures repeat:

1. **Late recognition.** Infection, heart-failure fluid build-up or a COPD flare develop over 12–48 h. Early signs such as breathing a bit faster, a slightly higher pulse or a creeping temperature are invisible to a non-clinician until the patient is obviously unwell.
2. **Alarm fatigue.** Home devices alarm on single bad readings, motion and loose probes. They also alarm on patients whose "abnormal" is normal for them, such as COPD patients with an 88–92% oxygen target. Caregivers learn to ignore alarms.
3. **Silent failure.** Wi-Fi drops, power cuts, a dead hub or a crashed cloud service. Most systems simply go quiet and the dashboard keeps showing the last "green" value.

HomeWard is designed around those three failures.

## 2. Solution overview

| Stage | Component | Key idea |
|---|---|---|
| **Sense** | `edge.py` bedside hub (simulated IoT; optional real ESP32 kit) | Vitals every 5 min (HR, RR, SpO₂, BP, temperature, weight, signal quality, motion), smart pill box, bed + room-motion sensors, oxygen concentrator telemetry and hub power. Everything is written to a local SQLite outbox before upload. |
| **Understand** | `quality.py`, `news2.py`, `features.py`, `notes.py` | Artifact gate → NEWS2 (with Scale 2) → personal baseline and 6/12 h trends → caregiver-note signals (EN/HI/Hinglish). |
| **Predict** | `model.py`, `train.py` | P(NEWS2 ≥ 5 within 24 h) from an explainable, sign-constrained logistic regression behind a circuit breaker with rule fallback. |
| **Alert** | `engine.py` | **Low / Medium / High risk + one-line reason** (internal tiers STABLE / WATCH / HIGH / CRITICAL); confirmation, de-duplication, hysteresis, typed alerts (VITALS, EARLY_WARNING, NOTE, SENSOR, DEVICE, CAREGIVER, MEDICATION, FALL, EQUIPMENT, POWER, EDGE). |
| **Respond** | `engine.py`, `web/`, `hardware/` | Escalation ladder with timers, acknowledge/resolve (dashboard, phone app or bedside button), SBAR, round checks, verified bedside check-in, response-time metrics, audit log. |

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

**Stack:** Python 3, FastAPI, Uvicorn, SQLite (WAL), NumPy, httpx; dashboard pushed by Server-Sent Events (with polling fallback), vanilla JS with no build step; pytest; Arduino/ESP32 for the optional hardware kit. Everything starts with `python run.py` (or `docker run`).

**Simulated time.** A `SimClock` runs at 300× by default (1 real second = 5 simulated minutes; 60× to 1800× from the UI). This lets a 24–48 h deterioration play out live in a short demo, from real computed data rather than a recording. Escalation timers use real seconds, because a human presenter has to react to them.

## 4. Features

- **Ward overview**: one card per home, with tier, NEWS2, 24 h risk, SpO₂, RR and device state.
- **Patient view**: live vital tiles with NEWS2 points and the personal baseline; 12/24/48 h charts with the normal band, baseline line, ignored artifacts (✕) and late-arriving data (●); a NEWS2 and AI-risk trend chart.
- **"Why?" panels**: the full NEWS2 table, the response recommendation and the consciousness source; the AI risk gauge with the factors pushing risk up and reassuring factors, written in plain language.
- **Caregiver notes**: English, Hindi, Hinglish, Tamil and Tanglish lexicon, with negation before ("no fever") and after ("bukhar nahi", "kaaichal illai") the term. The detected language is shown back to the writer. Red flags (chest pain, fall, unresponsive) raise immediate alerts. A note describing new confusion sets ACVPU = C (3 NEWS2 points).
- **Alerts**: typed, de-duplicated and severity-upgraded. The escalation ladder shows a live countdown. CRITICAL alerts notify the caregiver and the care desk in parallel. Each alert carries a recommended next step.
- **Response tracking**: acknowledge/resolve with action and note (the note feeds back into the patient's context), median acknowledgement time, on-duty check-in, overdue round alerts and an immutable audit log.
- **SBAR generator**: a structured phone handover built from live data.
- **Caregiver phone app**: Hindi/English, big status, "I'm with the patient" / "Done" buttons, voice notes (Web Speech API, `hi-IN`), 108 and care-desk call buttons, and a plain-language ACVPU round check.
- **Resilience Lab**: live fault injection plus counters (see §6).
- **Guided demo**: one button plays the whole Sense → Respond story on the live system with captions (about 2 min 50 s), which makes the demo reproducible for judges.
- **Home devices panel**: bedside hub, vitals sensor (simulated or real), smart pill box, bed + motion sensor, oxygen concentrator, power and smart scale.
- **Patient timeline**: every alert, acknowledgement, note, round, pill-box opening and bed event in one chronological view.

### Home IoT rules

| Device | Rule |
|---|---|
| Smart pill box | A dose not taken 60 min after it was due raises a MEDICATION alert. It auto-resolves when the box is opened. Heart-failure diuretics carry an extra explanation (fluid build-up risk). |
| Bed + room-motion sensors | Out of bed with **no room movement for 10 min** raises a FALL alert (HIGH), at any hour. Out of bed for more than 20 min at night also raises one. It auto-resolves when the patient is back in bed. |
| Oxygen concentrator | Flow 0 while oxygen is prescribed raises an EQUIPMENT alert (HIGH): *"switch to the backup cylinder"*. Purity below 85% raises a WATCH alert: *"check the filter"*. |
| Hub power | During a power cut the hub runs on battery and keeps monitoring. Battery below 30% raises a POWER alert. |
| Caregiver presence | Check-in by scanning the bedside tag marks presence as verified. The bedside button press refreshes verified presence. Overdue round checks raise a CAREGIVER alert. |

### Risk level and short reason

`STABLE → Low`, `WATCH → Medium`, `HIGH / CRITICAL → High`. The reason is one sentence built from the strongest drivers, for example:
*"NEWS2 9: breathing 26/min, oxygen 91%, temperature 38.9°C; caregiver notes mention fever / chills."* or
*"Trending worse: oxygen level falling -1.5 % over the last 12 h; caregiver notes mention breathlessness."*

## 5. Data processing and risk assessment

1. **Quality gate** (`quality.py`). Physiologically impossible values are rejected. Low signal quality (< 0.4) or a large SpO₂ drop during heavy motion is marked as an artifact. Artifacts are stored and shown, but never scored. If artifacts persist for 3 readings, the caregiver gets a low-priority "re-seat the probe" message instead of a clinical alarm.
2. **NEWS2** (`news2.py`). Full RCP 2017 tables, including Scale 2 for prescribed 88–92% targets and the supplemental-oxygen point. Consciousness comes from the latest caregiver round or note. If neither exists, ACVPU is shown as *assumed* Alert, with a clear label.
3. **Confirmation**. A tier is set from `min(NEWS2 of the last two good readings)`, so a single spike can't trigger an emergency. Display hysteresis steps down one level only after six calmer readings (30 min).
4. **Personal baseline** (`features.py`). Median of the first 24 h of good readings.
5. **Features (18)**. For each of HR, RR, SpO₂, systolic BP and temperature: deviation from baseline, 6 h slope and 12 h slope. Plus weight change, current NEWS2 and caregiver-note concern.
6. **Model**. Logistic regression (Newton/IRLS, L2), trained with **clinical sign constraints**: any feature whose coefficient contradicts physiology (usually from collinearity) is removed and the model is refit. Contributions `coef × z` are therefore exact explanations, and none of them can say "fever is reassuring". The model is gated during calibration (< 30 h of data), as it was in training.
7. **Early warning**. Probability ≥ threshold, sustained for 1 h, with hysteresis at 0.8 × threshold. The threshold is chosen to keep false early-warnings ≤ 0.15 per stable patient-day (an alarm-fatigue budget).
8. **Fallback**. If the model fails three times, the circuit breaker opens. A transparent slope heuristic is used and labelled *rule-based fallback* in the UI. NEWS2 alerting is unaffected.

### Evaluation (patient-level hold-out; see MODEL_CARD.md)

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
| Power cut | The hub keeps monitoring on battery. The oxygen concentrator stops, so oxygen-dependent patients get an immediate EQUIPMENT alert. A low hub battery raises a POWER alert. Alerts auto-resolve when power returns. | `test_power_cut_keeps_monitoring_and_flags_oxygen_concentrator` |
| Hub dead / no data | After 20 sim-min the status becomes **UNKNOWN** (never stable). After 45 min a DEVICE alert escalates. It auto-resolves when data resumes. | `test_dead_hub_goes_unknown_then_alerts` |
| AI service crash | Circuit breaker (3 failures → open, cool-down, half-open probe). Rule fallback is labelled in the UI. | `test_ml_circuit_breaker_falls_back_and_recovers` |
| Sensor artifact | Quality gate, no clinical alarm, "re-seat probe" hint. | `test_displaced_sensor_never_raises_vitals_alarm` |
| Nobody responds | Timed escalation L1 → L4; CRITICAL notifies the care desk in parallel; reminders for acknowledged-but-unresolved alerts. | `test_escalation_ladder_and_acknowledgement` |
| Caregiver absent | Scheduled round checks; an overdue round raises a CAREGIVER alert. | `test_missed_round_raises_caregiver_alert` |
| Dashboard loses the server | A red banner says "do not rely on this screen", and polling retries with back-off. | manual / Resilience Lab |

## 7. Optional hardware

`hardware/esp32_homeward/` is a real bedside node (ESP32 + MAX30102 pulse-oximeter + DS18B20 temperature, buzzer, LED, push button, about ₹1,000):
- It posts to `/api/devices/{patient}/sensor`. The real values replace the simulated channels in that home's next reading, so they go through the same quality gate, NEWS2, AI and alerting.
- The buzzer and LED follow `/api/devices/{patient}/command`.
- The button acknowledges alerts.
- After 60 s without the platform, the board applies its own local alarm rules.

`serial_bridge.py` supports USB-only boards, and `virtual_esp32.py` emulates the board. All of it is covered by `test_real_sensor_feeds_pipeline_drives_buzzer_and_button_acks`. No hardware is needed to run or judge HomeWard.

## 8. Safety and privacy

- **Role-based access** (`homeward/auth.py`): the care desk sees every home; a family member or home carer sees only their own patient (state, alerts, notifications, notes, audit). Other patients return 403, and the attempt is audited. Demo/lab controls are care-desk only.
- PINs are stored as salted PBKDF2 hashes. Sessions are random 256-bit tokens held server-side with a 12 h expiry. Logout destroys the session. Five wrong PINs lock the account for 60 s. The browser uses an HttpOnly, SameSite=Strict cookie, and the phone app a bearer token.
- **Per-device keys:** each hub / sensor board holds its own key (HMAC of its device id with a server secret). One home's device can never post readings for, or acknowledge alerts of, another patient. Device keys are compared in constant time. The buzzer command endpoint also needs a device key or a permitted login.
- The acting user is always taken from the session, never the request body, so the audit trail can't be forged. Opening a record is audited once per session.

- Demo patients are fictional; no real personal data is used or stored.
- Device ingestion needs a device key and a device↔patient binding check.
- No diagnosis language anywhere: risk is described as patterns and recommended response levels.
- Probabilities are clamped to 1–99% in the UI; a forecast should never claim certainty.
- The "Call 108" action is a suggestion that needs human confirmation; there is no autonomous emergency dialling.
- Every alert transition, note, round and fault injection is written to the audit log.

## 9. Assumptions and limitations

- Model metrics come from a held-out cohort of simulated patients, not a clinical study. Prospective validation on real, consented data would be required before clinical use.
- In the training cohort, the note-concern feature was dropped by the sign constraint, because simulated notes carried no independent signal. In the live system notes still act through NEWS2 (new confusion), red flags and the WATCH rule.
- Notifications (push, WhatsApp, SMS, calls) are simulated as an outbox log.
- Demo accounts use fixed PINs (overridable via environment variables). Production needs TLS, a proper user store and stronger credentials (OTP / SSO).
- One patient per hub; no multi-tenant isolation; a single SQLite instance.

## 10. Future improvements

BLE BP cuff and smart scale via a Raspberry Pi gateway; FHIR export to hospital EHRs; WhatsApp Business API delivery; GRU/temporal models trained on real home data, with calibration monitoring and drift alerts; Indic speech-to-text for notes beyond Hindi; role-based auth; PostgreSQL/Timescale; on-device model inference; medication adherence (smart pill box).


## 11. Testing and quality

| Suite | What it covers | How to run |
|---|---|---|
| `tests/test_units.py` (107) | Every NEWS2 threshold boundary (RCP 2017), Scale 2 on air vs oxygen, bands; notes in 5 languages incl. negation and red flags; quality gate; model sign constraints, exact explanations, monotonic risk; circuit breaker states; feature maths; PIN hashing and session expiry | `pytest -q` |
| `tests/test_homeward.py` (37) | Per-device keys, device command auth,  Full pipeline through real HTTP: duplicate storms, late data, outages + in-room alarm, dead hub, AI crash, artifacts, escalation ladder, early warning before NEWS2, IoT (pill box, fall, O₂ machine, power cut), hardware endpoints, Low/Medium/High reasons, verified attendance, role-based access | `pytest -q` |
| `tests/e2e/browser.e2e.cjs` (22 checks) | Real Chromium on the real app: login and roles, dashboard rendering, Tamil note, early warning in the UI, acknowledge, SBAR, Resilience Lab outage/recovery, AI fallback, dark mode, guided demo, scoped family view, Hindi phone app, mobile layout, zero JS errors | `node tests/e2e/browser.e2e.cjs` (also via pytest) |

GitHub Actions (`.github/workflows/ci.yml`) runs all three suites on every push.
