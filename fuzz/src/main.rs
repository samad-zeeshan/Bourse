//! Differential fuzzer: random command streams go through the engine, the invariant shadow and the Python reference.
//!
//! Any disagreement or broken invariant is logged and its case saved. A heartbeat line goes to the log every ten minutes.

use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use bourse::check::Shadow;
use bourse::codec::{command_to_text, event_to_text};
use bourse::gen::FlowGen;
use bourse::pipeline::{self, Pins};
use bourse::pyref::PyRef;
use bourse::*;

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        // splitmix64, so every case seed gives an independent stream that can be replayed from the log.
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n.max(1)
    }
    fn chance(&mut self, pct: u64) -> bool {
        self.below(100) < pct
    }
}

#[derive(Clone, Copy, Debug)]
enum Profile {
    /// Few ids, few levels, tiny capacity. Collisions and full books on almost every case.
    Tight,
    /// The seeded generator with a random seed, a deeper book, and edge prices mixed in.
    Flow,
    /// Prices at the range edges, zero sizes, the reserved id and ids near it.
    Edges,
}

fn make_case(rng: &mut Rng) -> (Profile, Config, Vec<Command>) {
    let profile = match rng.below(10) {
        0..=4 => Profile::Tight,
        5..=7 => Profile::Flow,
        _ => Profile::Edges,
    };
    let stp = match rng.below(3) {
        0 => Stp::Off,
        1 => Stp::CancelIncoming,
        _ => Stp::CancelResting,
    };
    let min_price = rng.below(2000) as i64 - 1000;
    match profile {
        Profile::Tight | Profile::Edges => {
            let levels = 2 + rng.below(40) as usize;
            let cfg = Config { min_price, levels, max_orders: 1 + rng.below(30) as usize, stp };
            let ids = 2 + rng.below(60);
            let n = 1 + rng.below(400) as usize;
            let edge = matches!(profile, Profile::Edges);
            let cmds = (0..n)
                .map(|_| {
                    let id = if edge && rng.chance(5) { u64::MAX - rng.below(2) } else { 1 + rng.below(ids) };
                    let price = if edge && rng.chance(40) {
                        [min_price - 1, min_price, min_price + levels as i64 - 1, min_price + levels as i64][rng.below(4) as usize]
                    } else {
                        min_price - 2 + rng.below(levels as u64 + 4) as i64
                    };
                    let qty = if rng.chance(if edge { 10 } else { 3 }) { 0 } else { 1 + rng.below(20) };
                    let side = if rng.chance(50) { Side::Buy } else { Side::Sell };
                    let owner = rng.below(4) as u32;
                    match rng.below(15) {
                        0..=7 => Command::Limit { id, owner, side, price, qty, tif: if rng.chance(20) { Tif::Ioc } else { Tif::Gtc } },
                        8..=9 => Command::Market { id, owner, side, qty },
                        10..=12 => Command::Cancel { id },
                        _ => Command::Replace { id, price, qty },
                    }
                })
                .collect();
            (profile, cfg, cmds)
        }
        Profile::Flow => {
            let levels = 256 + rng.below(4096) as i64;
            let cfg = Config { min_price, levels: levels as usize, max_orders: 50 + rng.below(3000) as usize, stp };
            let mut g = FlowGen::new(rng.next(), min_price, levels);
            let n = 100 + rng.below(3000) as usize;
            (profile, cfg, (0..n).map(|_| g.next_command()).collect())
        }
    }
}

fn run_engine(cfg: Config, cmds: &[Command]) -> (Vec<Vec<Event>>, Option<String>) {
    let mut book = Book::new(cfg);
    let mut shadow = Shadow::new(cfg);
    let mut out = Vec::with_capacity(cmds.len());
    for (i, c) in cmds.iter().enumerate() {
        let mut ev = Vec::new();
        book.process(c, &mut ev);
        if let Err(v) = shadow.step(c, &ev) {
            return (out, Some(format!("invariant '{}' at command {i}: {}", v.invariant, v.detail)));
        }
        out.push(ev);
    }
    if let Err(e) = book.check_structure() {
        return (out, Some(format!("structure: {e}")));
    }
    (out, None)
}

struct Log {
    file: File,
}

impl Log {
    fn line(&mut self, s: &str) {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs();
        writeln!(self.file, "{now} {s}").unwrap();
        self.file.flush().unwrap();
    }
}

