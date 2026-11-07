# 3-minute demo video

The easiest way to record is the built-in **▶ Guided demo**. It drives the live system through the full story (Sensor → Platform → AI Risk → Alert → Caregiver Response → Resilience) with on-screen captions, in about 2 min 50 s.

## Recording steps

1. Start the app with `python run.py`. Each start gives a clean demo.
2. Open http://127.0.0.1:8000 in a browser window about 1600 × 1000 or larger. Optionally put `/caregiver?lang=hi` in a phone-sized window next to it.
3. Start a screen recorder (OBS, Loom, Windows Game Bar, or QuickTime on macOS).
4. Click **▶ Guided demo** (or open `http://127.0.0.1:8000/?tour=1`) and read the captions aloud as voice-over. **Pause** and **Next** are on the caption card.

## Story and voice-over (follows the caption cards)

| # | Stage | What happens on screen | Say (short version) |
|---|---|---|---|
| 1 | Sense | Ward of 3 homes with live hubs | "HomeWard turns a bedroom into a ward: a bedside hub streams vitals plus pill-box, bed, motion and oxygen-equipment data." |
| 2 | Understand | Rajesh (COPD), SpO₂ 89% with "usual 90" | "Normal is personal. NEWS2 Scale 2 and a 24-hour personal baseline mean no false alarms for his prescribed target." |
| 3 | Predict | Kamla's infection starts; the purple risk line climbs | "A slow infection begins. The AI risk rises while NEWS2 is still flat." |
| 4 | Predict | Early-warning panel with factors | "Early warning with exact reasons, a median of 10 hours earlier than NEWS2 alone on held-out test patients." |
| 5 | Respond | Hindi/Hinglish note understood | "Her daughter-in-law writes in Hinglish; HomeWard understands it, including 'nahi'." |
| 6 | Alert | NEWS2 crosses the urgent threshold; risk becomes HIGH with a reason | "Now it's High risk, confirmed on two readings." |
| 7 | Respond | Escalation ladder and countdown | "Caregiver first, care desk in parallel for critical, then family and ambulance on a timer." |
| 8 | Respond | Acknowledge + SBAR handover | "Acknowledged, and the doctor gets an SBAR summary." |
| 9–10 | Resilience | Internet outage → in-room alarm → replay | "Offline? The hub alarms in the room and loses nothing." |
| 11 | Resilience | Power cut → oxygen concentrator alert | "Power cut? Monitoring continues on battery, and the oxygen machine is watched too." |
| 12 | Resilience | AI crash → rule fallback | "Even the AI can fail safely." |
| 13 | Sense | Fall: bed + motion sensors | "Out of bed and no movement for 10 minutes: possible fall." |
| 14 | Sense | Devices panel | "And it works with a ₹1,000 ESP32 sensor kit too: no hardware needed, but ready for it." |
| 15 | Wrap | Validation bars | "Earlier warnings, fewer false alarms, and it keeps working when things break. That's HomeWard." |

Tips:
- If you want a longer pause on any card, press **Pause**.
- `python scripts/fast_forward.py 36` prints the expected alert timeline in seconds.
- To show the phone app, open it in a second window during steps 5–8. Alerts appear there in Hindi with big "I'm with the patient" buttons.
