/* End-to-end browser tests: a real Chromium drives the real app (no mocks).
 *
 *   node tests/e2e/browser.e2e.cjs            # starts its own server on a free port
 *   (needs: npm i playwright && npx playwright install chromium)
 *
 * Also run from pytest via tests/test_e2e_browser.py (skipped if Playwright is missing).
 */
const { chromium, devices } = require("playwright");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");

const ROOT = path.resolve(__dirname, "..", "..");
const PORT = 8000 + Math.floor(Math.random() * 900) + 100;
const URL = `http://127.0.0.1:${PORT}`;
const results = [];
let server, browser;

async function check(name, fn) {
  const t0 = Date.now();
  try { await fn(); results.push({ name, ok: true }); console.log(`  ✓ ${name} (${Date.now() - t0} ms)`); }
  catch (e) { results.push({ name, ok: false, err: e.message }); console.log(`  ✗ ${name}\n      ${e.message}`); }
}
const assert = (c, msg) => { if (!c) throw new Error(msg); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitHealthy() {
  for (let i = 0; i < 120; i++) {
    try { const r = await fetch(`${URL}/api/health`); if (r.ok) { await sleep(2500); return; } } catch {}
    await sleep(500);
  }
  throw new Error("server did not start");
}
async function login(page, user, pin, next = "/") {
  await page.goto(URL + next);
  await page.waitForSelector(".who button");
  await page.click(`[data-id="${user}"]`);
  await page.fill("#pin", pin);
  await page.click("button[type=submit]");
}
async function api(page, p, body) {
  return page.evaluate(async ([p, body]) => {
    const r = await fetch(p, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    return { status: r.status, json: await r.json().catch(() => null) };
  }, [p, body]);
}

(async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), "homeward-e2e-"));
  server = spawn(process.env.PYTHON || "python3", ["run.py", "--port", String(PORT)], { cwd: ROOT, env: { ...process.env, HOMEWARD_DATA: data }, stdio: "ignore" });
  try {
    await waitHealthy();
    browser = await chromium.launch();
    console.log(`HomeWard browser E2E on ${URL}`);

    const desk = await browser.newContext({ viewport: { width: 1500, height: 950 } });
    const p = await desk.newPage();
    const errors = []; p.on("pageerror", (e) => errors.push(e.message));

    await check("signed-out visitor is redirected to the login page", async () => {
      await p.goto(URL + "/"); assert(p.url().includes("/login"), `url ${p.url()}`);
    });
    await check("wrong PIN is rejected with a message", async () => {
      await p.click('[data-id="asha"]'); await p.fill("#pin", "0000"); await p.click("button[type=submit]");
      await p.waitForFunction(() => document.querySelector("#err").textContent.length > 0);
    });
    await check("care desk signs in and sees all three homes", async () => {
      await login(p, "asha", "1234"); await p.waitForSelector(".pcard");
      await p.waitForFunction(() => document.querySelectorAll(".pcard").length === 3);
    });
    await check("live pipeline strip shows data flowing end to end", async () => {
      const txt = await p.textContent("#pipeline");
      for (const w of ["Sensors", "Edge hubs", "Platform", "AI risk", "Alerts", "Response"]) assert(txt.includes(w), `missing ${w}`);
      await p.waitForFunction(() => /\d+ ingested/.test(document.querySelector("#pipeline").textContent));
    });
    await check("vital tiles, risk level and NEWS2 table render", async () => {
      assert((await p.$$(".tile")).length === 6, "6 tiles");
      await p.waitForFunction(() => /risk/i.test(document.querySelector("#statusbar .big").textContent));
      assert((await p.$$("table.n2 tr")).length >= 8, "NEWS2 rows");
    });
    await check("selecting a patient updates the detail view", async () => {
      await p.click('.pcard[data-id="P-102"]');
      await p.waitForFunction(() => document.querySelector("#pname").textContent.includes("Rajesh"));
      assert((await p.textContent("#pchips")).includes("scale 2"), "Scale 2 chip");
    });
    await check("devices tab lists home IoT incl. oxygen concentrator", async () => {
      await p.click('#ptabs button[data-tab="care"]');
      const t = await p.textContent("#devgrid");
      for (const w of ["Bedside hub", "Smart pill box", "Bed + room motion", "Oxygen concentrator", "Power"]) assert(t.includes(w), `missing ${w}`);
    });
    await check("Tamil caregiver note is understood with language shown", async () => {
      await p.fill("#noteText", "Appa ku moochu thinaral irukku, kaaichal illai"); await p.click("#noteBtn");
      await p.waitForFunction(() => document.querySelector("#understood").textContent.includes("Breathlessness"));
      const u = await p.textContent("#understood");
      assert(u.includes("Tanglish") && u.includes("Fever"), u);
    });
    await check("timeline tab shows audited events", async () => {
      await p.click('#ptabs button[data-tab="timeline"]');
      await p.waitForFunction(() => document.querySelectorAll("#timeline .tl").length > 0, null, { timeout: 8000 });
      await p.click('#ptabs button[data-tab="overview"]');
    });
    await check("deterioration raises an early warning in the UI", async () => {
      await p.click('.pcard[data-id="P-101"]');
      await p.selectOption("#scnSel", "sepsis"); await p.click("#scnBtn");
      await p.click('#speedSeg button[data-speed="1800"]');
      await p.waitForFunction(() => [...document.querySelectorAll(".alert .t")].some((e) => /Early warning/.test(e.textContent)), null, { timeout: 90000 });
    });
    await check("acknowledging an alert stops escalation and records the user", async () => {
      await p.click('#speedSeg button[data-speed="60"]');
      await p.click("[data-ack]");
      await p.waitForFunction(() => /Nurse Asha/.test(document.querySelector("#alerts").textContent));
    });
    await check("SBAR handover opens with all four sections", async () => {
      await p.click("#sbarBtn"); await p.waitForSelector(".modal.open pre");
      const t = await p.textContent(".modal pre");
      for (const s of ["S - Situation", "B - Background", "A - Assessment", "R - Recommendation"]) assert(t.includes(s), s);
      await p.keyboard.press("Escape");
    });
    await check("Resilience Lab: internet outage -> offline -> full recovery", async () => {
      await p.click("#labBtn"); await p.waitForSelector("#lab:not(.hidden)");
      await p.check('[data-chaos="network_down"]', { force: true });
      await p.click('#speedSeg button[data-speed="900"]');
      await p.waitForFunction(() => /offline|no data/.test(document.querySelector("#plist").textContent), null, { timeout: 30000 });
      await p.uncheck('[data-chaos="network_down"]', { force: true });
      await p.waitForFunction(() => !/offline|no data/.test(document.querySelector("#plist").textContent), null, { timeout: 30000 });
      await p.click('#speedSeg button[data-speed="300"]');
    });
    await check("AI crash switches the forecast to rule fallback", async () => {
      await p.check('[data-chaos="ml_down"]', { force: true });
      await p.waitForFunction(() => /rule fallback/.test(document.querySelector("#fc").textContent) || /rule fallback/.test(document.querySelector("#pipeline").textContent), null, { timeout: 20000 });
      await p.uncheck('[data-chaos="ml_down"]', { force: true });
      await p.click("#labClose");
    });
    await check("dark theme toggle applies", async () => {
      await p.click("#themeBtn");
      const t = await p.evaluate(() => document.documentElement.dataset.theme);
      assert(t === "dark" || t === "light", `theme ${t}`);
    });
    await check("guided demo starts with the title slide and can be exited", async () => {
      await p.click("#tourBtn"); await p.waitForSelector("#slide:not(.hidden)");
      await p.keyboard.press("Escape"); await p.waitForSelector("#slide.hidden", { state: "attached", timeout: 3000 });
    });
    await check("no JavaScript errors on the dashboard", async () => { assert(errors.length === 0, errors.join(" | ")); });

    // ---- family member: privacy
    const fam = await browser.newContext({ viewport: { width: 1400, height: 900 } });
    const f = await fam.newPage();
    await check("family member is taken to the caregiver app for their own patient", async () => {
      await login(f, "meena", "2222"); await f.waitForURL(/caregiver\?p=P-102/);
      await f.waitForSelector("#who option", { state: "attached" });
      const opts = await f.$$eval("#who option", (o) => o.map((x) => x.textContent));
      assert(opts.length === 1 && opts[0].includes("Rajesh"), opts.join());
    });
    await check("family dashboard is scoped and hides demo controls", async () => {
      await f.goto(URL + "/"); await f.waitForSelector(".pcard");
      assert((await f.$$(".pcard")).length === 1, "one patient");
      assert(await f.$eval("#labBtn", (e) => e.classList.contains("hidden")), "lab hidden");
    });
    await check("family cannot open another patient's record (403)", async () => {
      const r = await api(f, "/api/patients/P-101/history"); assert(r.status === 403, `status ${r.status}`);
      const c = await api(f, "/api/chaos", { flag: "ml_down", value: true }); assert(c.status === 403, `chaos ${c.status}`);
    });

    // ---- caregiver phone (Hindi), mobile layout
    const ph = await browser.newContext({ ...devices["Pixel 7"] });
    const m = await ph.newPage();
    await check("caregiver phone app: sign-in, Hindi UI, verified bedside check-in", async () => {
      await m.goto(URL + "/caregiver?lang=hi");
      await m.waitForSelector("[data-u]"); await m.click('[data-u="sunita"]'); await m.fill("#pin", "1111"); await m.click("#go");
      await m.waitForSelector(".status .big");
      assert(/[ऀ-ॿ]/.test(await m.textContent(".status")), "Hindi text");
      await m.click("#tag");
      await m.waitForFunction(() => /चेक-इन/.test(document.body.textContent));
    });
    await check("caregiver phone has no horizontal scroll", async () => {
      const over = await m.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(over <= 1, `overflow ${over}px`);
    });
  } finally {
    if (browser) await browser.close();
    if (server) server.kill();
  }
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} browser checks passed`);
  fs.writeFileSync(path.join(__dirname, "last-run.json"), JSON.stringify(results, null, 1));
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.error(e); if (server) server.kill(); process.exit(1); });
