//! Per-order latency with rdtsc on a pinned thread, plus throughput alone and through the three-thread pipeline.
//!
//! Writes bench/results.json and bench/methodology.json. `--quick` is the CI sanity run and writes nothing.

use std::arch::x86_64::{__rdtscp, _mm_lfence, _rdtsc};
use std::process::Command as Proc;
use std::time::{Duration, Instant};

use bourse::affinity::pin_current;
use bourse::gen::FlowGen;
use bourse::pipeline::{self, Pins};
use bourse::spsc::channel;
use bourse::*;

const LEVELS: i64 = 1 << 16;
const MATCHER_CPU: usize = 4;

// lfence before the first read stops earlier loads drifting into the timed region, and rdtscp at the end
// waits for the timed work to retire. This is the pairing Intel's measurement note recommends.
#[inline(always)]
fn start() -> u64 {
    unsafe {
        _mm_lfence();
        _rdtsc()
    }
}

#[inline(always)]
fn stop() -> u64 {
    let mut aux = 0u32;
    unsafe {
        let t = __rdtscp(&mut aux);
        _mm_lfence();
        t
    }
}

fn tsc_per_ns() -> f64 {
    let t0 = Instant::now();
    let c0 = start();
    std::thread::sleep(Duration::from_millis(500));
    let c1 = stop();
    (c1 - c0) as f64 / t0.elapsed().as_nanos() as f64
}

fn pct(sorted: &[u64], p: f64) -> u64 {
    sorted[((sorted.len() as f64 - 1.0) * p).round() as usize]
}

fn summary(mut v: Vec<u64>, per_ns: f64) -> String {
    v.sort_unstable();
    let ns = |c: u64| (c as f64 / per_ns).round() as u64;
    format!(
        "{{\"samples\":{},\"p50_ns\":{},\"p90_ns\":{},\"p99_ns\":{},\"p999_ns\":{},\"p9999_ns\":{},\"max_ns\":{},\"mean_ns\":{:.1}}}",
        v.len(),
        ns(pct(&v, 0.5)),
        ns(pct(&v, 0.9)),
        ns(pct(&v, 0.99)),
        ns(pct(&v, 0.999)),
        ns(pct(&v, 0.9999)),
        ns(*v.last().unwrap()),
        v.iter().sum::<u64>() as f64 / v.len() as f64 / per_ns
    )
}

fn histogram(v: &[u64], per_ns: f64) -> String {
    // Log-spaced bins, four per doubling from 8 ns, which is what the site draws.
    let edges: Vec<f64> = (0..=56).map(|i| 8.0 * 2f64.powf(i as f64 / 4.0)).collect();
    let mut counts = vec![0u64; edges.len()];
    for &c in v {
        let ns = c as f64 / per_ns;
        let i = edges.iter().position(|&e| ns < e).unwrap_or(edges.len()).saturating_sub(1);
        counts[i] += 1;
    }
    let e: Vec<String> = edges.iter().map(|x| format!("{:.1}", x)).collect();
    let c: Vec<String> = counts.iter().map(|x| x.to_string()).collect();
    format!("{{\"edges_ns\":[{}],\"counts\":[{}]}}", e.join(","), c.join(","))
}

fn kind(c: &Command, before: u64, after: u64) -> usize {
    match c {
        Command::Limit { .. } if after > before => 1,
        Command::Limit { .. } => 0,
        Command::Cancel { .. } => 2,
        Command::Market { .. } => 3,
        Command::Replace { .. } => 4,
    }
}

fn shell(cmd: &str, args: &[&str]) -> String {
    Proc::new(cmd).args(args).output().map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default()
}