fn save_case(dir: &Path, n: u64, cfg: &Config, cmds: &[Command], why: &str) -> PathBuf {
    fs::create_dir_all(dir).unwrap();
    let p = dir.join(format!("fail-{n}.csv"));
    let mut f = File::create(&p).unwrap();
    writeln!(f, "# {why}\n# {cfg:?}").unwrap();
    for c in cmds {
        writeln!(f, "{}", command_to_text(c)).unwrap();
    }
    p
}

fn arg<T: std::str::FromStr>(name: &str, default: T) -> T {
    let a: Vec<String> = std::env::args().collect();
    a.iter().position(|x| x == name).and_then(|i| a.get(i + 1)).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn main() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf();
    let hours: f64 = arg("--hours", 24.0);
    let seed: u64 = arg("--seed", 20260926);
    let beat = Duration::from_secs(arg("--heartbeat-secs", 600u64));
    let log_path = PathBuf::from(arg("--log", root.join("fuzz/logs/fuzz.log").to_string_lossy().into_owned()));
    fs::create_dir_all(log_path.parent().unwrap()).unwrap();
    let mut log = Log { file: OpenOptions::new().create(true).append(true).open(&log_path).unwrap() };
    let mut py = PyRef::spawn(&root).expect("start the Python reference");
    let commit = std::process::Command::new("git").args(["rev-parse", "--short", "HEAD"]).current_dir(&root).output().map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default();
    log.line(&format!("START commit={commit} hours={hours} seed={seed} heartbeat_secs={} reference=tests/property/reference_book.py", beat.as_secs()));

    let start = Instant::now();
    let deadline = Duration::from_secs_f64(hours * 3600.0);
    let mut next_beat = beat;
    let (mut cases, mut commands, mut events, mut violations, mut diffs, mut pipeline_checks) = (0u64, 0u64, 0u64, 0u64, 0u64, 0u64);
    let mut by_profile = [0u64; 3];
    let mut master = Rng(seed);
    while start.elapsed() < deadline {
        let case_seed = master.next();
        let mut rng = Rng(case_seed);
        let (profile, cfg, cmds) = make_case(&mut rng);
        by_profile[profile as usize] += 1;
        cases += 1;
        commands += cmds.len() as u64;
        let (ours, broken) = run_engine(cfg, &cmds);
        events += ours.iter().map(|e| e.len() as u64).sum::<u64>();
        if let Some(why) = broken {
            violations += 1;
            let p = save_case(&root.join("fuzz/scratch"), cases, &cfg, &cmds, &why);
            log.line(&format!("VIOLATION case={cases} seed={case_seed} {why} saved={}", p.display()));
            continue;
        }
        let theirs = match py.run(&cfg, &cmds) {
            Ok(t) => t,
            Err(e) => {
                log.line(&format!("REFERENCE-RESTART case={cases} error={e}"));
                py = PyRef::spawn(&root).expect("restart the Python reference");
                continue;
            }
        };
        let mismatch = ours.iter().zip(theirs.iter()).position(|(a, b)| a.iter().map(event_to_text).ne(b.iter().cloned()));
        if mismatch.is_some() || ours.len() != theirs.len() {
            diffs += 1;
            let at = mismatch.unwrap_or(ours.len().min(theirs.len()));
            let why = format!("differs from reference at command {at}");
            let p = save_case(&root.join("fuzz/scratch"), cases, &cfg, &cmds, &why);
            log.line(&format!("DIFF case={cases} seed={case_seed} {why} saved={}", p.display()));
        }
        // One case in a hundred also goes through the threaded pipeline, which must match the direct run exactly.
        if cases % 100 == 0 {
            pipeline_checks += 1;
            let mut piped = Vec::new();
            pipeline::run(cfg, cmds.clone(), 64, Pins::default(), |e| piped.push(*e));
            if piped != ours.concat() {
                violations += 1;
                log.line(&format!("VIOLATION case={cases} seed={case_seed} pipeline output differs from direct run"));
            }
        }
        if start.elapsed() >= next_beat {
            next_beat += beat;
            log.line(&format!(
                "HEARTBEAT elapsed_s={} cases={cases} commands={commands} events={events} violations={violations} reference_diffs={diffs} pipeline_checks={pipeline_checks} tight={} flow={} edges={}",
                start.elapsed().as_secs(), by_profile[0], by_profile[1], by_profile[2]
            ));
        }
    }
    log.line(&format!(
        "END elapsed_s={} cases={cases} commands={commands} events={events} violations={violations} reference_diffs={diffs} pipeline_checks={pipeline_checks} tight={} flow={} edges={}",
        start.elapsed().as_secs(), by_profile[0], by_profile[1], by_profile[2]
    ));
    if violations + diffs > 0 {
        std::process::exit(1);
    }
}
