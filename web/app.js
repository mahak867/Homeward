/* HomeWard dashboard - vanilla JS, no build step, no external libraries (works offline). */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let S = null;                 // latest /api/state
let H = null;                 // latest history for selected patient
let sel = store.get("hw.sel", "P-101");
let rangeH = 24, vital = "rr", alertFilter = "open", actTab = "msgs";
let sound = false, fails = 0;
const seenAlerts = new Map();
let firstLoad = true;

const TIER_TEXT = { STABLE: "Stable", WATCH: "Watch closely", HIGH: "Urgent review", CRITICAL: "Emergency", UNKNOWN: "Status unknown" };
const VITALS = {
  hr: { label: "Heart rate", unit: "bpm", band: [51, 90] },
  rr: { label: "Breathing", unit: "/min", band: [12, 20] },
  spo2: { label: "SpO₂", unit: "%", band: [96, 100] },
  sbp: { label: "Blood pressure", unit: "mmHg", band: [111, 219] },
  temp: { label: "Temperature", unit: "°C", band: [36.1, 38.0] },
  weight: { label: "Weight", unit: "kg", band: null },
};
const n2key = { hr: "hr", rr: "rr", spo2: "spo2", sbp: "sbp", temp: "temp" };
const operator = () => store.get("hw.operator", "Care desk - Nurse Asha");

function fmtTime(ts, withDay = false) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return withDay ? d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) + " " + t : t;
}
const pct = (p) => `${Math.min(99, Math.max(1, Math.round(p * 100)))}%`;
const ago = (realTs) => { const s = Math.max(0, Date.now() / 1000 - realTs); return s < 60 ? `${Math.round(s)}s ago` : `${Math.round(s / 60)}m ago`; };
const tierOf = (p) => (p.data_state === "STALE" || p.data_state === "OFFLINE" || p.data_state === "NO_DATA") ? "UNKNOWN" : p.tier;

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  if (!r.ok) throw new Error(await r.text());
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

/* ------------------------------------------------------------------ polling */
async function poll() {
  try {
    S = await api("/api/state");
    fails = 0;
    $("#offline").classList.add("hidden");
    $("#conn").className = "pill ok"; $("#conn").textContent = "Live";
    render();
  } catch (e) {
    fails++;
    if (fails > 2) {
      $("#offline").classList.remove("hidden");
      $("#conn").className = "pill bad"; $("#conn").textContent = "Disconnected";
    }
  }
  setTimeout(poll, fails ? Math.min(5000, 1000 * fails) : 1000);
}
async function pollHistory() {
  try { H = await api(`/api/patients/${sel}/history?hours=48`); drawCharts(); renderNotes(); } catch {}
  setTimeout(pollHistory, 2500);
}

/* ------------------------------------------------------------------ render */
function render() {
  $("#clock").textContent = fmtTime(S.sim_time, true);
  $$(".speed button[data-speed]").forEach((b) => b.classList.toggle("on", !S.paused && +b.dataset.speed === S.speed));
  $("#pause").textContent = S.paused ? "Resume" : "Pause";
  renderList(); renderDetail(); renderAlerts(); renderActivity(); renderLab(); notifyNew();
}

function renderList() {
  const counts = {};
  $("#plist").innerHTML = S.patients.map((p) => {
    const t = tierOf(p); counts[t] = (counts[t] || 0) + 1;
    const l = p.latest || {};
    const fc = p.forecast || {};
    const hub = p.hub || {};
    const dev = p.data_state === "LIVE" ? (hub.online === false ? `⚠ hub offline · ${hub.buffered} buffered` : `● live`) : `⚠ ${p.data_state.toLowerCase()} · last data ${p.data_age_min ?? "-"} min`;
    return `<div class="pcard t-${t} ${p.id === sel ? "sel" : ""}" data-id="${p.id}">
      <div class="row1"><span class="name">${esc(p.name)}</span><span class="badge t-${t}">${TIER_TEXT[t]}</span></div>
      <div class="cond">${p.age}${p.sex} · ${esc(p.condition)}</div>
      <div class="nums">
        <div><span>NEWS2</span><b>${p.news2 ? p.news2.total : "-"}</b></div>
        <div><span>24h risk</span><b>${fc.source === "calibrating" ? "…" : fc.prob != null ? pct(fc.prob) : "-"}</b></div>
        <div><span>SpO₂</span><b>${l.spo2 ?? "-"}</b></div>
        <div><span>RR</span><b>${l.rr ?? "-"}</b></div>
      </div>
      <div class="foot"><span>${dev}</span><span>${hub.local_alarm ? "🔔 local alarm" : p.on_duty ? "🧑‍⚕️ " + esc(p.on_duty.caregiver.split(" ")[0]) : ""}</span></div>
    </div>`;
  }).join("");
  $$(".pcard").forEach((el) => el.onclick = () => { sel = el.dataset.id; store.set("hw.sel", sel); H = null; render(); api(`/api/patients/${sel}/history?hours=48`).then((h) => { H = h; drawCharts(); renderNotes(); }); });
  const order = ["CRITICAL", "HIGH", "WATCH", "UNKNOWN", "STABLE"];
  $("#wardSummary").textContent = order.filter((k) => counts[k]).map((k) => `${counts[k]} ${TIER_TEXT[k].toLowerCase()}`).join(" · ");
}

