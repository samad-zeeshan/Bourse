//! Command line for the simulator: `day` writes one synthetic day's summary, `impact` runs paired metaorder experiments.
//!
//! Output is JSON lines, one per run, for flow/sqrt_law.py to fit.

use std::io::Write;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use bourse::Side;
use sim::*;

fn arg(name: &str, default: &str) -> String {
    let a: Vec<String> = std::env::args().collect();
    a.iter().position(|x| x == name).and_then(|i| a.get(i + 1)).cloned().unwrap_or_else(|| default.to_string())
}

fn params() -> Params {
    let mut p = Params::load(&arg("--params", "flow/params.json"), &arg("--day", "2026-09-21"));
    p.liquidity = arg("--liquidity", "1").parse().unwrap();
    p.cancel_scale = arg("--cancel-scale", "1").parse().unwrap();
    p
}

fn day() {
    let p = params();
    let seconds: f64 = arg("--seconds", "7200").parse().unwrap();
    let seeds: u64 = arg("--seeds", "8").parse().unwrap();
    let mut out = std::fs::File::create(arg("--out", "day.jsonl")).unwrap();
    for seed in 0..seeds {
        let times: Vec<f64> = (0..=(seconds as usize)).map(|s| s as f64).collect();
        let arr = arrivals(&p, seconds, seed);
        let r = run(&p, seed, seconds, None, false, &times, (0.0, seconds));
        let mo: Vec<f64> = arr.iter().filter(|(_, k)| matches!(k, Kind::MarketBuy | Kind::MarketSell)).map(|(t, _)| *t).collect();
        let mut sp: Vec<i64> = r.spread_at.iter().copied().filter(|&x| x > 0).collect();
        sp.sort_unstable();
        let one_sided = r.spread_at.iter().filter(|&&x| x < 0).count();
        let mids: Vec<String> = r.mid2_at.iter().step_by(60).map(|m| (*m as f64 / 2.0 - CENTER as f64).to_string()).collect();
        writeln!(out, "{{\"seed\":{seed},\"seconds\":{seconds},\"liquidity\":{},\"median_spread_ticks\":{},\"one_sided_seconds\":{one_sided},\"bg_fill_ratio\":{:.5},\"depth10_lots\":{:.0},\"commands\":{},\"market_orders\":{},\"volume_lots\":{},\"mid_ticks_per_minute\":[{}],\"mo_times\":[{}]}}",
            p.liquidity, sp.get(sp.len() / 2).copied().unwrap_or(-1), r.bg_fill_ratio, r.depth10_mean, r.commands, mo.len(), r.volume_in_window, mids.join(","),
            mo.iter().map(|t| format!("{t:.4}")).collect::<Vec<_>>().join(",")).unwrap();
    }
}

fn impact() {
    let p = params();
    let seeds: u64 = arg("--seeds", "200").parse().unwrap();
    let mode = arg("--mode", "split");
    let sizes: Vec<u64> = arg("--sizes", "1000,3000,10000,30000,100000,300000").split(',').map(|s| s.parse().unwrap()).collect();
    let (start, duration, tail) = (600.0, 600.0, 60.0);
    let seconds = start + duration + tail;
    let jobs: Vec<(u64, u64)> = sizes.iter().flat_map(|&q| (0..seeds).map(move |s| (q, s))).collect();
    let next = AtomicUsize::new(0);
    let out = Mutex::new(std::fs::File::create(arg("--out", "impact.jsonl")).unwrap());
    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4).saturating_sub(2).max(1);
    std::thread::scope(|sc| {
        for _ in 0..threads {
            sc.spawn(|| loop {
                let i = next.fetch_add(1, Ordering::Relaxed);
                let Some(&(q, seed)) = jobs.get(i) else { break };
                // Alternate buy and sell metaorders so any drift in the background cancels out on average.
                let side = if seed % 2 == 0 { Side::Buy } else { Side::Sell };
                let children = match mode.as_str() {
                    "single" => 1,
                    _ => 60,
                };
                let meta = Meta { side, total: q, children, start, duration };
                let times = [start, start + duration, seconds];
                let frozen = mode == "no_follow";
                // The paired run shares every background draw, so the difference is the metaorder's own effect.
                let base = run(&p, seed, seconds, None, frozen, &times, (start, start + duration));
                let with = run(&p, seed, seconds, Some(&meta), frozen, &times, (start, start + duration));
                let sgn = if side == Side::Buy { 1.0 } else { -1.0 };
                let imp = |k: usize| sgn * (with.mid2_at[k] - base.mid2_at[k]) as f64 / 2.0;
                let line = format!(
                    "{{\"mode\":\"{mode}\",\"q\":{q},\"seed\":{seed},\"filled\":{},\"window_volume\":{},\"impact_end_ticks\":{},\"impact_after_ticks\":{},\"base_move_ticks\":{}}}",
                    with.meta_filled, base.volume_in_window, imp(1), imp(2),
                    (base.mid2_at[1] - base.mid2_at[0]) as f64 / 2.0
                );
                writeln!(out.lock().unwrap(), "{line}").unwrap();
            });
        }
    });
}

fn main() {
    match std::env::args().nth(1).as_deref() {
        Some("day") => day(),
        Some("impact") => impact(),
        _ => {
            eprintln!("usage: sim day|impact [--params F] [--day D] [--seeds N] [--out F] [--mode split|single|frozen] [--sizes a,b,c]");
            std::process::exit(2);
        }
    }
}
