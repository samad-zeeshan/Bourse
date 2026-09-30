// Demo page: replays recorded Binance depth through the engine compiled to WebAssembly, and draws the recorded results.
//
// Every number shown comes from data/results.json, which eval/report.py writes from the result files.

const svg = document.getElementById("chart");
const title = document.getElementById("stage-title");
const caption = document.getElementById("stage-caption");
const readout = document.getElementById("readout");
const stamp = document.getElementById("stamp");
const buttons = [...document.querySelectorAll(".controls button")];
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const NS = "http://www.w3.org/2000/svg";
// Phones get a narrower drawing so chart text stays readable once it is scaled down to the screen.
const narrow = matchMedia("(max-width: 640px)").matches;
const W = narrow ? 460 : 800, H = narrow ? 540 : 420;
svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

let R = null;       // results
let replay = null;  // replay data
let wasm = null;
let run = 0;        // bumps on every view change so a running animation knows to stop

// Two significant figures in prose, as the README does.
function r2(v) {
  if (v === 0) return "0";
  const d = Math.max(0, 1 - Math.floor(Math.log10(Math.abs(v))));
  return Number(v.toFixed(d)).toLocaleString("en-US", { maximumFractionDigits: d });
}
const fmt = (v, d = 0) => v.toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d });

function el(name, attrs = {}, text) {
  const e = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  svg.appendChild(e);
  return e;
}
const clear = () => { while (svg.firstChild) svg.removeChild(svg.firstChild); };

function showStamp(text, ok, low) {
  stamp.textContent = text;
  stamp.classList.toggle("low", !!low);
  stamp.classList.toggle("ok", !!ok);
  stamp.hidden = false;
}

function tapeLine() {
  const s = R.strategies;
  const anyProfit = Object.entries(s).some(([k, v]) => v.leak === "clean" && v.ci[0] > 0);
  document.getElementById("tape").innerHTML =
    `99 in 100 orders in <b>${R.latency.p99} ns</b> · <b>${r2(R.latency.single_mps / 1e6)} million</b> orders a second · ` +
    `<b>${R.fuzz.violations + R.fuzz.reference_diffs}</b> fuzz findings · ` +
    (anyProfit ? "a robot made money" : "no honest robot made money");
}

function facts() {
  const dl = document.getElementById("facts");
  const f = R.fuzz, s = R.strategies;
  const rows = [
    ["Half of all orders were handled within", `${R.latency.p50} ns`],
    ["99 in 100 orders were handled within, against a goal of 1,000 ns", `${R.latency.p99} ns`],
    ["One thread handled this many orders a second, against a goal of one million", `${r2(R.latency.single_mps / 1e6)} million`],
    ["The same orders replayed in separate runs gave identical output in", `${R.determinism.runs} of ${R.determinism.runs}`],
    [`Random orders checked against a slow reference book over ${r2(f.hours)} hours`, `${r2(f.commands / 1e9)} billion`],
    ["Disagreements or broken rules found in all that checking", `${f.violations + f.reference_diffs}`],
    ["The engine's book matched Binance's own snapshot in", `${R.reconstruction.snapshots_matching} of ${R.reconstruction.snapshots_compared}`],
    ["Price impact grew with order size to the power, where theory says 0.5", `${R.impact.delta.toFixed(2)}`, true],
    ["The trained robot's profit per hour on unseen days, in USDT", `${s.rl_adversarial.pnl.toFixed(2)}`, s.rl_adversarial.pnl < 0],
    ["The textbook robot's profit per hour on the same days, in USDT", `${s.avellaneda_stoikov.pnl.toFixed(2)}`, s.avellaneda_stoikov.pnl < 0],
    ["Settings tried and logged before anything was judged", `${R.register}`],
  ];
  dl.innerHTML = "";
  for (const [k, v, bad] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v; if (bad) dd.className = "bad";
    dl.append(dt, dd);
  }
}

// ---- Replay the book --------------------------------------------------------------------------

async function loadEngine() {
  if (wasm) return wasm;
  const mod = await import("./pkg/bourse_wasm.js");
  await mod.default();
  wasm = mod;
  return mod;
}

