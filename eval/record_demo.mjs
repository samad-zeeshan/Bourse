// Drive headless Chrome over the DevTools protocol to screenshot or record the demo page.
//
// node eval/record_demo.mjs shots <url> <outdir>     one screenshot per view, plus a phone-width one
// node eval/record_demo.mjs frames <url> <outdir> <script.json>   frames for the GIF and the recording

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const [mode, url, outdir, scriptPath] = process.argv.slice(2);
fs.mkdirSync(outdir, { recursive: true });

const port = 9300 + Math.floor(Math.random() * 500);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "bourse-chrome-"));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--hide-scrollbars", "--force-device-scale-factor=1", "--window-size=1280,860", "about:blank"], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function target() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch {}
    await sleep(200);
  }
  throw new Error("Chrome did not start");
}

const ws = new WebSocket(await target());
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let id = 0;
const pending = new Map();
ws.addEventListener("message", (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
});
const send = (method, params = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
const evaluate = async (expr) => (await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result?.result?.value;
const shot = async (file, full = false) => {
  const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: full });
  fs.writeFileSync(file, Buffer.from(r.result.data, "base64"));
};
const size = (w, h) => send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: w < 600 });

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: process.env.REDUCED ? "reduce" : "no-preference" }] });
const errors = [];
ws.addEventListener("message", (m) => {
  const d = JSON.parse(m.data);
  if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.text + " " + (d.params.exceptionDetails.exception?.description || ""));
  if (d.method === "Runtime.consoleAPICalled" && d.params.type === "error") errors.push(d.params.args.map((a) => a.value || a.description).join(" "));
});

async function open(w, h) {
  await size(w, h);
  const t0 = Date.now();
  await send("Page.navigate", { url });
  for (let i = 0; i < 100 && !(await evaluate("document.readyState === 'complete' && !!document.querySelector('#facts dt')")); i++) await sleep(100);
  return Date.now() - t0;
}
const click = (view) => evaluate(`document.querySelector('[data-view=${view}]').click()`);

if (mode === "shots") {
  const loadMs = await open(1280, 860);
  await sleep(4000);
  await shot(path.join(outdir, "replay.png"));
  await sleep(16000);
  await shot(path.join(outdir, "replay-end.png"));
  for (const v of ["latency", "impact", "cheat"]) { await click(v); await sleep(2500); await shot(path.join(outdir, `${v}.png`)); }
  await shot(path.join(outdir, "full.png"), true);
  await open(390, 844);
  await sleep(3000);
  const overflow = await evaluate("document.documentElement.scrollWidth > window.innerWidth");
  await shot(path.join(outdir, "phone.png"), true);
  console.log(JSON.stringify({ loadMs, phoneOverflow: overflow, errors }));
} else if (mode === "frames") {
  // The script is a list of [view, seconds]. Frames are captured at a fixed rate while each view plays.
  const plan = JSON.parse(fs.readFileSync(scriptPath, "utf8"));
  const fps = plan.fps || 10;
  await open(plan.width || 1280, plan.height || 860);
  let n = 0;
  for (const [view, secs] of plan.steps) {
    // Headless Chrome ignores smooth scrolling, so the page is scrolled by hand with an ease-out curve.
    if (view.startsWith("scroll:")) await evaluate(`(() => { const a = scrollY, b = ${view.slice(7)}, t0 = performance.now();
      const f = (t) => { const k = Math.min(1, (t - t0) / 900); scrollTo(0, a + (b - a) * (1 - (1 - k) ** 3)); if (k < 1) requestAnimationFrame(f); };
      requestAnimationFrame(f); })()`);
    else if (view !== "wait") await click(view);
    const end = Date.now() + secs * 1000;
    while (Date.now() < end) {
      const t = Date.now();
      await shot(path.join(outdir, `f${String(n++).padStart(5, "0")}.png`));
      await sleep(Math.max(0, 1000 / fps - (Date.now() - t)));
    }
  }
  console.log(JSON.stringify({ frames: n, seconds: plan.steps.reduce((a, s) => a + s[1], 0), errors }));
}
ws.close();
chrome.kill();
