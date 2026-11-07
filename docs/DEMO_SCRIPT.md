# 3-minute demo video: shot list and voice-over

Record at 1440×900 or larger. Before you record, run the flow once to rehearse, then restart for a clean take.

```bash
python run.py
```

Open the dashboard in one browser window. Open `/caregiver?lang=hi` in a narrow second window (or phone emulation in DevTools). Turn on **🔈 Sound**.

| Time | On screen | Say |
|---|---|---|
| **0:00–0:15** | Dashboard, three homes, all "Stable". | "This is HomeWard, by team Smart Health: a hospital ward that fits inside a bedroom. Three patients are at home, each with a bedside hub streaming vitals. Everything here is synthetic." |
| **0:15–0:35** | Click **Rajesh Iyer** (COPD). Point at SpO₂ 89–90% and the "NEWS2 SpO₂ scale 2" chip. | "Rajesh has COPD and his doctor's target is 88 to 92 percent. A normal monitor would alarm all day. We score him on NEWS2 Scale 2, the hospital standard for exactly this case. Every reading is also compared with his *own* baseline from his first 24 hours at home." |
| **0:35–1:05** | Click **Kamla Devi** → Demo simulator: *Infection / sepsis-like* → **Start deterioration**. Set speed **30 min/s**. Watch the AI-risk line climb; the *Early warning* alert appears while NEWS2 is still low. | "Kamla was discharged after pneumonia. I start a slow infection. NEWS2 still says she's fine, but our AI sees breathing, pulse and temperature drifting from *her* normal. Here's an early warning, hours before a standard score would trigger. And here's exactly why, in plain language. Every bar is the model's real maths, not a guess." |
| **1:05–1:25** | Caregiver phone (Hindi). Type or use the mic: *"Maa ji ko thand lag rahi hai, khana nahi khaya"* → Send. Show "We understood: Fever/chills, Poor eating". | "Sunita, her daughter-in-law, gets the alert on her phone in Hindi. She adds what she sees in her own words. HomeWard understands Hindi and Hinglish, including negations like 'bukhar nahi hai'." |
| **1:25–1:50** | Back on the dashboard: NEWS2 crosses 5 then 7. The CRITICAL alert shows the escalation ladder counting down and the parallel care-desk notification. Let it escalate to L2. Then **Acknowledge** and **Resolve…** → "Called doctor / tele-consult". | "Now it's urgent. A critical alert goes to Sunita and to our tele-nurse desk at the same time. If nobody acknowledges, it escalates on a timer to the next person, and every step is audited." |
| **1:50–2:05** | **📞 Call clinician (SBAR)** modal. | "One click gives a structured SBAR handover, so the doctor gets the full picture in 30 seconds." |
| **2:05–2:45** | Open **🧪 Resilience Lab**. Toggle **Internet outage** for ~10 s: cards turn *offline*, buffers grow, then "🔔 local alarm" appears. Turn it off and watch the buffers drain and the "[During connectivity gap]" / local-alarm alerts sync. Toggle **AI service crash**: risk source shows "rule-based fallback". On Rajesh, click **Displace finger sensor**: SpO₂ drops to the 70s, marked ✕, and only a "check the sensor" note appears. | "Homes lose internet. Here the hub keeps recording, and because Kamla is critical it sounds an alarm in the room itself. When the internet returns, everything replays in order and nothing is lost. If our AI crashes, a circuit breaker switches to transparent rules, and NEWS2 never stops. A loose finger probe isn't treated as an emergency: we tell the family to fix the sensor. These counters show every failure the system absorbed." |
| **2:45–3:00** | Model-card panel in the lab, then the README test badge or a terminal running `pytest -q`. | "On held-out synthetic patients we warn a median of 10 hours earlier than NEWS2 alone, with less than half the false alarms. Sudden events can't be forecast, and we say so. 21 automated tests cover these safety claims. HomeWard: sense, understand, predict, alert, respond, even when things break." |

**Tips**

- If an early warning takes too long, press **30 min/s**. If too much happens at once, press **Pause**.
- `python scripts/fast_forward.py 36` prints the exact alert timeline in seconds, so you know what to expect.
- Keep the disclaimer bar visible in at least one shot.