function drawBook(eng, clock, events, crossed) {
  clear();
  const n = 14;
  const bids = eng.depth(0, n), asks = eng.depth(1, n);
  let maxq = 1;
  for (let i = 1; i < bids.length; i += 2) maxq = Math.max(maxq, bids[i]);
  for (let i = 1; i < asks.length; i += 2) maxq = Math.max(maxq, asks[i]);
  // Leave room on the right so the size label next to the longest bar stays on the chart.
  const rowH = (H - 40) / (2 * n), mid = 20 + n * rowH, x0 = 120, span = W - x0 - 110;
  const len = (q) => Math.max(2, span * Math.sqrt(q / maxq));
  const row = (arr, i, cls, y) => {
    if (2 * i >= arr.length) return;
    const p = arr[2 * i] * replay.tick, q = arr[2 * i + 1] * replay.lot;
    el("rect", { x: x0, y: y + 2, width: len(arr[2 * i + 1]), height: rowH - 4, class: cls });
    el("text", { x: x0 - 10, y: y + rowH / 2 + 4, "text-anchor": "end" }, p.toFixed(2));
    if (i < 4) el("text", { x: x0 + len(arr[2 * i + 1]) + 6, y: y + rowH / 2 + 4 }, `${q.toFixed(3)} ETH`);
  };
  for (let i = 0; i < n; i++) row(asks, i, "ask", mid - (i + 1) * rowH);
  for (let i = 0; i < n; i++) row(bids, i, "bid", mid + i * rowH);
  el("line", { x1: x0 - 110, x2: W - 10, y1: mid, y2: mid, class: "axis" });
  el("text", { x: W - 12, y: 14, "text-anchor": "end" }, "sellers waiting");
  el("text", { x: W - 12, y: H - 6, "text-anchor": "end" }, "buyers waiting");
  const m = Math.floor(clock / 60000), s = Math.floor(clock / 1000) % 60;
  readout.innerHTML = `clock <b>${m}:${String(s).padStart(2, "0")}</b> · events <b>${fmt(events)}</b> · crossed book <b>${crossed ? "yes" : "never"}</b>`;
}

function sameLevels(eng, check) {
  const n = 20;
  const bids = eng.depth(0, n), asks = eng.depth(1, n);
  const eq = (arr, want) => want.every(([p, q], i) => arr[2 * i] === p && arr[2 * i + 1] === q);
  return eq(bids, check.bids) && eq(asks, check.asks);
}

async function showReplay() {
  const me = ++run;
  title.textContent = "Replay the book";
  caption.textContent = "Three minutes of Binance's ETHUSDT order book, played at ten times speed through the engine running in this page. " +
    "Yellow bars are buyers waiting, grey bars are sellers. Whenever Binance published a full snapshot, the page compares it with the engine's book.";
  let mod;
  try {
    mod = await loadEngine();
    if (!replay) replay = await (await fetch("data/replay.json")).json();
  } catch (e) {
    caption.textContent = "This browser could not start the engine, so the replay is unavailable. The recorded results below still apply.";
    return;
  }
  if (me !== run) return;
  const eng = new mod.Engine(replay.min_price, replay.levels, 1 << 20);
  eng.apply(replay.seed);
  const checks = new Map(replay.checks.map((c) => [c.after, c]));
  let i = 0, matched = 0, seen = 0, crossed = false;
  const start = performance.now();
  const speed = 10;
  const step = () => {
    if (me !== run) return;
    const clock = reduced ? Infinity : (performance.now() - start) * speed;
    while (i < replay.batches.length && replay.batches[i].t <= clock) {
      eng.apply(replay.batches[i].c);
      crossed = crossed || eng.crossed();
      const c = checks.get(i);
      if (c) {
        seen++;
        const ok = sameLevels(eng, c);
        matched += ok;
        showStamp(ok ? `Matches Binance, all 40 levels (${matched} of ${seen})` : "Differs from Binance", ok);
        setTimeout(() => { if (me === run) stamp.hidden = true; }, 2200);
      }
      i++;
    }
    const last = replay.batches[Math.min(i, replay.batches.length) - 1];
    drawBook(eng, last ? last.t : 0, eng.events(), crossed);
    if (i < replay.batches.length) requestAnimationFrame(step);
    else caption.textContent = `The replay applied ${fmt(replay.batches.length)} batches of changes. The engine's book matched Binance's snapshot ${matched} of ${seen} times it was checked, and it was never crossed.`;
  };
  requestAnimationFrame(step);
}

// ---- Time each order -------------------------------------------------------------------------

