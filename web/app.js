/* HomeWard care-desk dashboard. Vanilla JS, no build step, no chart library. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = { get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch {} } };
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

let S = null, H = null;
let sel = store.get("hw.sel", "P-101"), tab = "overview";
let rangeH = 24, vital = "rr", alertFilter = "open", actTab = "msgs";
let sound = false, fails = 0, firstLoad = true;
const seenAlerts = new Map(), prevVals = {};

const TIER_TEXT = { STABLE: "Stable", WATCH: "Watch closely", HIGH: "Urgent review", CRITICAL: "Emergency", UNKNOWN: "Status unknown" };
const VITALS = {
  hr: { label: "Heart rate", unit: "bpm", band: [51, 90] }, rr: { label: "Breathing", unit: "/min", band: [12, 20] },
  spo2: { label: "SpO₂", unit: "%", band: [96, 100] }, sbp: { label: "Blood pressure", unit: "mmHg", band: [111, 219] },
  temp: { label: "Temperature", unit: "°C", band: [36.1, 38.0] }, weight: { label: "Weight", unit: "kg", band: null },
};
const RISK_TEXT = { STABLE: "Low risk", WATCH: "Medium risk", HIGH: "High risk", CRITICAL: "High risk", UNKNOWN: "Unknown" };
const operator = () => store.get("hw.operator", "Care desk - Nurse Asha");
const initials = (n) => n.split(" ").map((x) => x[0]).slice(0, 2).join("");
const fmtTime = (ts, day = false) => { const d = new Date(ts * 1000); const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); return day ? d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) + " · " + t : t; };
const pct = (p) => `${Math.min(99, Math.max(1, Math.round(p * 100)))}%`;
const ago = (realTs) => { const s = Math.max(0, Date.now() / 1000 - realTs); return s < 60 ? `${Math.round(s)}s ago` : `${Math.round(s / 60)}m ago`; };
const tierOf = (p) => ["STALE", "OFFLINE", "NO_DATA"].includes(p.data_state) ? "UNKNOWN" : p.tier;
const patient = (id = sel) => S && S.patients.find((x) => x.id === id);

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  if (!r.ok) throw new Error(await r.text());
  return (r.headers.get("content-type") || "").includes("json") ? r.json() : r.text();
}

/* ------------------------------------------------------------ live data: SSE with polling fallback */
function online(ok) {
  $("#offline").classList.toggle("hidden", ok);
  $("#conn").classList.toggle("bad", !ok);
  $("#conn span").textContent = ok ? "Live" : "Reconnecting";
}
function startStream() {
  if (!window.EventSource) return poll();
  const es = new EventSource("/api/stream");
  es.onmessage = (e) => { S = JSON.parse(e.data); fails = 0; online(true); render(); };
  es.onerror = () => { fails++; if (fails > 2) { online(false); es.close(); setTimeout(startStream, 2000); } };
}
async function poll() {
  try { S = await api("/api/state"); fails = 0; online(true); render(); } catch { fails++; if (fails > 2) online(false); }
  setTimeout(poll, fails ? Math.min(5000, 1000 * fails) : 1000);
}
async function poll1() { try { S = await api("/api/state"); render(); } catch {} }
async function loadHistory() { try { H = await api(`/api/patients/${sel}/history?hours=48`); drawCharts(); renderNotes(); renderTimeline(); } catch {} }
function historyLoop() { loadHistory().finally(() => setTimeout(historyLoop, 2500)); }

/* ------------------------------------------------------------ render */
function render() {
  if (!S) return;
  $("#clock").textContent = fmtTime(S.sim_time, true);
  $$("#speedSeg button[data-speed]").forEach((b) => b.classList.toggle("on", !S.paused && +b.dataset.speed === S.speed));
  $("#pause").textContent = S.paused ? "Resume" : "Pause";
  renderKpis(); renderPipeline(); renderList(); renderDetail(); renderAlerts(); renderActivity(); renderLab(); notifyNew();
}

const pipePrev = {}; let rpm = { t: Date.now(), n: null, v: 0 };
function renderPipeline() {
  const m = S.metrics, pts = S.patients, open = S.alerts.filter((a) => a.status !== "RESOLVED");
  const now = Date.now(); if (rpm.n == null) rpm = { t: now, n: m.received, v: 0 };
  else if (now - rpm.t > 5000) { rpm = { t: now, n: m.received, v: Math.round((m.received - rpm.n) / ((now - rpm.t) / 60000)) }; }
  const live = pts.filter((p) => p.data_state === "LIVE").length;
  const queued = pts.reduce((s, p) => s + ((p.hub && p.hub.buffered) || 0), 0);
  const acked = S.alerts.filter((a) => a.acked_real).length;
  const nodes = [
    ["sense", "📡", "Sensors", `${pts.length * 7} devices · ${rpm.v || "…"} readings/min`, false],
    ["edge", "🏠", "Edge hubs", `${live}/${pts.length} online · ${queued} queued`, live < pts.length || queued > 0],
    ["plat", "🖥️", "Platform", `${m.received} ingested · ${m.duplicates} dupes dropped`, false],
    ["ai", "🧠", "AI risk", `${pts.filter((p) => p.forecast && p.forecast.source !== "calibrating").length} forecasts · ${m.ml_breaker === "closed" ? "model OK" : "rule fallback"}`, m.ml_breaker !== "closed"],
    ["alert", "🚨", "Alerts", `${open.length} open · ${S.responses.escalated} escalated`, open.some((a) => a.severity === "CRITICAL")],
    ["resp", "🙋", "Response", `${acked} acknowledged · median ${S.responses.median_ack_s ?? "-"}s`, false],
  ];
  const sig = { sense: m.received, edge: queued, plat: m.received, ai: m.received, alert: open.length, resp: acked };
  $("#pipeline").innerHTML = nodes.map(([k, ic, t, sub, bad], i) => {
    const pulse = pipePrev[k] !== undefined && pipePrev[k] !== sig[k]; pipePrev[k] = sig[k];
    return `${i ? `<div class="parr">→</div>` : ""}<div class="pnode ${pulse ? "pulse" : ""} ${bad ? "bad" : ""}" id="pn-${k}"><div class="ic">${ic}</div><div><b>${t}</b><span>${sub}</span></div></div>`;
  }).join("");
}