function renderDetail() {
  const p = S.patients.find((x) => x.id === sel) || S.patients[0];
  if (!p) return;
  sel = p.id;
  const t = tierOf(p);
  $("#pname").textContent = `${p.name} · ${p.age}${p.sex}`;
  $("#pcond").textContent = p.condition;
  const l1 = p.contacts[0];
  $("#pchips").innerHTML = [
    p.on_oxygen ? `<span class="chip blue">Home oxygen</span>` : "",
    p.spo2_scale === 2 ? `<span class="chip blue" title="NEWS2 SpO2 Scale 2: prescribed target 88-92%">NEWS2 SpO₂ scale 2</span>` : "",
    `<span class="chip">Primary: ${esc(l1.name)}</span>`,
    p.on_duty ? `<span class="chip green">On duty: ${esc(p.on_duty.caregiver)}</span>` : `<span class="chip amber">No caregiver checked in</span>`,
    p.round_due ? `<span class="chip ${S.sim_time > p.round_due ? "red" : ""}">Next round check ${fmtTime(p.round_due)}</span>` : "",
  ].join("");
  $("#dutyBtn").textContent = p.on_duty ? "🧑‍⚕️ Check out" : "🧑‍⚕️ Check in";

  // status bar
  const sb = $("#statusbar");
  sb.className = `statusbar t-${t}`;
  let reasons = p.why && p.why.length ? p.why : ["All vital signs within this patient's expected range."];
  if (t === "UNKNOWN") reasons = [p.data_state === "NO_DATA" ? "No data received yet." : `No data for ${p.data_age_min} min. Last known level: ${p.tier}. Never assume stable on old data - check in person.`];
  if (p.quality && p.quality.artifact_now) reasons = [...reasons, `Latest reading ignored: ${p.quality.last_issue}`];
  if (p.scale1_would_alarm) reasons = [...reasons, "A standard SpO₂ threshold would be alarming now; this patient's prescribed target (88-92%) says otherwise - false alarm avoided."];
  sb.innerHTML = `<div class="big">${TIER_TEXT[t]}</div><ul>${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`;

  // tiles
  const l = p.latest || {}, b = p.baseline || {}, comp = (p.news2 && p.news2.components) || {};
  $("#tiles").innerHTML = Object.entries(VITALS).map(([k, v]) => {
    let val = l[k], extra = "";
    if (k === "sbp") { val = l.sbp != null ? `${Math.round(l.sbp)}/${Math.round(l.dbp)}` : "-"; }
    else if (val != null) val = k === "temp" || k === "weight" ? (+val).toFixed(1) : Math.round(val);
    const pts = comp[n2key[k]];
    const base = b[k] != null ? `usual ${k === "temp" || k === "weight" ? (+b[k]).toFixed(1) : Math.round(b[k])}` : "";
    if (k === "weight" && b.weight != null && l.weight != null) { const d = l.weight - b.weight; extra = ` (${d >= 0 ? "+" : ""}${d.toFixed(1)})`; }
    return `<div class="tile ${vital === k ? "sel" : ""}" data-v="${k}"><div class="lbl">${v.label}${pts != null ? `<span class="pts p${pts}">${pts} pt</span>` : ""}</div>
      <div class="val">${val ?? "-"} <small>${v.unit}</small></div><div class="base">${base}${extra}</div></div>`;
  }).join("");
  $$(".tile").forEach((el) => el.onclick = () => { vital = el.dataset.v; renderDetail(); drawCharts(); });

  // NEWS2 table
  if (p.news2_rows) {
    $("#n2").innerHTML = `<table class="n2">${p.news2_rows.map((r) => `<tr><td>${esc(r.label)}</td><td>${esc(r.value)}</td><td><span class="pts p${r.points}">${r.points}</span></td></tr>`).join("")}
      <tr class="total"><td>Total NEWS2</td><td>${esc(p.news2.band)}</td><td>${p.news2.total}</td></tr></table>
      <div class="note"><b>Response:</b> ${esc(p.news2.response)}</div>
      <div class="note">Consciousness source: ${esc(p.acvpu_source || "")}. ${p.spo2_scale === 2 ? "SpO₂ scored on NEWS2 Scale 2 (target 88-92%)." : ""} CRITICAL needs two consecutive good-quality readings, so one bad sample can't trigger an emergency.</div>`;
  } else $("#n2").innerHTML = `<div class="empty">Waiting for data…</div>`;

  // forecast
  const fc = p.forecast;
  if (!fc) { $("#fc").innerHTML = `<div class="empty">Waiting for data…</div>`; }
  else if (fc.source === "calibrating") {
    $("#fc").innerHTML = `<div class="empty">Learning this patient's personal baseline… ${fc.calibrating_h_left} sim-hours left.<br>The forecast compares the patient with themselves, so it waits for 24 h of data plus 6 h of trend.</div>`;
  } else {
    const pr = fc.prob, thr = fc.threshold, col = pr >= thr ? "var(--high)" : "var(--stable)";
    const maxImp = Math.max(1, ...fc.factors.map((f) => f.impact || 0));
    $("#fc").innerHTML = `<div class="gauge"><div class="ring" style="background:conic-gradient(${col} ${pr * 360}deg,#e5e7eb 0)"><span>${pct(pr)}</span></div>
      <div><b>${pr >= thr ? (fc.early_warning ? "Early warning active" : "Rising - confirming (needs 1 h sustained)") : "No early warning"}</b><br>
      <span style="font-size:12px;color:var(--muted)">Estimated chance of reaching NEWS2 ≥ 5 in the next 24 h. Alert threshold ${pct(thr)}. Source: ${fc.source === "ml" ? "AI model" : "<b style='color:var(--watch)'>rule-based fallback (AI unavailable)</b>"}.</span></div></div>
      ${fc.factors.length ? `<div style="font-size:12px;font-weight:600;margin-top:4px">Pushing risk up</div>` : ""}
      ${fc.factors.map((f) => `<div class="factor"><div>${esc(f.text)}</div><div class="bar"><b style="width:${f.impact ? Math.round(100 * f.impact / maxImp) : 50}%"></b></div></div>`).join("")}
      ${fc.reassuring && fc.reassuring.length ? `<div style="font-size:12px;font-weight:600;margin-top:6px">Reassuring</div>` + fc.reassuring.map((f) => `<div class="factor good"><div>${esc(f.text)}</div><div class="bar"><b style="width:60%"></b></div></div>`).join("") : ""}
      <div class="note">Explanation is exact: the model is logistic regression, so each bar is that factor's actual contribution. Trained on synthetic data only - a decision-support signal, not a diagnosis.</div>`;
  }

  // hub info
  const hub = p.hub;
  $("#hubinfo").textContent = hub ? `Hub ${hub.device_id}: ${hub.online ? "online" : "OFFLINE " + hub.offline_for_s + "s"} · buffer ${hub.buffered} · battery ${hub.battery}%${hub.scenario ? " · scenario: " + hub.scenario + " (" + Math.round(hub.progress * 100) + "%)" : ""}` : "";
  if (!$("#scnSel").options.length) $("#scnSel").innerHTML = Object.entries(S.scenarios).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#deadToggle").checked = S.chaos.dead_devices.includes(sel);
}