fn cpu_model() -> String {
    let out = shell("reg", &["query", r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0", "/v", "ProcessorNameString"]);
    out.lines().find_map(|l| l.split("REG_SZ").nth(1)).map(|s| s.trim().to_string()).unwrap_or_else(|| "unknown".into())
}

fn clean(s: &str) -> String {
    s.replace('\\', "/").replace('"', "'")
}

fn main() {
    let quick = std::env::args().any(|a| a == "--quick");
    let (warm_n, n) = if quick { (100_000, 300_000) } else { (2_000_000, 20_000_000) };
    let pinned = pin_current(MATCHER_CPU);
    let per_ns = tsc_per_ns();
    let cfg = Config { min_price: 0, levels: LEVELS as usize, max_orders: 1 << 20, stp: Stp::Off };
    let mut g = FlowGen::new(20260926, 0, LEVELS);
    let warm: Vec<Command> = (0..warm_n).map(|_| g.next_command()).collect();
    let hot: Vec<Command> = (0..n).map(|_| g.next_command()).collect();

    // Timer overhead: two reads back to back with nothing between them.
    let mut over = Vec::with_capacity(1_000_000);
    for _ in 0..1_000_000 {
        let a = start();
        over.push(stop() - a);
    }

    let mut book = Book::new(cfg);
    let mut sink = CountingSink::default();
    for c in &warm {
        book.process(c, &mut sink);
    }
    let mut lat = vec![0u64; hot.len()];
    let mut by_kind: Vec<Vec<u64>> = vec![Vec::with_capacity(hot.len() / 2); 5];
    for (i, c) in hot.iter().enumerate() {
        let before = sink.trades;
        let a = start();
        book.process(c, &mut sink);
        let b = stop();
        lat[i] = b - a;
        by_kind[kind(c, before, sink.trades)].push(b - a);
    }
    let resting = book.live_orders();

    // Throughput on one thread: the same stream, no timer inside the loop.
    let mut book2 = Book::new(cfg);
    let mut sink2 = CountingSink::default();
    for c in &warm {
        book2.process(c, &mut sink2);
    }
    let t = Instant::now();
    for c in &hot {
        book2.process(c, &mut sink2);
    }
    let single_ops = hot.len() as f64 / t.elapsed().as_secs_f64();

    // Saturation through the pipeline: the reader pushes as fast as the ring accepts.
    let all: Vec<Command> = warm.iter().chain(hot.iter()).copied().collect();
    let total = all.len();
    let t = Instant::now();
    let events = pipeline::run(cfg, all, 1 << 16, Pins { reader: Some(2), matcher: Some(MATCHER_CPU), writer: Some(6) }, |_| {});
    let pipe_ops = total as f64 / t.elapsed().as_secs_f64();

    // One-way ring handoff: ping-pong a counter between two pinned threads and halve the round trip.
    let (mut ptx, mut prx) = channel::<u64>(1024);
    let (mut qtx, mut qrx) = channel::<u64>(1024);
    let rounds = if quick { 50_000 } else { 1_000_000 };
    let echo = std::thread::spawn(move || {
        pin_current(8);
        for _ in 0..rounds + 1000 {
            let v = prx.pop();
            qtx.push(v);
        }
    });
    let mut rtt = Vec::with_capacity(rounds);
    for i in 0..rounds + 1000 {
        let a = start();
        ptx.push(i as u64);
        qrx.pop();
        let b = stop();
        if i >= 1000 {
            rtt.push((b - a) / 2);
        }
    }
    echo.join().unwrap();

    let kinds = ["limit_resting", "limit_crossing", "cancel", "market", "replace"];
    let kind_json: Vec<String> =
        kinds.iter().zip(by_kind).filter(|(_, v)| !v.is_empty()).map(|(k, v)| format!("\"{k}\":{}", summary(v, per_ns))).collect();
    let results = format!(
        "{{\n  \"per_order\": {},\n  \"per_order_by_kind\": {{{}}},\n  \"timer_overhead\": {},\n  \"ring_one_way\": {},\n  \"histogram\": {},\n  \"single_thread_orders_per_sec\": {:.0},\n  \"pipeline_orders_per_sec\": {:.0},\n  \"pipeline_events\": {},\n  \"resting_orders_at_end\": {},\n  \"trades\": {}\n}}\n",
        summary(lat.clone(), per_ns),
        kind_json.join(","),
        summary(over, per_ns),
        summary(rtt, per_ns),
        histogram(&lat, per_ns),
        single_ops,
        pipe_ops,
        events,
        resting,
        sink.trades
    );
    assert!(sink.trades > 0 && sink2.hash == sink.hash, "the two single-thread runs disagree");
    if quick {
        println!("{results}");
        return;
    }
    let power = shell("powercfg", &["/getactivescheme"]);
    let meth = format!(
        "{{\n  \"cpu\": \"{}\",\n  \"logical_cpus\": {},\n  \"os\": \"{}\",\n  \"compiler\": \"{}, release profile, fat LTO, one codegen unit\",\n  \"power_plan\": \"{}\",\n  \"tsc_ticks_per_ns\": {:.4},\n  \"matcher_cpu\": {},\n  \"pinned\": {},\n  \"thread_priority\": \"time critical\",\n  \"core_isolation\": \"none, a Windows PC with the usual background services running\",\n  \"perf_counters\": \"not used, Linux perf does not exist on Windows\",\n  \"workload\": \"seeded synthetic flow: about 47% limit adds near the touch with one in ten priced through it, 43% cancels, 5% market orders, 5% replaces\",\n  \"warm_up_orders\": {},\n  \"measured_orders\": {},\n  \"price_levels\": {},\n  \"timer\": \"lfence and rdtsc before, rdtscp and lfence after, per order, overhead reported separately and not subtracted\"\n}}\n",
        cpu_model(),
        std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0),
        clean(&shell("cmd", &["/c", "ver"])),
        clean(&shell("rustc", &["--version"])),
        clean(power.split(':').nth(1).map(|s| s.trim()).unwrap_or("unknown")),
        per_ns,
        MATCHER_CPU,
        pinned,
        warm_n,
        n,
        LEVELS
    );
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    std::fs::write(root.join("results.json"), &results).unwrap();
    std::fs::write(root.join("methodology.json"), &meth).unwrap();
    println!("{results}{meth}");
}