function renderKpis() {
  const open = S.alerts.filter((a) => a.status !== "RESOLVED");
  const crit = open.filter((a) => a.severity === "CRITICAL" || a.severity === "HIGH").length;
  const live = S.patients.filter((p) => p.data_state === "LIVE").length;
  $("#kpis").innerHTML = [
    [`${S.patients.length}`, "homes"], [`${live}/${S.patients.length}`, "live"],
    [open.length, "open alerts", crit ? "alarm" : ""], [S.responses.median_ack_s != null ? S.responses.median_ack_s + "s" : "-", "median ack"],
  ].map(([v, l, c]) => `<div class="kpi-chip ${c || ""}"><b>${v}</b>${l}</div>`).join("");
}

function devBadges(p) {
  const h = p.hub || {}, out = [];
  if (p.data_state !== "LIVE") out.push(`<span class="dev bad">⚠ no data ${p.data_age_min ?? ""}m</span>`);
  else if (h.online === false) out.push(`<span class="dev warn">📶 offline · ${h.buffered} queued</span>`);
  else out.push(`<span class="dev ok">📶 hub live</span>`);
  if (h.real_sensor) out.push(`<span class="dev hw">🔌 real sensor</span>`);
  if (h.local_alarm) out.push(`<span class="dev bad">🔔 room alarm</span>`);
  if (p.dose_missed) out.push(`<span class="dev warn">💊 dose missed</span>`);
  if (p.bed && !p.bed.in_bed) out.push(`<span class="dev warn">🛏 out of bed</span>`);
  const eq = p.equipment || {};
  if (eq.hub_power === "battery") out.push(`<span class="dev warn">⚡ battery ${Math.round(eq.hub_battery)}%</span>`);
  if (eq.concentrator) out.push(eq.concentrator.running && eq.concentrator.purity_pct >= 85 ? `<span class="dev ok">🫁 O₂ ${eq.concentrator.flow_lpm} L/min</span>` : `<span class="dev bad">🫁 O₂ fault</span>`);
  return out.join("");
}

function renderList() {
  const counts = {};
  $("#plist").innerHTML = S.patients.map((p) => {
    const t = tierOf(p); counts[t] = (counts[t] || 0) + 1;
    const l = p.latest || {}, fc = p.forecast || {};
    return `<div class="pcard t-${t} ${p.id === sel ? "sel" : ""}" data-id="${p.id}">
      <div class="prow"><div class="avatar">${initials(p.name)}</div>
        <div style="min-width:0;flex:1"><div style="display:flex;justify-content:space-between;gap:6px;align-items:center"><span class="pname">${esc(p.name)}</span><span class="badge t-${t}">${RISK_TEXT[t]}</span></div>
        <div class="pcond">${p.age}${p.sex} · ${esc(p.condition)}</div></div></div>
      <div class="pstats"><div><span>NEWS2</span><b>${p.news2 ? p.news2.total : "-"}</b></div><div><span>24h risk</span><b>${fc.source === "calibrating" || fc.prob == null ? "…" : pct(fc.prob)}</b></div>
        <div><span>SpO₂</span><b>${l.spo2 != null ? Math.round(l.spo2) : "-"}</b></div><div><span>Resp</span><b>${l.rr != null ? Math.round(l.rr) : "-"}</b></div></div>
      <canvas class="spark" data-spark="${p.id}"></canvas>
      <div class="devrow">${devBadges(p)}</div></div>`;
  }).join("");
  $$(".pcard").forEach((el) => el.onclick = () => select(el.dataset.id));
  $$("[data-spark]").forEach((c) => drawSpark(c, patient(c.dataset.spark)));
  const order = ["CRITICAL", "HIGH", "WATCH", "UNKNOWN", "STABLE"];
  $("#wardSummary").textContent = order.filter((k) => counts[k]).map((k) => `${counts[k]} ${TIER_TEXT[k].toLowerCase()}`).join(" · ");
}

