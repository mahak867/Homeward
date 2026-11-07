# Voice-over script for `HomeWard-demo-video.mp4` (2:36)

The video has no captions and no audio. Read each line starting at its timestamp, at a calm pace. Every line fits its slot with a little room to spare.

**Easy recording:** play the video muted in one window and record your voice with your phone or Audacity while watching. Then combine the two in any editor (CapCut, Clipchamp, iMovie, DaVinci), or with ffmpeg:

```bash
ffmpeg -i HomeWard-demo-video.mp4 -i voice.mp3 -c:v copy -c:a aac -shortest HomeWard-final.mp4
```

| Time | On screen | Say |
|---|---|---|
| **0:00** | Title slide | "Hi, we're team Smart Health. After discharge, the hospital ward at home is usually a family member and a thermometer. Problems are noticed late, cheap monitors cry wolf, and Wi-Fi or power cuts make them go silent. This is HomeWard." |
| **0:08** | Architecture slide | "HomeWard senses, understands, predicts, alerts and makes sure someone responds, end to end." |
| **0:17** | Dashboard, pipeline strip highlighted | "Three homes, one live pipeline: sensors, edge hubs, platform, AI, alerts and response. Time is fast-forwarded." |
| **0:24** | Rajesh's vitals tiles | "Rajesh has COPD and a prescribed oxygen target of 88 to 92. We use NEWS2 Scale 2, the hospital standard, plus his own baseline, so there are no false alarms." |
| **0:33** | Kamla's charts; the purple risk line climbs | "Kamla came home after pneumonia. A slow infection starts. Watch the purple AI risk rise while the orange NEWS2 score is still flat." |
| **0:55** | Early-warning panel | "Early warning, with exact reasons: breathing, pulse and temperature drifting from her normal. On held-out test patients, that's about ten hours earlier than NEWS2 alone." |
| **1:04** | Phone app (Hindi), note typed | "Her daughter-in-law Sunita writes in Hinglish on her phone. HomeWard understands it, even 'nahi', and adds it to the risk reason." |
| **1:13** | Alerts panel, risk becomes HIGH | "Now it's High risk, confirmed on two readings so one glitch can't cause panic." |
| **1:28** | Escalation ladder and countdown | "If nobody responds, the alert climbs: caregiver, family, care desk, ambulance." |
| **1:34** | Phone acknowledge, then SBAR handover | "Sunita taps 'I'm with the patient', and the nurse calls the doctor with a ready SBAR summary." |
| **1:45** | Resilience Lab, internet off | "Internet down: the hub keeps recording and sounds an alarm in the room." |
| **1:49** | Counters, data replayed | "When it's back, every reading replays in order. Nothing is lost." |
| **1:57** | Power cut, oxygen alert | "Power cut: monitoring continues on battery, and the oxygen machine is watched too." |
| **2:04** | AI crash switch | "Even if the AI crashes, safe rules take over instantly." |
| **2:09** | Devices panel, fall alert | "Bed and motion sensors catch a possible fall, and the pill box catches missed doses." |
| **2:16** | Devices panel | "No hardware is needed, but it also works with a thousand-rupee ESP32 sensor kit." |
| **2:22** | Outro slide with numbers | "Earlier warnings, fewer false alarms, never silent. That's HomeWard. Thank you!" |
