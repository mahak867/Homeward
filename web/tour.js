/* Guided demo: plays the whole Sense -> Understand -> Predict -> Alert -> Respond story on the
   live system with captions, title slides and the caregiver's phone. Every step drives the real
   API; nothing is pre-recorded. Total runtime is just under 3 minutes.
   ?tour=1 starts it automatically; add &clean=1 to hide the caption card (for screen recording). */
(() => {
  const STAGES = ["Sense", "Understand", "Predict", "Alert", "Respond", "Resilience"];
  const QS = new URLSearchParams(location.search);
  const CLEAN = QS.get("clean") === "1";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const P = (id) => HW.patient(id) || {};
  const M = () => ((HW.state() || {}).model || {}).metrics || {};
  const speed = (v) => HW.api("/api/sim", { speed: v, paused: false });
  const chaos = (flag, value, patient_id) => HW.api("/api/chaos", { flag, value, patient_id });
  const sim = (body) => HW.api("/api/sim", body);
  const openAlert = (pid, kinds) => (HW.state().alerts || []).find((a) => a.patient_id === pid && a.status !== "RESOLVED" && (!kinds || kinds.includes(a.kind)));

  // ---------- caregiver phone (the real /caregiver app in an iframe)
  const phone = document.getElementById("phone"), frame = document.getElementById("phoneFrame");
  async function showPhone(pid) {
    if (!phone.classList.contains("hidden")) return;
    // the phone is Sunita's own device: it signs in as her (family role, sees only Kamla) with a bearer token
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user: "sunita", pin: "1111", cookie: false }) });
    const tok = r.ok ? (await r.json()).token : "";
    frame.src = `/caregiver?p=${pid}&lang=hi#token=${tok}`; phone.classList.remove("hidden");
  }
  function hidePhone() { phone.classList.add("hidden"); }
  const pdoc = () => frame.contentDocument;
  async function phoneType(text) {
    for (let i = 0; i < 30 && !(pdoc() && pdoc().getElementById("note")); i++) await sleep(200);
    const ta = pdoc().getElementById("note"); ta.focus();
    for (let i = 1; i <= text.length; i += 3) { ta.value = text.slice(0, i); await sleep(35); }
    ta.value = text; await sleep(300);
    pdoc().getElementById("send").click();
  }
  async function phoneAck() {
    for (let i = 0; i < 30; i++) { const b = pdoc() && pdoc().querySelector("[data-ack]"); if (b) { b.click(); return true; } await sleep(250); }
    return false;
  }

  // ---------- title slides
  const slideEl = document.getElementById("slide");
  const SLIDES = {
    intro: () => `<div class="inner"><div class="kicker">Operation Ward Zero · Team Smart Health</div>
      <h1>HomeWard: a hospital ward that fits inside a <em>bedroom</em></h1>
      <p>After discharge, the "ward" at home is usually a daughter-in-law, a thermometer and "are you feeling okay?". Deterioration is noticed late, cheap monitors cry wolf, and Wi-Fi or power cuts make them go silent.</p>
      <p>HomeWard watches the patient like a good ward nurse would, and keeps working when things break.</p></div>`,
    arch: () => `<div class="inner"><div class="kicker">How it works</div><h1>Sense → Understand → Predict → Alert → <em>Respond</em></h1>
      <div class="flow">
        <div><i>📡</i><b>Sense</b><span>Bedside hub per home: vitals, pill box, bed + motion, oxygen machine, power. Store-and-forward.</span></div>
        <div><i>🩺</i><b>Understand</b><span>Artifact filter, NEWS2 hospital score, personal baseline, Hindi/English notes.</span></div>
        <div><i>🧠</i><b>Predict</b><span>Explainable AI: risk of deterioration in the next 24 h, with exact reasons.</span></div>
        <div><i>🚨</i><b>Alert</b><span>Low / Medium / High + reason. Confirmed, de-duplicated, escalated on a timer.</span></div>
        <div><i>🙋</i><b>Respond</b><span>Caregiver app in Hindi, care-desk dashboard, SBAR call, verified attendance.</span></div>
      </div></div>`,
    outro: () => { const m = M(); return `<div class="inner"><div class="kicker">HomeWard · Team Smart Health</div><h1>Earlier warnings. Fewer false alarms. <em>Never silent.</em></h1>
      <div class="stats">
        <div><b>${m.ml_median_lead_h ?? 10.2} h</b><span>median early warning (vs ${m.news2_ge3_median_lead_h ?? 6} h for NEWS2 alone, held-out test patients)</span></div>
        <div><b>${m.ml_false_early_warnings_per_stable_patient_day ?? 0.115}</b><span>false alerts per patient-day (vs ${m.news2_ge3_alerts_per_stable_patient_day ?? 0.265})</span></div>
        <div><b>6</b><span>live failure modes survived: internet, power, duplicates, late data, AI crash, hub loss</span></div>
        <div><b>163</b><span>automated checks (141 tests + 22 real-browser) · CI on every push · role-based access · optional ₹1,000 ESP32 kit</span></div>
      </div><div class="team">Notes in English · हिन्दी · Hinglish · தமிழ் · Tanglish &nbsp;|&nbsp; Python · FastAPI · SQLite · NumPy · SSE · ESP32 &nbsp;|&nbsp; Thank you!</div></div>`; },
  };
  function showSlide(k) { slideEl.innerHTML = SLIDES[k](); slideEl.classList.remove("hidden"); }
  function hideSlide() { slideEl.classList.add("hidden"); }

  const steps = [
    { slide: "intro", title: "Intro", hold: 8 },
    { slide: "arch", title: "How it works", hold: 9 },
    { stage: "Sense", title: "Three homes, one live pipeline",
      text: "Each home's hub streams pulse, breathing, oxygen, blood pressure, temperature and weight, plus pill box, bed, motion and oxygen-machine data. The strip on top shows data flowing end to end. Time is fast-forwarded.",
      run: async () => { HW.toggleLab(false); HW.setTab("overview"); HW.select("P-101"); await speed(300); }, spot: "#pipeline", hold: 7 },
    { stage: "Understand", title: "Normal is personal",
      text: "Rajesh has COPD with a prescribed oxygen target of 88-92%. We score him on NEWS2 Scale 2, the hospital standard, and compare every reading with his own baseline (the 'usual' values), so there are no false alarms.",
      run: async () => { HW.select("P-102"); }, spot: "#tiles", hold: 8 },
    { stage: "Predict", title: "A slow infection begins…",
      text: "Kamla came home after pneumonia. A gradual infection starts. Watch the purple AI-risk line climb while the orange NEWS2 line is still flat.",
      wait: "Fast-forwarding until the AI raises an early warning…",
      run: async () => { HW.select("P-101"); await sim({ patient_id: "P-101", scenario: "sepsis" }); await speed(1800); },
      spot: "#chartwrap", until: () => P("P-101").forecast && P("P-101").forecast.early_warning, timeout: 90 },
    { stage: "Predict", title: "Early warning, before any score turns red",
      text: () => `NEWS2 is only ${P("P-101").news2 ? P("P-101").news2.total : "low"}, so a standard score stays quiet. HomeWard sees breathing, pulse and temperature drifting from her normal and shows exactly why: risk is now ${P("P-101").risk_level || "MEDIUM"}. On held-out test patients this comes ${M().ml_median_lead_h ?? 10} h earlier than NEWS2 alone.`,
      run: async () => { await speed(60); }, spot: "#fcbox", hold: 9 },
    { stage: "Respond", title: "Sunita writes on her phone, in Hinglish",
      text: "Her daughter-in-law gets the alert in Hindi and writes what she sees in her own words. HomeWard understands Hinglish, including 'nahi', and adds it to the risk reason. Privacy by design: Sunita's account can see only her mother-in-law.",
      run: async () => { await showPhone("P-101"); await sleep(1500); await phoneType("Maa ji ko thand lag rahi hai, khana nahi khaya. Seene mein dard nahi hai."); },
      spot: "#statusbar", hold: 6 },
    { stage: "Alert", title: "Now it's High risk",
      text: "As she worsens, NEWS2 crosses the urgent threshold, confirmed on two consecutive good readings so a single glitch can't cause panic. Critical alerts reach Sunita and the tele-nurse desk at once.",
      wait: "Fast-forwarding until NEWS2 confirms an urgent response…",
      run: async () => { await speed(1800); }, spot: "#alertsCard",
      until: () => ["HIGH", "CRITICAL"].includes(P("P-101").tier), timeout: 90, hold: 3 },
    { stage: "Respond", title: "An alert that can't get lost",
      text: "If nobody acknowledges, the alert climbs the ladder on a timer: caregiver → family → care desk → ambulance. Every step is audited.",
      run: async () => { await speed(60); }, spot: "#alertsCard", hold: 6 },
    { stage: "Respond", title: "Sunita responds; the nurse calls the doctor",
      text: "Sunita taps 'I'm with the patient' on her phone, and the escalation stops. The care desk calls the doctor with an SBAR handover generated from live data.",
      run: async () => { const ok = await phoneAck(); if (!ok) { const a = openAlert("P-101"); if (a) await HW.api(`/api/alerts/${a.id}/ack`, { by: "Sunita (daughter-in-law)" }); } await sleep(2200); await HW.openSBAR(); },
      spot: "#modalCard", hold: 8, after: () => { HW.closeModal(); hidePhone(); } },
    { stage: "Resilience", title: "The internet goes down",
      text: "The hubs keep recording locally, the dashboard refuses to show stale data as 'stable', and Kamla's hub sounds an alarm in the room itself.",
      wait: "Waiting for the bedside hub to raise its in-room alarm…",
      run: async () => { HW.toggleLab(true); await chaos("network_down", true); await speed(900); }, spot: "#labToggles",
      until: () => (P("P-101").hub || {}).local_alarm, timeout: 40, hold: 3 },
    { stage: "Resilience", title: "…and comes back. Nothing is lost.",
      text: "Every buffered reading replays in order, duplicates are ignored, and events from the gap are labelled. The counters show every fault absorbed.",
      wait: "Replaying buffered readings…",
      run: async () => { await chaos("network_down", false); }, spot: "#kcounters",
      until: () => HW.state().patients.every((p) => !p.hub || (p.hub.buffered === 0 && p.data_state === "LIVE")), timeout: 30, hold: 3 },
    { stage: "Resilience", title: "Power cut",
      text: "Hubs keep monitoring on battery. Rajesh's oxygen concentrator stops, so his family is told at once to switch to the backup cylinder.",
      wait: "Cutting the power…",
      run: async () => { HW.select("P-102"); await chaos("power_cut", true); }, spot: "#alertsCard",
      until: () => !!openAlert("P-102", ["EQUIPMENT"]), timeout: 20, hold: 6, after: () => chaos("power_cut", false) },
    { stage: "Resilience", title: "Even the AI can fail safely",
      text: "We crash the AI service. A circuit breaker switches to transparent rules, and the NEWS2 safety floor never depended on the AI.",
      run: async () => { await chaos("ml_down", true); }, spot: "#labToggles", hold: 5, after: () => chaos("ml_down", false) },
    { stage: "Sense", title: "The whole home: a possible fall",
      text: "Anil gets up, falls and lies still. Bed sensor plus room-motion sensor raise a possible-fall alert. His pill box and smart scale are watched too.",
      wait: "Waiting for the bed and motion sensors…",
      run: async () => { HW.toggleLab(false); HW.select("P-103"); HW.setTab("care"); await sim({ patient_id: "P-103", fall: true }); await speed(300); },
      spot: "#devgrid", until: () => !!openAlert("P-103", ["FALL"]), timeout: 45, hold: 4,
      after: () => sim({ patient_id: "P-103", back_to_bed: true }) },
    { stage: "Sense", title: "No hardware needed, but ready for it",
      text: "Everything you saw runs on software sensors. The same pipeline also accepts a ₹1,000 ESP32 kit (pulse-oximeter, thermometer, buzzer, acknowledge button) that alarms on its own when offline.",
      run: async () => { HW.setTab("care"); }, spot: "#devgrid", hold: 6 },
    { slide: "outro", title: "Outro", hold: 10, run: async () => { HW.setTab("overview"); HW.select("P-101"); await speed(300); } },
  ];

  let i = -1, running = false, paused = false, token = 0, spotEl = null, t0tour = 0;
  window.__tourLog = [];
  const el = document.getElementById("tour");
  function spot(sel) {
    if (spotEl) spotEl.classList.remove("spot");
    spotEl = sel ? document.querySelector(sel) : null;
    if (spotEl) { spotEl.classList.add("spot"); spotEl.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" }); }
  }
  function draw(s, waiting) {
    if (CLEAN) return;
    const txt = typeof s.text === "function" ? s.text() : s.text;
    el.innerHTML = `<div class="top"><span>Guided demo · ${i + 1}/${steps.length}</span><span class="stage">${STAGES.map((x) => `<span class="${x === s.stage ? "on" : ""}">${x}</span>`).join("")}</span></div>
      <h4>${s.title}</h4><p>${txt}</p>
      <div class="ctl">${waiting ? `<span class="wait">${s.wait}</span>` : `<div class="prog"><b style="transform:scaleX(0)"></b></div>`}
        <button class="btn sm" id="tPause">${paused ? "Resume" : "Pause"}</button><button class="btn sm primary" id="tNext">Next</button><button class="btn sm" id="tExit">Exit</button></div>`;
    document.getElementById("tNext").onclick = () => go(i + 1);
    document.getElementById("tExit").onclick = stop;
    document.getElementById("tPause").onclick = () => { paused = !paused; document.getElementById("tPause").textContent = paused ? "Resume" : "Pause"; };
  }
  async function go(n) {
    const my = ++token;
    if (i >= 0 && steps[i] && steps[i].after) { try { await steps[i].after(); } catch {} }
    if (n >= steps.length) return stop();
    i = n; const s = steps[i];
    window.__tourLog.push({ step: i + 1, title: s.title, at: (Date.now() - t0tour) / 1000 });
    if (s.slide) { el.classList.add("hidden"); spot(null); showSlide(s.slide); }
    else { hideSlide(); if (!CLEAN) el.classList.remove("hidden"); draw(s, !!s.until); spot(s.spot); }
    try { if (s.run) await s.run(); } catch (e) { console.warn(e); }
    if (!s.slide) { await sleep(300); spot(s.spot); }
    if (s.until) {
      const t0 = Date.now();
      while (my === token && !(s.until() || Date.now() - t0 > s.timeout * 1000)) await sleep(300);
      if (my !== token) return;
      draw(s, false);
    }
    const hold = (s.hold || 6) * 1000; let el0 = 0;
    while (my === token && el0 < hold) {
      await sleep(100); if (!paused) el0 += 100;
      const bar = el.querySelector(".prog b"); if (bar) bar.style.transform = `scaleX(${(el0 / hold).toFixed(3)})`;
    }
    if (my === token) go(i + 1);
  }
  function start() { running = true; paused = false; t0tour = Date.now(); window.__tourLog = []; go(0); }
  function stop() { token++; running = false; spot(null); el.classList.add("hidden"); hideSlide(); hidePhone(); i = -1; HW.closeModal(); window.__tourDone = true; }
  document.getElementById("tourBtn").onclick = () => (running ? stop() : start());
  slideEl.onclick = () => running && go(i + 1);
  document.addEventListener("keydown", (e) => { if (!running) return; if (e.key === "Escape") stop(); else if (e.key === "ArrowRight") go(i + 1); });
  if (QS.get("tour") === "1") setTimeout(start, 1500);
  window.HWTour = { start, stop };
})();