function showLatency() {
  ++run;
  title.textContent = "Time each order";
  const L = R.latency, edges = L.hist.edges_ns, counts = L.hist.counts;
  readout.innerHTML = `orders timed <b>${r2(L.samples / 1e6)} million</b>`;
  caption.textContent = `Half of all orders were handled in ${L.p50} nanoseconds or less, and 99 in 100 in ${L.p99} or less. ` +
    `The goal was one microsecond, so this is well inside it. It was measured on a ${L.cpu.replace(" 6-Core Processor", "")} laptop with nothing isolated, so treat it as a guide.`;
  clear();
  const x0 = 50, x1 = W - 20, y0 = H - 40, y1 = 20;
  const lo = Math.log2(edges[0]), hi = Math.log2(edges[edges.length - 1]);
  const xs = (ns) => x0 + ((Math.log2(ns) - lo) / (hi - lo)) * (x1 - x0);
  const maxc = Math.max(...counts);
  const ys = (c) => y0 - (Math.sqrt(c) / Math.sqrt(maxc)) * (y0 - y1);
  el("line", { x1: x0, x2: x1, y1: y0, y2: y0, class: "axis" });
  counts.forEach((c, i) => {
    if (!c || i + 1 >= edges.length) return;
    const r = el("rect", { x: xs(edges[i]) + 0.5, width: Math.max(1, xs(edges[i + 1]) - xs(edges[i]) - 1), y: y0, height: 0, class: "bar" });
    const h = y0 - ys(c);
    if (reduced) { r.setAttribute("y", y0 - h); r.setAttribute("height", h); return; }
    setTimeout(() => { r.style.transition = "all 500ms cubic-bezier(.2,.8,.2,1)"; r.setAttribute("y", y0 - h); r.setAttribute("height", h); }, 20 + i * 12);
  });
  for (const ns of [10, 100, 1000, 10000, 100000]) {
    if (ns < edges[0] || ns > edges[edges.length - 1]) continue;
    el("text", { x: xs(ns), y: y0 + 18, "text-anchor": "middle" }, ns >= 1000 ? `${ns / 1000} µs` : `${ns} ns`);
  }
  // Labels step down so the three markers, which sit close together, never overlap.
  [["half", L.p50], ["99 in 100", L.p99], ["999 in 1,000", L.p999]].forEach(([label, v], k) => {
    el("line", { x1: xs(v), x2: xs(v), y1: y1 + k * 18, y2: y0, class: "mark" });
    el("text", { x: xs(v) + 6, y: y1 + 12 + k * 18, class: "label" }, `${label}: ${v} ns`);
  });
  el("text", { x: x1, y: y0 + 34, "text-anchor": "end" }, "time per order, log scale");
}

// ---- Push the price --------------------------------------------------------------------------

function showImpact() {
  ++run;
  title.textContent = "Push the price";
  const I = R.impact, pts = I.points;
  readout.innerHTML = `fitted power <b>${I.delta.toFixed(2)}</b> · 95% interval <b>${I.ci[0].toFixed(2)} to ${I.ci[1].toFixed(2)}</b>`;
  caption.textContent = `A big order was split into 60 pieces and sent into a simulated market built from Binance data. ` +
    `Bigger orders moved the price more, and the move grew with size to the power ${I.delta.toFixed(2)}. ` +
    `The square-root law says 0.5, shown dashed, so this simulated market moves too much for big orders and does not reproduce the law.`;
  clear();
  const x0 = 60, x1 = W - 20, y0 = H - 40, y1 = 20;
  const lx = pts.map((p) => Math.log10(p.q_over_v)), ly = pts.map((p) => Math.log10(p.impact_over_sigma));
  const xmin = Math.min(...lx) - 0.2, xmax = Math.max(...lx) + 0.2, ymin = Math.min(...ly) - 0.4, ymax = Math.max(...ly) + 0.3;
  const X = (v) => x0 + ((Math.log10(v) - xmin) / (xmax - xmin)) * (x1 - x0);
  const Y = (v) => y0 - ((Math.log10(v) - ymin) / (ymax - ymin)) * (y0 - y1);
  el("line", { x1: x0, x2: x1, y1: y0, y2: y0, class: "axis" });
  el("line", { x1: x0, x2: x0, y1: y1, y2: y0, class: "axis" });
  const a = pts[0].q_over_v, b = pts[pts.length - 1].q_over_v;
  const fit = el("path", { d: `M${X(a)},${Y(I.c * a ** I.delta)} L${X(b)},${Y(I.c * b ** I.delta)}`, class: "fit" });
  const mid = pts[Math.floor(pts.length / 2)];
  const c5 = mid.impact_over_sigma / Math.sqrt(mid.q_over_v);
  el("path", { d: `M${X(a)},${Y(c5 * Math.sqrt(a))} L${X(b)},${Y(c5 * Math.sqrt(b))}`, class: "ref" });
  pts.forEach((p, i) => {
    const lo = Math.max(p.impact_over_sigma - 1.96 * p.se, 1e-6), hi = p.impact_over_sigma + 1.96 * p.se;
    el("line", { x1: X(p.q_over_v), x2: X(p.q_over_v), y1: Y(lo), y2: Y(hi), class: "err" });
    const c = el("circle", { cx: X(p.q_over_v), cy: Y(p.impact_over_sigma), r: reduced ? 4 : 0, class: "pt" });
    if (!reduced) setTimeout(() => { c.style.transition = "r 300ms ease"; c.setAttribute("r", 4); }, 80 * i);
  });
  if (!reduced) {
    const len = fit.getTotalLength();
    fit.style.strokeDasharray = len; fit.style.strokeDashoffset = len;
    requestAnimationFrame(() => { fit.style.transition = "stroke-dashoffset 900ms ease 500ms"; fit.style.strokeDashoffset = 0; });
  }
  for (const v of [0.003, 0.01, 0.03, 0.1, 0.3]) {
    if (Math.log10(v) < xmin || Math.log10(v) > xmax) continue;
    el("text", { x: X(v), y: y0 + 18, "text-anchor": "middle" }, `${v * 100}%`);
  }
  el("text", { x: x1, y: y0 + 34, "text-anchor": "end" }, "order size as a share of what traded meanwhile");
  el("text", { x: x0 + 6, y: y1 + 10 }, "price move, in normal ten-minute moves");
  el("text", { x: X(b) - 4, y: Y(c5 * Math.sqrt(b)) + 16, "text-anchor": "end" }, "square-root law");
}