function select(id) { sel = id; store.set("hw.sel", id); H = null; render(); loadHistory(); }
function setTab(t) {
  tab = t;
  $$("#ptabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  ["overview", "care", "timeline"].forEach((x) => $(`#tab-${x}`).classList.toggle("hidden", x !== t));
  if (t === "overview") drawCharts();
}

function renderDetail() {
  const p = patient() || S.patients[0]; if (!p) return; sel = p.id;
  const t = tierOf(p);
  $("#pavatar").textContent = initials(p.name); $("#pavatar").className = `avatar lg t-${t}`;
  $("#pname").textContent = `${p.name}, ${p.age}${p.sex}`;
  $("#pcond").textContent = p.condition;
  $("#pchips").innerHTML = [
    p.on_oxygen ? `<span class="chip blue">Home oxygen</span>` : "",
    p.spo2_scale === 2 ? `<span class="chip blue" title="Prescribed target 88-92%">NEWS2 SpO₂ scale 2</span>` : "",
    p.hub && p.hub.real_sensor ? `<span class="chip blue">🔌 Real sensor connected</span>` : "",
    `<span class="chip">Primary: ${esc(p.contacts[0].name)}</span>`,
    p.on_duty ? `<span class="chip green">${p.on_duty.verified ? "✓ Verified at bedside" : "On duty"}: ${esc(p.on_duty.caregiver.split(" (")[0])}</span>` : `<span class="chip amber">No caregiver checked in</span>`,
    p.round_due ? `<span class="chip ${S.sim_time > p.round_due ? "red" : ""}">Round due ${fmtTime(p.round_due)}</span>` : "",
  ].join("");
  $("#dutyBtn").textContent = p.on_duty ? "Check out" : "Check in";
  const sb = $("#statusbar"); sb.className = `status t-${t}`;
  let reasons = p.why && p.why.length ? [...p.why] : ["All vital signs are within this patient's expected range."];
  if (t === "UNKNOWN") reasons = [p.data_state === "NO_DATA" ? "No data received yet." : `No data for ${p.data_age_min} min. Last known level: ${TIER_TEXT[p.tier] || p.tier}. We never assume "stable" on old data, so check in person.`];
  if (p.quality && p.quality.artifact_now) reasons.push(`Latest reading ignored: ${p.quality.last_issue}`);
  if (p.scale1_would_alarm) reasons.push("A standard SpO₂ threshold would alarm now. This patient's prescribed 88-92% target says otherwise, so the false alarm was avoided.");
  const lvl = t === "UNKNOWN" ? "UNKNOWN" : p.risk_level;
  sb.innerHTML = `<div><div class="big">${lvl === "UNKNOWN" ? "Status unknown" : lvl[0] + lvl.slice(1).toLowerCase() + " risk"}</div><div class="note" style="margin:0">${TIER_TEXT[t]}</div></div>
    <div style="flex:1;min-width:240px">${p.risk_reason && t !== "UNKNOWN" ? `<div style="font-weight:600;margin-bottom:3px">${esc(p.risk_reason)}</div>` : ""}<ul>${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>`;

  // tiles
  const l = p.latest || {}, b = p.baseline || {}, comp = (p.news2 && p.news2.components) || {};
  const real = p.hub && p.hub.real_sensor;
  $("#tiles").innerHTML = Object.entries(VITALS).map(([k, v]) => {
    let val = l[k];
    if (k === "sbp") val = l.sbp != null ? `${Math.round(l.sbp)}<small>/${Math.round(l.dbp)}</small>` : "-";
    else if (val != null) val = k === "temp" || k === "weight" ? (+val).toFixed(1) : Math.round(val);
    const pts = comp[k];
    const key = `${p.id}.${k}`, changed = prevVals[key] !== undefined && prevVals[key] !== val; prevVals[key] = val;
    let base = b[k] != null ? `usual ${k === "temp" || k === "weight" ? (+b[k]).toFixed(1) : Math.round(b[k])}` : "";
    if (k === "weight" && b.weight != null && l.weight != null) { const d = l.weight - b.weight; base += ` (${d >= 0 ? "+" : ""}${d.toFixed(1)})`; }
    const hw = real && ["hr", "spo2", "temp"].includes(k) ? `<div class="src">● SENSOR</div>` : "";
    return `<button class="tile ${vital === k ? "sel" : ""} ${changed ? "flash" : ""}" data-v="${k}"><div class="lbl">${v.label}${pts != null ? `<span class="pts p${pts}">${pts}</span>` : ""}</div>
      <div class="val">${val ?? "-"} <small>${k === "sbp" ? "" : v.unit}</small></div><div class="base">${base}</div>${hw}</button>`;
  }).join("");
  $$(".tile").forEach((el) => el.onclick = () => { vital = el.dataset.v; renderDetail(); drawCharts(); });

  // NEWS2
  $("#n2").innerHTML = p.news2_rows ? `<table class="n2">${p.news2_rows.map((r) => `<tr><td>${esc(r.label)}</td><td>${esc(r.value)}</td><td><span class="pts p${r.points}">${r.points}</span></td></tr>`).join("")}
      <tr class="total"><td>Total NEWS2</td><td>${esc(p.news2.band)}</td><td>${p.news2.total}</td></tr></table>
      <div class="note"><b>Clinical response:</b> ${esc(p.news2.response)}</div>
      <div class="note">Consciousness: ${esc(p.acvpu_source || "")}. ${p.spo2_scale === 2 ? "SpO₂ scored on Scale 2 (target 88-92%). " : ""}Escalation needs two consecutive good-quality readings, so one bad sample can't trigger an emergency.</div>` : `<div class="empty">Waiting for data…</div>`;

  // forecast
  const fc = p.forecast;
  if (!fc) $("#fc").innerHTML = `<div class="empty">Waiting for data…</div>`;
  else if (fc.source === "calibrating") $("#fc").innerHTML = `<div class="empty"><span class="big">🧠</span>Learning this patient's personal baseline… ${fc.calibrating_h_left} h left.<br>The forecast compares the patient with themselves.</div>`;
  else {
    const pr = fc.prob, thr = fc.threshold, hot = pr >= thr;
    const maxImp = Math.max(1, ...fc.factors.map((f) => f.impact || 0));
    $("#fc").innerHTML = `<div class="gauge"><div class="ring" style="--p:${pr};--rc:${hot ? "var(--high)" : "var(--stable)"}"><span>${pct(pr)}</span></div>
      <div><b>${hot ? (fc.early_warning ? "Early warning active" : "Rising - confirming for 1 h") : "No early warning"}</b><div class="note" style="margin-top:2px">Chance of reaching NEWS2 ≥ 5 within 24 h · alert at ${pct(thr)} · ${fc.source === "ml" ? "AI model" : "<b style='color:var(--watch)'>rule fallback (AI offline)</b>"}</div></div></div>
      ${fc.factors.length ? `<div class="subhead">Pushing risk up</div>` : ""}
      ${fc.factors.map((f) => `<div class="factor"><div>${esc(f.text)}</div><div class="bar"><b style="transform:scaleX(${f.impact ? (f.impact / maxImp).toFixed(3) : 0.5})"></b></div></div>`).join("")}
      ${fc.reassuring && fc.reassuring.length ? `<div class="subhead">Reassuring</div>` + fc.reassuring.map((f) => `<div class="factor good"><div>${esc(f.text)}</div><div class="bar"><b style="transform:scaleX(.6)"></b></div></div>`).join("") : ""}
      <div class="note">Each bar is the factor's exact contribution in the model (logistic regression with clinically-signed weights), not an after-the-fact guess.</div>`;
  }
  renderDevices(p);
  const hub = p.hub;
  $("#hubinfo").textContent = hub ? `${hub.device_id} · ${hub.online ? "online" : "OFFLINE " + hub.offline_for_s + "s"} · queue ${hub.buffered} · battery ${hub.battery}%${hub.scenario ? ` · ${hub.scenario} ${Math.round(hub.progress * 100)}%` : ""}` : "";
  if (!$("#scnSel").options.length) $("#scnSel").innerHTML = Object.entries(S.scenarios).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#deadToggle").checked = S.chaos.dead_devices.includes(sel);
  $("#deadName").textContent = `(${p.name})`;
}

function renderDevices(p) {
  const h = p.hub || {}, l = p.latest || {};
  const hw = h.real_sensor_values;
  const cards = [
    { ico: "📶", name: "Bedside hub", v: h.online === false ? `Offline ${h.offline_for_s}s · ${h.buffered} readings queued` : p.data_state === "LIVE" ? "Online · streaming every 5 min" : "No data", s: `Battery ${h.battery ?? "-"}% · outbox ${h.buffered ?? 0} · ${h.sent ?? 0} sent`, cls: "" },
    { ico: "🔌", name: "Vitals sensor", v: h.real_sensor ? `ESP32 + MAX30102 · HR ${hw.hr != null ? Math.round(hw.hr) : "-"} · SpO₂ ${hw.spo2 != null ? Math.round(hw.spo2) : "-"}% · ${hw.temp ?? "-"}°C` : "Wearable patch + oximeter", s: h.real_sensor ? (hw.finger === false ? "⚠ finger off the sensor" : `Live hardware · ${esc(hw.firmware || "")}`) : "Connect the ESP32 board to use real readings", cls: h.real_sensor ? "hwcard" : "" },
    { ico: "💊", name: "Smart pill box", v: p.dose_missed ? `⚠ Missed: ${esc(p.dose_missed.med)} (due ${fmtTime(p.dose_missed.due)})` : p.last_dose ? `Last opened ${fmtTime(p.last_dose.ts)}` : "Waiting for first dose", s: p.next_dose ? `Next: ${esc(p.next_dose.med)} at ${fmtTime(p.next_dose.ts)}` : "", cls: "" },
    { ico: "🛏", name: "Bed + room motion", v: p.bed ? (p.bed.in_bed ? "In bed" : `Out of bed since ${fmtTime(p.bed.since)}`) : "In bed", s: "Possible fall: out of bed + no room movement 10 min, or out > 20 min at night", cls: "" },
    ...(p.equipment && p.equipment.concentrator ? [(() => { const c = p.equipment.concentrator; const ok = c.running && c.purity_pct >= 85; return { ico: "🫁", name: "Oxygen concentrator", v: c.running ? `${c.flow_lpm} L/min · ${c.purity_pct}% O₂` : "STOPPED · 0 L/min", s: ok ? `Prescribed ${c.prescribed_lpm} L/min · output normal` : c.running ? "⚠ Low purity: check filter" : "⚠ No oxygen: switch to backup cylinder", cls: ok ? "" : "hwcard" }; })()] : []),
    { ico: "⚡", name: "Power", v: p.equipment && p.equipment.hub_power === "battery" ? `Power cut · hub on battery ${Math.round(p.equipment.hub_battery)}%` : "Mains power", s: "Hub keeps monitoring on battery during power cuts", cls: p.equipment && p.equipment.hub_power === "battery" ? "hwcard" : "" },
    { ico: "⚖️", name: "Smart scale", v: l.weight != null ? `${(+l.weight).toFixed(1)} kg` : "-", s: p.baseline && l.weight != null ? `${(l.weight - p.baseline.weight >= 0 ? "+" : "")}${(l.weight - p.baseline.weight).toFixed(1)} kg vs baseline` : "", cls: "" },
  ];
  $("#devgrid").innerHTML = cards.map((c) => `<div class="devcard ${c.cls}"><div class="h"><span>${c.name}</span><span class="ico">${c.ico}</span></div><div class="v">${c.v}</div><div class="s">${c.s}</div></div>`).join("");
}

function renderAlerts() {
  const list = S.alerts.filter((a) => alertFilter === "all" || a.status !== "RESOLVED");
  if (!list.length) { $("#alerts").innerHTML = `<div class="empty"><span class="big">✓</span>No open alerts.<br>Alerts are confirmed, de-duplicated and artifact-filtered, so when one appears, it matters.</div>`; return; }
  $("#alerts").innerHTML = list.map((a) => {
    const p = patient(a.patient_id);
    const max = { CRITICAL: 4, HIGH: 3, WATCH: 2, LOW: 1 }[a.severity];
    const ladder = p.contacts.filter((c) => c.level <= max).map((c) => `<span class="stp ${c.level < a.level ? "done" : c.level === a.level ? "cur" : ""}" title="${esc(c.role)} · ${esc(c.phone)}">L${c.level} ${esc(c.name.split(" (")[0])}</span>`).join(`<span class="arr">›</span>`);
    const d = a.detail || {};
    const status = a.status === "OPEN" ? (a.escalates_in_s != null ? `<span class="countdown">escalates in ${Math.max(0, a.escalates_in_s)}s</span>` : "awaiting response")
      : a.status === "ACKNOWLEDGED" ? `✓ ${esc(a.acked_by)} · ${Math.round(a.acked_real - a.created_real)}s` : `resolved · ${esc(a.action || "")}`;
    const why = (d.why || []).slice(0, 2).concat(a.kind === "EARLY_WARNING" ? (d.factors || []).filter((f) => f.feature !== "news2_now").slice(0, 3).map((f) => f.text) : []);
    return `<div class="alert t-${a.severity} st-${a.status}" data-aid="${a.id}">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start"><span class="t">${esc(a.title)}</span><span class="badge t-${a.severity}">${a.severity}</span></div>
      <div class="m"><span>${esc(a.patient_name)}</span><span>${a.kind.replace("_", " ").toLowerCase()}</span><span>${fmtTime(a.created_ts)}</span>${a.improving && a.status !== "RESOLVED" ? `<span style="color:var(--stable)">▼ improving</span>` : ""}</div>
      ${why.length ? `<ul>${why.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      ${d.recommendation && a.status !== "RESOLVED" ? `<div class="rec">→ ${esc(d.recommendation)}</div>` : ""}
      ${a.status !== "RESOLVED" ? `<div class="ladder">${ladder}</div>` : ""}
      <div class="afoot"><span>${status}</span><span style="display:flex;gap:5px">${a.status === "OPEN" ? `<button class="btn sm primary" data-ack="${a.id}">Acknowledge</button>` : ""}${a.status !== "RESOLVED" ? `<button class="btn sm" data-res="${a.id}">Resolve</button>` : ""}</span></div></div>`;
  }).join("");
  $$("[data-ack]").forEach((b) => b.onclick = async () => { await api(`/api/alerts/${b.dataset.ack}/ack`, { by: operator() }); poll1(); });
  $$("[data-res]").forEach((b) => b.onclick = () => resolveModal(+b.dataset.res));
}

function renderActivity() {
  const el = $("#activity");
  if (actTab === "msgs") {
    el.innerHTML = S.messages.length ? S.messages.map((m) => `<div class="msg"><div class="ch">${esc(m.channel)} → ${esc(m.to_name)}</div>${esc(m.text)}<div class="meta">${esc(m.to_phone)} · ${fmtTime(m.ts)} · ${ago(m.real)}</div></div>`).join("") : `<div class="empty">No notifications sent yet.</div>`;
  } else if (actTab === "resp") {
    const r = S.responses;
    el.innerHTML = `<div class="kgrid" style="margin-bottom:10px"><div class="k"><b>${r.median_ack_s != null ? r.median_ack_s + "s" : "-"}</b><span>median time to acknowledge</span></div><div class="k"><b>${r.escalated}/${r.alerts_total}</b><span>alerts that needed escalation</span></div></div>
      ${Object.entries(r.per_responder).map(([k, v]) => `<div class="msg"><b>${esc(k)}</b> · ${v.n} acknowledged · median ${v.median_ack_s}s</div>`).join("") || `<div class="empty">No responses yet.</div>`}
      ${S.patients.map((p) => `<div class="msg"><b>${esc(p.name)}</b>: ${p.on_duty ? "on duty - " + esc(p.on_duty.caregiver) : "nobody checked in"} · next round ${p.round_due ? fmtTime(p.round_due) : "-"}</div>`).join("")}`;
  } else el.innerHTML = S.audit.map((a) => `<div class="audit">${fmtTime(a.ts)} [${esc(a.actor)}] ${esc(a.what)}</div>`).join("");
}

function renderLab() {
  if ($("#lab").classList.contains("hidden")) return;
  $$("[data-chaos]").forEach((i) => i.checked = !!S.chaos[i.dataset.chaos]);
  const m = S.metrics;
  const buffered = S.patients.reduce((s, p) => s + ((p.hub && p.hub.buffered) || 0), 0);
  const k = [[m.received, "readings ingested"], [m.duplicates, "duplicates dropped safely"], [m.late, "late readings reconciled"], [buffered, "queued at the edge now"],
    [m.replayed_readings, "replayed after outages"], [m.edge_local_alarms, "in-room alarms while offline"], [m.artifact_alarms_prevented, "artifact false alarms blocked"],
    [m.unconfirmed_suppressed, "single spikes not escalated"], [m.scale2_prevented, "COPD false alarms avoided"], [`${m.ml_breaker}${m.ml_fallbacks ? " · " + m.ml_fallbacks : ""}`, "AI circuit breaker"]];
  $("#kcounters").innerHTML = k.map(([v, l]) => `<div class="k ${v && v !== "closed" ? "hot" : ""}"><b>${v}</b><span>${l}</span></div>`).join("");
  const mm = S.model && S.model.metrics;
  if (mm && !$("#modelcard").dataset.done) {
    $("#modelcard").dataset.done = 1;
    const lead = [["HomeWard AI", mm.ml_median_lead_h, "var(--accent)"], ["NEWS2 ≥ 3 alone", mm.news2_ge3_median_lead_h, "var(--faint)"]];
    const fa = [["HomeWard AI", mm.ml_false_early_warnings_per_stable_patient_day, "var(--stable)"], ["NEWS2 ≥ 3 alone", mm.news2_ge3_alerts_per_stable_patient_day, "var(--faint)"]];
    const bar = (rows, max, unit) => rows.map(([l, v, c]) => `<div class="row"><span>${l}</span><div class="track"><div class="fill" style="width:${(100 * v / max).toFixed(0)}%;background:${c}"></div></div><b>${v}${unit}</b></div>`).join("");
    const sc = mm.per_scenario;
    $("#modelcard").innerHTML = `<div class="subhead" style="margin-top:0">Median warning before NEWS2 ≥ 5</div><div class="bars">${bar(lead, 12, " h")}</div>
      <div class="subhead">False alerts per stable patient-day</div><div class="bars">${bar(fa, 0.3, "")}</div>
      <table class="mc" style="margin-top:10px"><tr><td>Infection / sepsis-like flagged early</td><td>${sc.sepsis.flagged_early}/${sc.sepsis.n}</td></tr>
      <tr><td>Heart-failure decompensation flagged early</td><td>${sc.hf_decompensation.flagged_early}/${sc.hf_decompensation.n}</td></tr>
      <tr><td>COPD exacerbation flagged early</td><td>${sc.copd_exacerbation.flagged_early}/${sc.copd_exacerbation.n}</td></tr>
      <tr><td>Sudden events (not forecastable - NEWS2 catches them live)</td><td>${sc.acute_hypoxia.flagged_early}/${sc.acute_hypoxia.n}</td></tr></table>`;
  }
}

function notifyNew() {
  for (const a of S.alerts) {
    const prev = seenAlerts.get(a.id);
    if (!firstLoad && a.status !== "RESOLVED" && (!prev || prev !== a.severity)) {
      toast(`${a.patient_name}: ${a.title}`, a.severity);
      if (sound && (a.severity === "HIGH" || a.severity === "CRITICAL")) beep(a.severity === "CRITICAL" ? 3 : 1);
      if (window.Notification && Notification.permission === "granted" && a.severity !== "LOW") new Notification(`HomeWard · ${a.severity}`, { body: `${a.patient_name}: ${a.title}` });
    }
    seenAlerts.set(a.id, a.severity);
  }
  firstLoad = false;
}

/* ------------------------------------------------------------ charts */
function setupCanvas(c) {
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  if (!w) return [null, 0, 0];
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  return [g, w, h];
}
function drawSpark(c, p) {
  const [g, w, h] = setupCanvas(c); if (!g || !p) return;
  const d = p.spark || []; if (d.length < 2) return;
  const thr = p.forecast && p.forecast.threshold < 1 ? p.forecast.threshold : 0.4;
  const X = (i) => i / (d.length - 1) * w, Y = (v) => h - 2 - v * (h - 4);
  g.strokeStyle = css("--line-2"); g.setLineDash([3, 3]); g.beginPath(); g.moveTo(0, Y(thr)); g.lineTo(w, Y(thr)); g.stroke(); g.setLineDash([]);
  const grad = g.createLinearGradient(0, 0, 0, h); grad.addColorStop(0, "rgba(124,58,237,.25)"); grad.addColorStop(1, "rgba(124,58,237,0)");
  g.beginPath(); d.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.lineTo(w, h); g.lineTo(0, h); g.fillStyle = grad; g.fill();
  g.beginPath(); d.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))); g.strokeStyle = css("--risk"); g.lineWidth = 1.6; g.stroke();
}

let geo = null;
function drawCharts() {
  if (!H || !S || tab !== "overview") return;
  const p = patient(); if (!p) return;
  const now = S.sim_time, t0 = now - rangeH * 3600;
  const rows = H.readings.filter((r) => r.ts >= t0);
  const [g, w, h] = setupCanvas($("#chart")); if (!g) return;
  const L = 38, R = 8, T = 22, B = 20, pw = w - L - R, ph = h - T - B;
  const v = vital, meta = VITALS[v];
  let band = meta.band ? [...meta.band] : null;
  if (v === "spo2" && p.spo2_scale === 2) band = [88, 92];
  if (v === "sbp") band = [111, 160];
  const good = rows.filter((r) => !r.artifact && r[v] != null), bad = rows.filter((r) => r.artifact && r[v] != null);
  const vals = good.map((r) => r[v]); if (band) vals.push(...band);
  if (p.baseline && p.baseline[v] != null) vals.push(p.baseline[v]);
  if (!vals.length) return;
  let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  const X = (ts) => L + (ts - t0) / (now - t0) * pw, Y = (y) => T + (1 - (y - lo) / (hi - lo)) * ph;
  geo = { L, pw, t0, now, X, Y, rows: good, h };
  g.font = "11px Inter, system-ui"; g.fillStyle = css("--chart-text"); g.strokeStyle = css("--chart-grid"); g.lineWidth = 1;
  for (let i = 0; i <= 4; i++) { const y = lo + (hi - lo) * i / 4; g.beginPath(); g.moveTo(L, Y(y)); g.lineTo(w - R, Y(y)); g.stroke(); g.fillText(v === "temp" || v === "weight" ? y.toFixed(1) : Math.round(y), 2, Y(y) + 4); }
  for (let i = 0; i <= 6; i++) { const ts = t0 + (now - t0) * i / 6; const lbl = fmtTime(ts); g.fillText(lbl, Math.max(L - 4, Math.min(w - R - g.measureText(lbl).width, X(ts) - g.measureText(lbl).width / 2)), h - 4); }
  if (band) { g.fillStyle = css("--chart-band"); const yt = Y(Math.min(band[1], hi)), yb = Y(Math.max(band[0], lo)); g.fillRect(L, yt, pw, yb - yt); }
  if (p.baseline && p.baseline[v] != null) { g.strokeStyle = css("--chart-base"); g.setLineDash([5, 4]); g.lineWidth = 1.2; g.beginPath(); g.moveTo(L, Y(p.baseline[v])); g.lineTo(w - R, Y(p.baseline[v])); g.stroke(); g.setLineDash([]); }
  // area + line, broken at gaps > 20 min
  const segs = []; let cur = [];
  for (const r of good) { if (cur.length && r.ts - cur[cur.length - 1].ts > 20 * 60) { segs.push(cur); cur = []; } cur.push(r); }
  if (cur.length) segs.push(cur);
  for (const sgm of segs) {
    if (sgm.length < 2) continue;
    const grad = g.createLinearGradient(0, T, 0, T + ph); grad.addColorStop(0, css("--chart-fill")); grad.addColorStop(1, "rgba(0,0,0,0)");
    g.beginPath(); sgm.forEach((r, i) => i ? g.lineTo(X(r.ts), Y(r[v])) : g.moveTo(X(r.ts), Y(r[v]))); g.lineTo(X(sgm[sgm.length - 1].ts), T + ph); g.lineTo(X(sgm[0].ts), T + ph); g.closePath(); g.fillStyle = grad; g.fill();
    g.beginPath(); sgm.forEach((r, i) => i ? g.lineTo(X(r.ts), Y(r[v])) : g.moveTo(X(r.ts), Y(r[v]))); g.strokeStyle = css("--chart-line"); g.lineWidth = 1.7; g.lineJoin = "round"; g.stroke();
  }
  g.strokeStyle = css("--faint"); g.lineWidth = 1.4;
  for (const r of bad) { const x = X(r.ts), y = Y(Math.max(lo, Math.min(hi, r[v]))); g.beginPath(); g.moveTo(x - 3, y - 3); g.lineTo(x + 3, y + 3); g.moveTo(x + 3, y - 3); g.lineTo(x - 3, y + 3); g.stroke(); }
  g.fillStyle = css("--watch"); for (const r of good.filter((r) => r.late)) { g.beginPath(); g.arc(X(r.ts), Y(r[v]), 2.4, 0, 7); g.fill(); }
  g.fillStyle = css("--accent"); for (const r of good.filter((r) => r.source === "sensor")) { g.beginPath(); g.arc(X(r.ts), Y(r[v]), 2, 0, 7); g.fill(); }
  g.fillStyle = css("--ink"); g.font = "600 12px Inter, system-ui"; g.fillText(`${meta.label} (${meta.unit})`, L, 13);

  const [g2, w2, h2] = setupCanvas($("#chart2")); if (!g2) return;
  const ph2 = h2 - 22, Y2 = (f) => 6 + (1 - f) * ph2;
  const trail = (H.trail || []).filter((x) => x.at >= t0);
  g2.font = "11px Inter, system-ui"; g2.fillStyle = css("--chart-text"); g2.strokeStyle = css("--chart-grid");
  [0, .5, 1].forEach((f) => { g2.beginPath(); g2.moveTo(L, Y2(f)); g2.lineTo(w2 - R, Y2(f)); g2.stroke(); });
  g2.fillText("100%", 2, Y2(1) + 4); g2.fillText("0", 2, Y2(0) + 4);
  const thr = p.forecast && p.forecast.threshold < 1 ? p.forecast.threshold : null;
  if (thr) { g2.strokeStyle = css("--risk"); g2.globalAlpha = .45; g2.setLineDash([4, 4]); g2.beginPath(); g2.moveTo(L, Y2(thr)); g2.lineTo(w2 - R, Y2(thr)); g2.stroke(); g2.setLineDash([]); g2.globalAlpha = 1; }
  const line = (key, color, f) => { g2.strokeStyle = color; g2.lineWidth = 1.7; g2.beginPath(); let first = true; for (const x of trail) { if (x[key] == null) continue; const px = X(x.at), py = Y2(f(x[key])); first ? g2.moveTo(px, py) : g2.lineTo(px, py); first = false; } g2.stroke(); };
  line("news2", css("--n2"), (n) => Math.min(1, n / 14));
  line("prob", css("--risk"), (q) => q);
}
// hover tooltip on the main chart
$("#chart").addEventListener("mousemove", (e) => {
  if (!geo || !geo.rows.length) return;
  const rect = e.target.getBoundingClientRect(), x = e.clientX - rect.left;
  const ts = geo.t0 + (x - geo.L) / geo.pw * (geo.now - geo.t0);
  let best = null, bd = 1e18; for (const r of geo.rows) { const d = Math.abs(r.ts - ts); if (d < bd) { bd = d; best = r; } }
  if (!best || bd > 30 * 60) return hideTip();
  const cx = geo.X(best.ts), tip = $("#tip"), cr = $("#cross");
  tip.innerHTML = `<div style="opacity:.7">${fmtTime(best.ts, true)}${best.source === "sensor" ? " · sensor" : ""}${best.late ? " · arrived late" : ""}</div>HR <b>${Math.round(best.hr)}</b> · RR <b>${Math.round(best.rr)}</b> · SpO₂ <b>${Math.round(best.spo2)}%</b><br>BP <b>${Math.round(best.sbp)}/${Math.round(best.dbp)}</b> · T <b>${(+best.temp).toFixed(1)}°</b>`;
  const wrap = $("#chartwrap").getBoundingClientRect();
  const left = rect.left - wrap.left + cx;
  tip.style.left = `${Math.min(wrap.width - 200, Math.max(0, left + 12))}px`; tip.style.top = `${rect.top - wrap.top + 30}px`;
  cr.style.left = `${left}px`; cr.style.height = `${geo.h - 40}px`; cr.style.top = `${rect.top - wrap.top + 20}px`;
  tip.classList.add("on"); cr.classList.add("on");
});
const hideTip = () => { $("#tip").classList.remove("on"); $("#cross").classList.remove("on"); };
$("#chart").addEventListener("mouseleave", hideTip);

/* ------------------------------------------------------------ notes + timeline */
const EXAMPLES = ["Saans phool rahi hai, khana nahi khaya", "She seems confused today", "बुखार है और कमजोरी", "Ankles more swollen, less urine", "No chest pain, feeling better", "Papa gir gaye bathroom mein"];
function renderNotes() {
  if (!H) return;
  const items = [...(H.notes || []).map((n) => ({ ...n, kind: "note" })), ...(H.rounds || []).map((r) => ({ ...r, kind: "round" }))].sort((a, b) => b.ts - a.ts);
  $("#notelist").innerHTML = items.length ? items.map((n) => {
    if (n.kind === "round") return `<div class="n">✅ <b>Round check</b> by ${esc(n.caregiver)} · consciousness ${esc(n.acvpu)}<div class="meta">${fmtTime(n.ts, true)}</div></div>`;
    const a = JSON.parse(n.analysis || "{}");
    const chips = (a.signals || []).filter((s) => s.weight > 0).map((s) => `<span class="chip ${s.red_flag ? "red" : "amber"}">${esc(s.label)}</span>`).join(" ") + (a.negated || []).map((s) => `<span class="chip">no ${esc(s.label.toLowerCase())}</span>`).join(" ");
    return `<div class="n">📝 ${esc(n.text)}<div class="chips">${chips}</div><div class="meta">${esc(n.author)} · ${fmtTime(n.ts, true)}</div></div>`;
  }).join("") : `<div class="empty">No notes yet.</div>`;
}
const EV = { PILLBOX_OPEN: ["💊", "Pill box opened", "--stable"], BED_EXIT: ["🛏", "Left bed", "--watch"], BED_RETURN: ["🛏", "Back in bed", "--stable"], ROOM_STILL: ["🚶", "Room motion sensor: no movement for 10 min", "--critical"], LOCAL_ALARM: ["🔔", "Hub sounded in-room alarm (offline)", "--critical"] };
function renderTimeline() {
  if (!H || tab !== "timeline") return;
  const items = [
    ...(H.audit || []).filter((a) => !/^Resilience lab|^Simulator/.test(a.what)).map((a) => ({ ts: a.ts, ico: a.what.includes("raised") ? "🚨" : a.what.includes("acknowledged") ? "✋" : a.what.includes("resolved") ? "✅" : a.what.includes("escalated") ? "📣" : "•", text: a.what, who: a.actor, c: a.what.includes("raised") || a.what.includes("escalated") ? "--critical" : "--accent" })),
    ...(H.events || []).map((e) => { const d = JSON.parse(e.detail || "{}"); const m = EV[e.kind] || ["•", e.kind, "--faint"]; return { ts: e.ts, ico: m[0], text: m[1] + (d.med ? ` - ${d.med}` : ""), who: "device", c: m[2] }; }),
  ].sort((a, b) => b.ts - a.ts).slice(0, 120);
  $("#timeline").innerHTML = items.length ? items.map((i) => `<div class="tl"><div class="when">${fmtTime(i.ts)}</div><div class="dot" style="--c:var(${i.c})"></div><div class="what">${i.ico} ${esc(i.text)} <small>· ${esc(i.who)}</small></div></div>`).join("") : `<div class="empty">Nothing yet.</div>`;
}

/* ------------------------------------------------------------ modals */
function modal(html) { $("#modalCard").innerHTML = html; $("#modal").classList.add("open"); }
function closeModal() { $("#modal").classList.remove("open"); }
$("#modal").onclick = (e) => { if (e.target.id === "modal") closeModal(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

function resolveModal(id) {
  const a = S.alerts.find((x) => x.id === id);
  modal(`<h3>Resolve alert</h3><div class="note" style="margin-top:0">${esc(a.patient_name)} · ${esc(a.title)}</div>
    <label>What was done?</label><select id="resAct"><option>Checked on patient - condition reviewed</option><option>Gave prescribed medicine</option><option>Called doctor / tele-consult</option><option>Arranged hospital transfer / ambulance</option><option>Sensor re-fitted / technical issue</option><option>False alarm - patient fine</option></select>
    <label>Note (feeds back into the patient's context)</label><textarea id="resNote" placeholder="e.g. Spoke to Dr. Rao, recheck in 1 h"></textarea>
    <label>Your name</label><input id="resBy" value="${esc(operator())}">
    <div class="row"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn primary" id="resGo">Resolve</button></div>`);
  $("#resGo").onclick = async () => { store.set("hw.operator", $("#resBy").value); await api(`/api/alerts/${id}/resolve`, { by: $("#resBy").value || operator(), action: $("#resAct").value, note: $("#resNote").value }); closeModal(); poll1(); };
}
async function openSBAR() {
  const txt = await api(`/api/patients/${sel}/sbar`), p = patient();
  modal(`<h3>📞 SBAR handover</h3><div class="note" style="margin:0 0 10px">The structured format clinicians use on the phone, generated from live data.</div><pre>${esc(txt)}</pre>
    <div class="row"><button class="btn" onclick="closeModal()">Close</button><button class="btn" id="copySbar">Copy</button><a class="btn primary" href="tel:${esc(p.contacts[2].phone)}">Call care desk</a></div>`);
  $("#copySbar").onclick = () => navigator.clipboard && navigator.clipboard.writeText(txt).then(() => toast("SBAR copied"));
}
$("#sbarBtn").onclick = openSBAR;
$("#roundBtn").onclick = () => {
  const p = patient();
  modal(`<h3>✅ Round check · ${esc(p.name)}</h3>
    <label>How does the patient respond? (ACVPU)</label><select id="rAcvpu"><option value="A">Awake, alert, talking normally</option><option value="C">New confusion / not making sense</option><option value="V">Only responds to voice</option><option value="P">Only responds to pain</option><option value="U">Unresponsive</option></select>
    <label>Anything else?</label><textarea id="rNote" placeholder="Medicines given, eating, mood…"></textarea>
    <label>Your name</label><input id="rBy" value="${esc(p.contacts[0].name)}">
    <div class="row"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn primary" id="rGo">Save round</button></div>`);
  $("#rGo").onclick = async () => { await api(`/api/patients/${sel}/rounds`, { caregiver: $("#rBy").value, acvpu: $("#rAcvpu").value, note: $("#rNote").value }); closeModal(); poll1(); toast("Round check saved"); };
};
$("#dutyBtn").onclick = async () => { const p = patient(); await api(`/api/patients/${sel}/checkin`, { caregiver: p.contacts[0].name, on: !p.on_duty }); poll1(); };
async function addNote(text, author) {
  const a = await api(`/api/patients/${sel}/notes`, { author: author || $("#noteAuthor").value.trim() || patient().contacts[0].name, text });
  $("#understood").innerHTML = `<b>Understood:</b> ${(a.signals || []).filter((s) => s.weight > 0).map((s) => `<span class="chip ${s.red_flag ? "red" : "amber"}">${esc(s.label)}</span>`).join(" ") || "no concerning signs"}${(a.negated || []).length ? ` · <b>ruled out:</b> ${a.negated.map((n) => esc(n.label)).join(", ")}` : ""}${a.suggests_new_confusion ? ` · <b>counts as new confusion in NEWS2</b>` : ""}`;
  loadHistory(); poll1(); return a;
}
$("#noteBtn").onclick = async () => { const t = $("#noteText").value.trim(); if (!t) return; await addNote(t); $("#noteText").value = ""; };
$("#examples").innerHTML = EXAMPLES.map((e) => `<button>${esc(e)}</button>`).join("");
$$("#examples button").forEach((b) => b.onclick = () => $("#noteText").value = b.textContent);

/* ------------------------------------------------------------ controls */
$$("#speedSeg button[data-speed]").forEach((b) => b.onclick = () => api("/api/sim", { speed: +b.dataset.speed, paused: false }).then(poll1));
$("#pause").onclick = () => api("/api/sim", { paused: !S.paused }).then(poll1);
function toggleLab(show) { const el = $("#lab"); show = show ?? el.classList.contains("hidden"); el.classList.toggle("hidden", !show); store.set("hw.lab", show ? "1" : "0"); if (show) { $("#modelcard").dataset.done = ""; renderLab(); } }
$("#labBtn").onclick = () => toggleLab(); $("#labClose").onclick = () => toggleLab(false);
if (store.get("hw.lab", "0") === "1") $("#lab").classList.remove("hidden");
$$("[data-chaos]").forEach((i) => i.onchange = () => api("/api/chaos", { flag: i.dataset.chaos, value: i.checked }).then(poll1));
$("#deadToggle").onchange = (e) => api("/api/chaos", { flag: "device_dead", value: e.target.checked, patient_id: sel }).then(poll1);
$("#scnBtn").onclick = () => api("/api/sim", { patient_id: sel, scenario: $("#scnSel").value }).then(() => toast(`Deterioration started for ${patient().name}`));
$("#artBtn").onclick = () => api("/api/sim", { patient_id: sel, artifact: true }).then(() => toast("Finger sensor displaced for 20 min"));
$("#fallBtn").onclick = () => api("/api/sim", { patient_id: sel, fall: true }).then(() => toast("Patient gets up, falls, and lies still"));
$("#o2Btn").onclick = () => api("/api/sim", { patient_id: sel, o2_fault: true }).then(() => toast("Oxygen concentrator output degraded (only for patients on home oxygen)"));
$("#doseBtn").onclick = () => api("/api/sim", { patient_id: sel, skip_dose: true }).then(() => toast("Next dose will be missed"));
$("#resetBtn").onclick = () => api("/api/sim", { patient_id: sel, reset: true, back_to_bed: true }).then(() => toast("Patient recovering"));
$$("#rangeSeg button").forEach((b) => b.onclick = () => { rangeH = +b.dataset.h; $$("#rangeSeg button").forEach((x) => x.classList.toggle("on", x === b)); drawCharts(); });
$$("#alertSeg button").forEach((b) => b.onclick = () => { alertFilter = b.dataset.f; $$("#alertSeg button").forEach((x) => x.classList.toggle("on", x === b)); renderAlerts(); });
$$("#actSeg button").forEach((b) => b.onclick = () => { actTab = b.dataset.t; $$("#actSeg button").forEach((x) => x.classList.toggle("on", x === b)); renderActivity(); });
$$("#ptabs button").forEach((b) => b.onclick = () => { setTab(b.dataset.tab); renderTimeline(); });
$("#soundBtn").onclick = () => { sound = !sound; $("#soundBtn").textContent = sound ? "🔊" : "🔈"; if (sound) { beep(1); if (window.Notification && Notification.permission === "default") Notification.requestPermission(); } };
const applyTheme = (t) => { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; setTimeout(drawCharts, 30); };
applyTheme(store.get("hw.theme", ""));
$("#themeBtn").onclick = () => { const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches); const t = dark ? "light" : "dark"; store.set("hw.theme", t); applyTheme(t); render(); };
window.addEventListener("resize", () => { drawCharts(); });

let actx;
function beep(n) { try { actx = actx || new AudioContext(); for (let i = 0; i < n; i++) { const o = actx.createOscillator(), g = actx.createGain(); o.frequency.value = 880; o.connect(g); g.connect(actx.destination); const t = actx.currentTime + i * 0.3; g.gain.setValueAtTime(0.2, t); g.gain.exponentialRampToValueAtTime(0.001, t + 0.25); o.start(t); o.stop(t + 0.26); } } catch {} }
function toast(msg, sev) {
  const d = document.createElement("div"); d.className = `toast ${sev ? "t-" + sev : ""}`; d.textContent = msg; $("#toasts").appendChild(d);
  setTimeout(() => { d.classList.add("out"); setTimeout(() => d.remove(), 200); }, 5000);
  while ($("#toasts").children.length > 4) $("#toasts").firstChild.remove();
}

// small API for the guided tour
window.HW = { api, poll1, select, setTab, toggleLab, openSBAR, closeModal, addNote, toast, loadHistory, state: () => S, patient };
startStream(); historyLoop();
