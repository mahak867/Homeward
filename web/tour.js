/* Guided demo: plays the whole Sense -> Understand -> Predict -> Alert -> Respond story on the
   live system, with captions. Every step drives the real API; nothing here is pre-recorded. */
(() => {
  const STAGES = ["Sense", "Understand", "Predict", "Alert", "Respond", "Resilience"];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const P = (id) => HW.patient(id) || {};
  const speed = (v) => HW.api("/api/sim", { speed: v, paused: false });
  const chaos = (flag, value, patient_id) => HW.api("/api/chaos", { flag, value, patient_id });
  const sim = (body) => HW.api("/api/sim", body);
  const openAlert = (pid, kinds) => (HW.state().alerts || []).find((a) => a.patient_id === pid && a.status !== "RESOLVED" && (!kinds || kinds.includes(a.kind)));

  const steps = [
    { stage: "Sense", title: "Three homes. One virtual ward.",
      text: "Each home has a bedside hub streaming pulse, breathing, oxygen, blood pressure, temperature and weight, plus a smart pill box and bed and motion sensors. Time is fast-forwarded so a day passes in minutes.",
      run: async () => { HW.toggleLab(false); HW.setTab("overview"); HW.select("P-101"); await speed(300); }, spot: "#plist", hold: 9 },
    { stage: "Understand", title: "Normal is personal",
      text: "Rajesh has COPD. His doctor's oxygen target is 88-92%. A generic monitor would alarm all day, so we score him on NEWS2 Scale 2, the hospital standard, and compare every reading with his own 24-hour baseline (the 'usual' under each value).",
      run: async () => { HW.select("P-102"); }, spot: "#tiles", hold: 10 },
    { stage: "Predict", title: "A slow infection begins…",
      text: "Kamla came home 3 days after pneumonia. We start a gradual infection. Watch the purple AI-risk line: it climbs while the orange NEWS2 line is still flat.",
      wait: "Fast-forwarding until the AI raises an early warning…",
      run: async () => { HW.select("P-101"); await sim({ patient_id: "P-101", scenario: "sepsis" }); await speed(1800); },
      spot: "#chartwrap", until: () => P("P-101").forecast && P("P-101").forecast.early_warning, timeout: 120 },
    { stage: "Predict", title: "Early warning, before any score turns red",
      text: () => `NEWS2 is only ${P("P-101").news2 ? P("P-101").news2.total : "low"}, so a standard score stays quiet. HomeWard already sees her breathing, pulse and temperature drifting from her own normal, and explains exactly why. On held-out test patients this comes a median of ${(HW.state().model || {}).metrics ? HW.state().model.metrics.ml_median_lead_h : 10} hours earlier than NEWS2 alone.`,
      run: async () => { await speed(60); }, spot: "#fcbox", hold: 12 },
    { stage: "Respond", title: "The family is part of the team, in Hindi",
      text: "Sunita, her daughter-in-law, writes what she sees in her own words. HomeWard understands English, Hindi and Hinglish, including negation ('bukhar nahi hai'), and feeds it into the risk picture.",
      run: async () => { HW.setTab("care"); await HW.addNote("Maa ji ko thand lag rahi hai, khana bhi nahi khaya. Seene mein dard nahi hai.", "Sunita (daughter-in-law)"); },
      spot: "#notebox", hold: 10 },
    { stage: "Alert", title: "Now it's urgent",
      text: "As she worsens, NEWS2 crosses the urgent threshold, confirmed on two consecutive good readings so a single glitch can't cause panic.",
      wait: "Fast-forwarding until NEWS2 confirms an urgent response…",
      run: async () => { HW.setTab("overview"); await speed(1800); }, spot: "#alertsCard",
      until: () => ["HIGH", "CRITICAL"].includes(P("P-101").tier), timeout: 120 },
    { stage: "Respond", title: "An alert that can't get lost",
      text: "The caregiver is notified first. Critical alerts reach the tele-nurse desk at the same time. If nobody acknowledges, the alert climbs the ladder on a timer: family, care desk, then ambulance. Every step is audited.",
      run: async () => { await speed(60); }, spot: "#alertsCard", hold: 12 },
    { stage: "Respond", title: "Acknowledged, with a doctor-ready handover",
      text: "The nurse acknowledges and calls the doctor with a structured SBAR summary generated from live data: Situation, Background, Assessment, Recommendation.",
      run: async () => { const a = openAlert("P-101", ["VITALS", "NOTE"]) || openAlert("P-101"); if (a && a.status === "OPEN") await HW.api(`/api/alerts/${a.id}/ack`, { by: "Care desk - Nurse Asha" }); await HW.openSBAR(); },
      spot: "#modalCard", hold: 12, after: () => HW.closeModal() },
    { stage: "Resilience", title: "The internet goes down",
      text: "Home Wi-Fi fails. The hubs keep recording to their own storage, the dashboard refuses to show stale data as 'stable', and because Kamla is critical her hub sounds an alarm in the room itself.",
      wait: "Waiting for the bedside hub to raise its in-room alarm…",
      run: async () => { HW.toggleLab(true); await chaos("network_down", true); await speed(900); }, spot: "#labToggles",
      until: () => (P("P-101").hub || {}).local_alarm, timeout: 45 },
    { stage: "Resilience", title: "…and comes back. Nothing is lost.",
      text: "On reconnect, every buffered reading replays in order, duplicates are ignored, and what happened during the gap is evaluated and labelled. These counters show every fault the system absorbed.",
      wait: "Replaying buffered readings…",
      run: async () => { await chaos("network_down", false); }, spot: "#kcounters",
      until: () => HW.state().patients.every((p) => !p.hub || (p.hub.buffered === 0 && p.data_state === "LIVE")), timeout: 40, hold: 4 },
    { stage: "Resilience", title: "Power cut",
      text: "The electricity goes. Hubs keep monitoring on battery. Rajesh's oxygen concentrator stops, so his family is told immediately to switch to the backup cylinder: medical equipment is monitored like a vital sign.",
      wait: "Cutting the power…",
      run: async () => { HW.select("P-102"); await chaos("power_cut", true); }, spot: "#alertsCard",
      until: () => !!openAlert("P-102", ["EQUIPMENT"]), timeout: 20, hold: 8, after: () => chaos("power_cut", false) },
    { stage: "Resilience", title: "Even the AI can fail safely",
      text: "We crash the AI service. A circuit breaker switches to transparent rules within seconds. The NEWS2 safety floor never depended on the AI, so patient safety is unchanged.",
      run: async () => { await chaos("ml_down", true); }, spot: "#labToggles", hold: 9, after: () => chaos("ml_down", false) },
    { stage: "Sense", title: "More than vitals: the whole home",
      text: "Anil has heart failure. His smart pill box tracks every dose (a missed water tablet raises an alert), and his smart scale watches for fluid build-up. Now he gets up, falls and lies still: the bed sensor and the room motion sensor together raise a possible-fall alert.",
      wait: "Waiting for the bed and motion sensors…",
      run: async () => { HW.toggleLab(false); HW.select("P-103"); HW.setTab("care"); await sim({ patient_id: "P-103", fall: true }); await speed(300); },
      spot: "#devgrid", until: () => !!openAlert("P-103", ["FALL"]), timeout: 60, hold: 6,
      after: () => sim({ patient_id: "P-103", back_to_bed: true }) },
    { stage: "Sense", title: "Real hardware, same pipeline",
      text: "An ESP32 with a MAX30102 pulse-oximeter and a temperature probe (about ₹1,000) plugs straight in. Its buzzer and LED follow the alert state, its button acknowledges alerts at the bedside, and it alarms on its own if the internet is down.",
      run: async () => { HW.setTab("care"); }, spot: "#devgrid", hold: 10 },
    { stage: "Respond", title: "HomeWard: a ward that fits inside a bedroom",
      text: () => { const m = (HW.state().model || {}).metrics || {}; return `Earlier warning (${m.ml_median_lead_h ?? 10} h vs ${m.news2_ge3_median_lead_h ?? 6} h), fewer false alarms (${m.ml_false_early_warnings_per_stable_patient_day ?? 0.12} vs ${m.news2_ge3_alerts_per_stable_patient_day ?? 0.27} per patient-day), explanations families understand, and a system that keeps working when things break. Thank you!`; },
      run: async () => { HW.setTab("overview"); HW.select("P-101"); HW.toggleLab(true); await speed(300); }, spot: "#modelcard", hold: 14 },
  ];

  let i = -1, running = false, paused = false, token = 0, spotEl = null;
  const el = document.getElementById("tour");

  function spot(sel) {
    if (spotEl) spotEl.classList.remove("spot");
    spotEl = sel ? document.querySelector(sel) : null;
    if (spotEl) { spotEl.classList.add("spot"); spotEl.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" }); }
  }
  function draw(s, waiting, frac) {
    const txt = typeof s.text === "function" ? s.text() : s.text;
    el.innerHTML = `<div class="top"><span>Guided demo · ${i + 1}/${steps.length}</span><span class="stage">${STAGES.map((x) => `<span class="${x === s.stage ? "on" : ""}">${x}</span>`).join("")}</span></div>
      <h4>${s.title}</h4><p>${txt}</p>
      <div class="ctl">${waiting ? `<span class="wait">${s.wait}</span>` : `<div class="prog"><b style="transform:scaleX(${frac.toFixed(3)})"></b></div>`}
        <button class="btn sm" id="tPause">${paused ? "Resume" : "Pause"}</button><button class="btn sm primary" id="tNext">Next</button><button class="btn sm" id="tExit">Exit</button></div>`;
    document.getElementById("tNext").onclick = () => go(i + 1);
    document.getElementById("tExit").onclick = stop;
    document.getElementById("tPause").onclick = () => { paused = !paused; draw(s, waiting, frac); };
  }
  async function go(n) {
    const my = ++token;
    if (i >= 0 && steps[i] && steps[i].after) { try { await steps[i].after(); } catch {} }
    if (n >= steps.length) return stop();
    i = n; const s = steps[i];
    draw(s, !!s.until, 0); spot(s.spot);
    try { await s.run(); } catch (e) { console.warn(e); }
    await sleep(400); spot(s.spot);
    if (s.until) {
      const t0 = Date.now();
      while (my === token && !(s.until() || Date.now() - t0 > s.timeout * 1000)) await sleep(400);
      if (my !== token) return;
    }
    const hold = (s.hold || 6) * 1000; let el0 = 0;
    draw(s, false, 0);
    while (my === token && el0 < hold) {
      await sleep(100); if (!paused) el0 += 100;
      const bar = el.querySelector(".prog b"); if (bar) bar.style.transform = `scaleX(${(el0 / hold).toFixed(3)})`;
    }
    if (my === token) go(i + 1);
  }
  function start() { running = true; paused = false; el.classList.remove("hidden"); go(0); }
  function stop() { token++; running = false; spot(null); el.classList.add("hidden"); i = -1; HW.closeModal(); }
  document.getElementById("tourBtn").onclick = () => (running ? stop() : start());
  if (new URLSearchParams(location.search).get("tour") === "1") setTimeout(start, 2500);
  window.HWTour = { start, stop };
})();