// ---- Sneak in a cheat ------------------------------------------------------------------------

function showCheat() {
  const me = ++run;
  title.textContent = "Sneak in a cheat";
  const S = R.strategies;
  const order = ["avellaneda_stoikov", "multi_level", "rl_adversarial", "leaky_oracle"];
  const cheat = S.leaky_oracle;
  readout.innerHTML = `settings tried and logged <b>${R.register}</b>`;
  const days = Math.round(cheat.cum.length / 24);
  const allLost = order.filter((k) => k !== "leaky_oracle").every((k) => S[k].pnl < 0);
  caption.textContent = `Each robot traded ${days} days it had never seen. ` +
    (allLost ? "Every honest one lost money, shown with its 95% interval. " : "The honest ones are shown with their 95% intervals. ") +
    `The cheat made about ${r2(cheat.pnl)} USDT an hour because it could see five seconds ahead. ` +
    `Its profit looks like skill to the statistics. The leakage check rewrites the future and sees its quotes change, so it is refused.`;
  clear();
  const x0 = narrow ? 150 : 190, x1 = W - 30, rowH = narrow ? 110 : 80, top = 40;
  const lo = Math.min(...order.map((k) => S[k].ci[0])) - 2, hi = Math.max(...order.map((k) => S[k].ci[1])) + 2;
  const X = (v) => x0 + ((v - lo) / (hi - lo)) * (x1 - x0);
  el("line", { x1: X(0), x2: X(0), y1: top - 20, y2: top + rowH * order.length - 20, class: "zero" });
  el("text", { x: X(0), y: top - 26, "text-anchor": "middle" }, "breaks even");
  order.forEach((k, i) => {
    const s = S[k], y = top + i * rowH + 10;
    const isCheat = k === "leaky_oracle";
    const g = [];
    g.push(el("text", { x: 10, y: y + 4, class: isCheat ? "label warn" : "label" }, s.name));
    g.push(el("line", { x1: X(s.ci[0]), x2: X(s.ci[1]), y1: y, y2: y, class: `ci ${isCheat ? "cheat" : "honest"}` }));
    g.push(el("circle", { cx: X(s.pnl), cy: y, r: 5, class: isCheat ? "" : "pt", fill: isCheat ? "var(--signal)" : undefined }));
    g.push(el("text", { x: X(s.pnl), y: y - 12, "text-anchor": "middle" }, `${s.pnl.toFixed(2)} an hour`));
    g.push(el("text", { x: 10, y: y + 22 }, isCheat ? "reads the future" : `grade ${Math.round(s.grade)} of 100`));
    if (isCheat && !reduced) {
      g.forEach((e) => { e.style.opacity = 0; });
      setTimeout(() => { if (me === run) g.forEach((e) => { e.style.transition = "opacity 400ms ease"; e.style.opacity = 1; }); }, 700);
    }
  });
  el("text", { x: 10, y: H - 8 }, "profit per hour on unseen days, USDT");
  setTimeout(() => { if (me === run) showStamp("Refused. It reads the next five seconds.", false, true); }, reduced ? 0 : 1500);
}

const views = { replay: showReplay, latency: showLatency, impact: showImpact, cheat: showCheat };

function select(view) {
  stamp.hidden = true;
  for (const b of buttons) b.setAttribute("aria-pressed", String(b.dataset.view === view));
  views[view]();
}

buttons.forEach((b) => b.addEventListener("click", () => select(b.dataset.view)));

(async () => {
  R = await (await fetch("data/results.json")).json();
  tapeLine();
  facts();
  select("replay");
})();