function renderAlerts() {
  const list = S.alerts.filter((a) => alertFilter === "all" || a.status !== "RESOLVED");
  if (!list.length) { $("#alerts").innerHTML = `<div class="empty">No open alerts. ✨<br>Alerts are de-duplicated, need confirmed data, and ignore sensor artifacts - so when one appears, it matters.</div>`; return; }
  $("#alerts").innerHTML = list.map((a) => {
    const p = S.patients.find((x) => x.id === a.patient_id);
    const max = { CRITICAL: 4, HIGH: 3, WATCH: 2, LOW: 1 }[a.severity];
    const ladder = p.contacts.filter((c) => c.level <= max).map((c) => `<span class="step ${c.level < a.level ? "done" : c.level === a.level ? "cur" : ""}" title="${esc(c.role)} · ${esc(c.phone)}">L${c.level} ${esc(c.name.split(" (")[0])}</span>`).join(`<span class="arrow">→</span>`);
    const d = a.detail || {};
    const status = a.status === "OPEN" ? (a.escalates_in_s != null ? `<span class="countdown">escalates in ${Math.max(0, a.escalates_in_s)}s</span>` : "awaiting response") :
      a.status === "ACKNOWLEDGED" ? `acknowledged by ${esc(a.acked_by)} in ${Math.round(a.acked_real - a.created_real)}s` :
        `resolved by ${esc(a.resolved_by)} · ${esc(a.action || "")}`;
    return `<div class="alert t-${a.severity} st-${a.status}">
      <div style="display:flex;justify-content:space-between;gap:6px"><span class="t">${esc(a.title)}</span><span class="badge t-${a.severity}">${a.severity}</span></div>
      <div class="m"><span>${esc(a.patient_name)}</span><span>#${a.id} · ${a.kind}</span><span>${fmtTime(a.created_ts)}</span>${a.improving && a.status !== "RESOLVED" ? `<span style="color:var(--stable)">▼ improving</span>` : ""}</div>
      ${d.why && d.why.length ? `<ul>${d.why.slice(0, 3).map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      ${d.factors && d.factors.length && a.kind === "EARLY_WARNING" ? `<ul>${d.factors.filter(f => f.feature !== "news2_now").slice(0, 3).map((f) => `<li>${esc(f.text)}</li>`).join("")}</ul>` : ""}
      ${d.recommendation ? `<div class="rec">➜ ${esc(d.recommendation)}</div>` : ""}
      ${a.status !== "RESOLVED" ? `<div class="ladder">${ladder}</div>` : ""}
      <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;font-size:12px"><span>${status}</span>
      <span>${a.status === "OPEN" ? `<button class="btn small primary" data-ack="${a.id}">Acknowledge</button> ` : ""}${a.status !== "RESOLVED" ? `<button class="btn small" data-res="${a.id}">Resolve…</button>` : ""}</span></div>
    </div>`;
  }).join("");
  $$("[data-ack]").forEach((b) => b.onclick = async () => { await api(`/api/alerts/${b.dataset.ack}/ack`, { by: operator() }); poll1(); });
  $$("[data-res]").forEach((b) => b.onclick = () => resolveModal(+b.dataset.res));
}

function renderActivity() {
  const el = $("#activity");
  if (actTab === "msgs") {
    el.innerHTML = S.messages.length ? S.messages.map((m) => `<div class="msg"><div class="ch">${esc(m.channel)} → ${esc(m.to_name)} (${esc(m.to_phone)})</div>${esc(m.text)}<div style="color:var(--muted);font-size:11px">${fmtTime(m.ts)} · ${ago(m.real)}</div></div>`).join("") : `<div class="empty">No notifications sent yet.</div>`;
  } else if (actTab === "resp") {
    const r = S.responses;
    el.innerHTML = `<div class="kpis" style="margin-bottom:10px"><div class="kpi" style="border-color:var(--line)"><b>${r.median_ack_s != null ? r.median_ack_s + "s" : "-"}</b><span>median time to acknowledge</span></div>
      <div class="kpi" style="border-color:var(--line)"><b>${r.escalated}/${r.alerts_total}</b><span>alerts needed escalation</span></div></div>
      ${Object.entries(r.per_responder).map(([k, v]) => `<div class="msg"><b>${esc(k)}</b> - ${v.n} alerts acknowledged, median ${v.median_ack_s}s</div>`).join("") || `<div class="empty">No responses yet.</div>`}
      <div style="margin-top:8px">${S.patients.map((p) => `<div class="msg"><b>${esc(p.name)}</b>: ${p.on_duty ? "on duty - " + esc(p.on_duty.caregiver) + " since " + fmtTime(p.on_duty.since) : "nobody checked in"} · next round ${p.round_due ? fmtTime(p.round_due) : "-"}</div>`).join("")}</div>`;
  } else {
    el.innerHTML = S.audit.map((a) => `<div class="audit">${fmtTime(a.ts)} [${esc(a.actor)}] ${esc(a.what)}</div>`).join("");
  }
}

function renderLab() {
  $$("[data-chaos]").forEach((i) => i.checked = !!S.chaos[i.dataset.chaos]);
  const m = S.metrics;
  const buffered = S.patients.reduce((s, p) => s + ((p.hub && p.hub.buffered) || 0), 0);
  const k = [
    [m.received, "readings ingested"], [m.duplicates, "duplicates safely dropped"],
    [m.late, "out-of-order readings reconciled"], [buffered, "readings buffered at the edge now"],
    [m.replayed_readings, "readings replayed after outages"], [m.edge_local_alarms, "in-room alarms raised while offline"],
    [m.artifact_alarms_prevented, "sensor-artifact false alarms blocked"], [m.unconfirmed_suppressed, "single-reading spikes not escalated"],
    [m.scale2_prevented, "COPD false alarms avoided (Scale 2)"], [`${m.ml_breaker}${m.ml_fallbacks ? " · " + m.ml_fallbacks + " fallbacks" : ""}`, "AI circuit breaker"],
  ];
  $("#kpis").innerHTML = k.map(([v, l]) => `<div class="kpi"><b>${v}</b><span>${l}</span></div>`).join("");
  const mm = S.model && S.model.metrics;
  if (mm) {
    $("#modelcard").innerHTML = `<table style="width:100%;border-collapse:collapse">
      <tr><td>Gradual deteriorations flagged before NEWS2 ≥ 5</td><td><b>${mm.ml_flagged_before_news2_5}</b></td></tr>
      <tr><td>Median warning time - AI</td><td><b>${mm.ml_median_lead_h} h</b></td></tr>
      <tr><td>Median warning time - NEWS2 ≥ 3 alone</td><td>${mm.news2_ge3_median_lead_h} h</td></tr>
      <tr><td>False early-warnings per stable patient-day - AI</td><td><b>${mm.ml_false_early_warnings_per_stable_patient_day}</b></td></tr>
      <tr><td>Alerts per stable patient-day - NEWS2 ≥ 3</td><td>${mm.news2_ge3_alerts_per_stable_patient_day}</td></tr>
      <tr><td>Sudden events (acute hypoxia) flagged early</td><td>${(mm.per_scenario.acute_hypoxia || {}).flagged_early ?? "-"}/${(mm.per_scenario.acute_hypoxia || {}).n ?? "-"}</td></tr>
      </table><div class="note" style="margin-top:6px">Honest limits: sudden events can't be forecast - NEWS2 + confirmation catch those in real time. ${esc(S.model.caveat)}</div>`;
  }
}

function notifyNew() {
  for (const a of S.alerts) {
    const prev = seenAlerts.get(a.id);
    if (!firstLoad && a.status !== "RESOLVED" && (!prev || prev !== a.severity)) {
      toast(`${a.severity}: ${a.patient_name} - ${a.title}`, a.severity);
      if (sound && (a.severity === "HIGH" || a.severity === "CRITICAL")) beep(a.severity === "CRITICAL" ? 3 : 1);
      if (window.Notification && Notification.permission === "granted" && a.severity !== "LOW") new Notification(`HomeWard ${a.severity}`, { body: `${a.patient_name}: ${a.title}` });
    }
    seenAlerts.set(a.id, a.severity);
  }
  firstLoad = false;
}

/* ------------------------------------------------------------------ charts */
function setupCanvas(c) {
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  if (c.width !== w * dpr || c.height !== h * dpr) { c.width = w * dpr; c.height = h * dpr; }
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  return [g, w, h];
}
function drawCharts() {
  if (!H || !S) return;
  const p = S.patients.find((x) => x.id === sel); if (!p) return;
  const now = S.sim_time, t0 = now - rangeH * 3600;
  const rows = H.readings.filter((r) => r.ts >= t0);
  const c = $("#chart"); const [g, w, h] = setupCanvas(c);
  const L = 40, R = 10, T = 10, B = 22, pw = w - L - R, ph = h - T - B;
  const v = vital, meta = VITALS[v];
  let band = meta.band ? [...meta.band] : null;
  if (v === "spo2" && p.spo2_scale === 2) band = [88, 92];
  if (v === "sbp") band = [111, 160];
  const good = rows.filter((r) => !r.artifact && r[v] != null), bad = rows.filter((r) => r.artifact && r[v] != null);
  const vals = rows.map((r) => r[v]).filter((x) => x != null);
  if (band) vals.push(...band);
  if (p.baseline && p.baseline[v] != null) vals.push(p.baseline[v]);
  let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  const X = (ts) => L + (ts - t0) / (now - t0) * pw, Y = (y) => T + (1 - (y - lo) / (hi - lo)) * ph;
  g.font = "11px system-ui"; g.fillStyle = "#64748b"; g.strokeStyle = "#eef2f7"; g.lineWidth = 1;
  for (let i = 0; i <= 4; i++) { const y = lo + (hi - lo) * i / 4; g.beginPath(); g.moveTo(L, Y(y)); g.lineTo(w - R, Y(y)); g.stroke(); g.fillText(v === "temp" || v === "weight" ? y.toFixed(1) : Math.round(y), 4, Y(y) + 4); }
  for (let i = 0; i <= 6; i++) { const ts = t0 + (now - t0) * i / 6; g.fillText(fmtTime(ts), Math.max(L - 10, Math.min(w - 62, X(ts) - 24)), h - 6); }
  if (band) { g.fillStyle = "rgba(16,185,129,.08)"; g.fillRect(L, Y(Math.min(band[1], hi)), pw, Y(Math.max(band[0], lo)) - Y(Math.min(band[1], hi))); }
  if (p.baseline && p.baseline[v] != null) { g.strokeStyle = "#0f766e"; g.setLineDash([5, 4]); g.beginPath(); g.moveTo(L, Y(p.baseline[v])); g.lineTo(w - R, Y(p.baseline[v])); g.stroke(); g.setLineDash([]); }
  g.strokeStyle = "#2563eb"; g.lineWidth = 1.8; g.beginPath(); let prev = null;
  for (const r of good) { const x = X(r.ts), y = Y(r[v]); if (prev && r.ts - prev.ts > 20 * 60) { g.stroke(); g.beginPath(); g.moveTo(x, y); } else if (!prev) g.moveTo(x, y); else g.lineTo(x, y); prev = r; }
  g.stroke();
  g.strokeStyle = "#94a3b8"; g.lineWidth = 1.5;
  for (const r of bad) { const x = X(r.ts), y = Y(Math.max(lo, Math.min(hi, r[v]))); g.beginPath(); g.moveTo(x - 3, y - 3); g.lineTo(x + 3, y + 3); g.moveTo(x + 3, y - 3); g.lineTo(x - 3, y + 3); g.stroke(); }
  for (const r of rows.filter((r) => r.late)) { g.fillStyle = "#f59e0b"; g.beginPath(); g.arc(X(r.ts), Y(r[v] ?? lo), 2.2, 0, 7); g.fill(); }
  g.fillStyle = "#142033"; g.font = "600 12px system-ui"; g.fillText(`${meta.label} (${meta.unit})`, L + 6, T + 12);

  // chart2: NEWS2 + risk trail
  const c2 = $("#chart2"); const [g2, w2, h2] = setupCanvas(c2);
  const ph2 = h2 - 26, Y2 = (f) => 8 + (1 - f) * ph2;
  const trail = (H.trail || []).filter((x) => x.at >= t0);
  g2.font = "11px system-ui"; g2.fillStyle = "#64748b";
  g2.strokeStyle = "#eef2f7"; [0, .5, 1].forEach((f) => { g2.beginPath(); g2.moveTo(L, Y2(f)); g2.lineTo(w2 - R, Y2(f)); g2.stroke(); });
  g2.fillText("100%", 4, Y2(1) + 4); g2.fillText("0", 4, Y2(0) + 4);
  const thr = p.forecast && p.forecast.threshold < 1 ? p.forecast.threshold : null;
  if (thr) { g2.strokeStyle = "rgba(124,58,237,.35)"; g2.setLineDash([4, 4]); g2.beginPath(); g2.moveTo(L, Y2(thr)); g2.lineTo(w2 - R, Y2(thr)); g2.stroke(); g2.setLineDash([]); }
  const line = (key, color, f) => { g2.strokeStyle = color; g2.lineWidth = 1.8; g2.beginPath(); let first = true; for (const x of trail) { if (x[key] == null) continue; const px = X(x.at), py = Y2(f(x[key])); first ? g2.moveTo(px, py) : g2.lineTo(px, py); first = false; } g2.stroke(); };
  line("news2", "#ea580c", (n) => Math.min(1, n / 14));
  line("prob", "#7c3aed", (p) => p);
  g2.fillStyle = "#ea580c"; g2.fillText("NEWS2 (0-14)", w2 - 90, 18);
}

/* ------------------------------------------------------------------ notes */
const EXAMPLES = ["Saans phool rahi hai, khana nahi khaya", "She seems confused today", "बुखार है और कमजोरी", "Ankles more swollen, less urine", "No chest pain, feeling better", "Papa gir gaye bathroom mein"];
function renderNotes() {
  if (!H) return;
  const items = [...(H.notes || []).map((n) => ({ ...n, kind: "note" })), ...(H.rounds || []).map((r) => ({ ...r, kind: "round" }))].sort((a, b) => b.ts - a.ts);
  $("#notelist").innerHTML = items.length ? items.map((n) => {
    if (n.kind === "round") return `<div class="n">✅ <b>Round check</b> by ${esc(n.caregiver)} - consciousness ${esc(n.acvpu)}<div class="meta">${fmtTime(n.ts, true)}</div></div>`;
    const a = JSON.parse(n.analysis || "{}");
    const chips = (a.signals || []).filter((s) => s.weight > 0).map((s) => `<span class="chip ${s.red_flag ? "red" : "amber"}">${esc(s.label)}</span>`).join(" ") +
      (a.negated || []).map((s) => `<span class="chip" title="negated">no ${esc(s.label.toLowerCase())}</span>`).join(" ");
    return `<div class="n">📝 ${esc(n.text)}<div class="chips">${chips}</div><div class="meta">${esc(n.author)} · ${fmtTime(n.ts, true)}</div></div>`;
  }).join("") : `<div class="empty">No notes yet.</div>`;
}

/* ------------------------------------------------------------------ modals */
function modal(html) { $("#modalCard").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").onclick = (e) => { if (e.target.id === "modal") closeModal(); };

function resolveModal(id) {
  const a = S.alerts.find((x) => x.id === id);
  modal(`<h3>Resolve alert #${id}</h3><div style="font-size:13px">${esc(a.patient_name)} - ${esc(a.title)}</div>
    <label>What was done?</label><select id="resAct">
      <option>Checked on patient - condition reviewed</option><option>Gave prescribed medicine</option>
      <option>Called doctor / tele-consult</option><option>Arranged hospital transfer / ambulance</option>
      <option>Sensor re-fitted / technical issue</option><option>False alarm - patient fine</option></select>
    <label>Note (feeds back into the patient's context)</label><textarea id="resNote" placeholder="e.g. Spoke to Dr. Rao, started paracetamol, recheck in 1 h"></textarea>
    <label>Your name</label><input id="resBy" value="${esc(operator())}">
    <div class="row"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn primary" id="resGo">Resolve</button></div>`);
  $("#resGo").onclick = async () => { store.set("hw.operator", $("#resBy").value); await api(`/api/alerts/${id}/resolve`, { by: $("#resBy").value || operator(), action: $("#resAct").value, note: $("#resNote").value }); closeModal(); poll1(); };
}

$("#sbarBtn").onclick = async () => {
  const txt = await api(`/api/patients/${sel}/sbar`);
  const p = S.patients.find((x) => x.id === sel);
  modal(`<h3>📞 SBAR handover - ready to read out</h3><div style="font-size:12px;color:var(--muted);margin-bottom:6px">The structured format clinicians use for phone handovers. Generated from live data.</div><pre id="sbarTxt">${esc(txt)}</pre>
    <div class="row"><button class="btn" onclick="closeModal()">Close</button><button class="btn" id="copySbar">Copy</button><a class="btn primary" style="text-decoration:none" href="tel:${esc(p.contacts[2].phone)}">Call care desk</a></div>`);
  $("#copySbar").onclick = () => navigator.clipboard && navigator.clipboard.writeText(txt).then(() => toast("SBAR copied"));
};
$("#roundBtn").onclick = () => {
  const p = S.patients.find((x) => x.id === sel);
  modal(`<h3>✅ Round check - ${esc(p.name)}</h3>
    <label>How does the patient respond? (ACVPU)</label><select id="rAcvpu">
      <option value="A">Awake, alert, talking normally</option><option value="C">New confusion / not making sense</option>
      <option value="V">Only responds to voice</option><option value="P">Only responds to pain</option><option value="U">Unresponsive</option></select>
    <label>Anything else? (optional)</label><textarea id="rNote" placeholder="Medicines given, eating, mood…"></textarea>
    <label>Your name</label><input id="rBy" value="${esc(p.contacts[0].name)}">
    <div class="row"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn primary" id="rGo">Save round</button></div>`);
  $("#rGo").onclick = async () => { await api(`/api/patients/${sel}/rounds`, { caregiver: $("#rBy").value, acvpu: $("#rAcvpu").value, note: $("#rNote").value }); closeModal(); poll1(); toast("Round check saved"); };
};
$("#dutyBtn").onclick = async () => {
  const p = S.patients.find((x) => x.id === sel);
  await api(`/api/patients/${sel}/checkin`, { caregiver: p.contacts[0].name, on: !p.on_duty }); poll1();
};
$("#noteBtn").onclick = async () => {
  const text = $("#noteText").value.trim(); if (!text) return;
  const author = $("#noteAuthor").value.trim() || S.patients.find((x) => x.id === sel).contacts[0].name;
  const a = await api(`/api/patients/${sel}/notes`, { author, text });
  $("#understood").innerHTML = `<b>Understood:</b> ${(a.signals || []).map((s) => `<span class="chip ${s.red_flag ? "red" : "amber"}">${esc(s.label)}</span>`).join(" ") || "no concerning signs"}${(a.negated || []).length ? ` · <b>ruled out:</b> ${a.negated.map((n) => esc(n.label)).join(", ")}` : ""}${a.suggests_new_confusion ? ` · <b>counts as new confusion in NEWS2</b>` : ""}`;
  $("#noteText").value = ""; poll1();
};
$("#examples").innerHTML = EXAMPLES.map((e) => `<button>${esc(e)}</button>`).join("");
$$("#examples button").forEach((b) => b.onclick = () => $("#noteText").value = b.textContent);

/* ------------------------------------------------------------------ controls */
$$(".speed button[data-speed]").forEach((b) => b.onclick = () => api("/api/sim", { speed: +b.dataset.speed, paused: false }).then(poll1));
$("#pause").onclick = () => api("/api/sim", { paused: !S.paused }).then(poll1);
$("#labBtn").onclick = () => { $("#lab").classList.toggle("hidden"); store.set("hw.lab", $("#lab").classList.contains("hidden") ? "0" : "1"); };
if (store.get("hw.lab", "0") === "1") $("#lab").classList.remove("hidden");
$$("[data-chaos]").forEach((i) => i.onchange = () => api("/api/chaos", { flag: i.dataset.chaos, value: i.checked }).then(poll1));
$("#deadToggle").onchange = (e) => api("/api/chaos", { flag: "device_dead", value: e.target.checked, patient_id: sel }).then(poll1);
$("#scnBtn").onclick = () => api("/api/sim", { patient_id: sel, scenario: $("#scnSel").value }).then(() => toast(`Scenario started for ${sel}`));
$("#artBtn").onclick = () => api("/api/sim", { patient_id: sel, artifact: true }).then(() => toast("Finger sensor displaced for 20 sim-min"));
$("#resetBtn").onclick = () => api("/api/sim", { patient_id: sel, reset: true }).then(() => toast("Patient recovering"));
$$("#rangeTabs button").forEach((b) => b.onclick = () => { rangeH = +b.dataset.h; $$("#rangeTabs button").forEach((x) => x.classList.toggle("on", x === b)); drawCharts(); });
$$("#alertTabs button").forEach((b) => b.onclick = () => { alertFilter = b.dataset.f; $$("#alertTabs button").forEach((x) => x.classList.toggle("on", x === b)); renderAlerts(); });
$$("#actTabs button").forEach((b) => b.onclick = () => { actTab = b.dataset.t; $$("#actTabs button").forEach((x) => x.classList.toggle("on", x === b)); renderActivity(); });
$("#soundBtn").onclick = () => { sound = !sound; $("#soundBtn").textContent = sound ? "🔊 Sound on" : "🔈 Sound off"; if (sound) { beep(1); if (window.Notification && Notification.permission === "default") Notification.requestPermission(); } };
window.addEventListener("resize", drawCharts);
async function poll1() { try { S = await api("/api/state"); render(); } catch {} }

let actx;
function beep(n) {
  try {
    actx = actx || new AudioContext();
    for (let i = 0; i < n; i++) { const o = actx.createOscillator(), g = actx.createGain(); o.frequency.value = 880; o.connect(g); g.connect(actx.destination); const t = actx.currentTime + i * 0.3; g.gain.setValueAtTime(0.2, t); g.gain.exponentialRampToValueAtTime(0.001, t + 0.25); o.start(t); o.stop(t + 0.26); }
  } catch {}
}
function toast(msg, sev) {
  const d = document.createElement("div"); d.className = `toast ${sev ? "t-" + sev : ""}`; d.textContent = msg;
  $("#toasts").appendChild(d); setTimeout(() => d.remove(), 6000);
}

poll(); pollHistory();
