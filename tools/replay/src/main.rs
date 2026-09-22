//! Replay a recorded command stream through the engine and write the event stream, or generate a stream.
//!
//! Output is the fixed 48-byte event record, so two runs can be compared with a byte diff or a hash.

use std::fs;
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::time::Instant;

use bourse::codec::*;
use bourse::gen::FlowGen;
use bourse::pipeline::{self, Pins};
use bourse::*;
use sha2::{Digest, Sha256};

struct Args(Vec<String>);

impl Args {
    fn get(&self, name: &str) -> Option<&str> {
        self.0.iter().position(|a| a == name).and_then(|i| self.0.get(i + 1)).map(|s| s.as_str())
    }
    fn num<T: std::str::FromStr>(&self, name: &str, default: T) -> T {
        self.get(name).map(|v| v.parse().unwrap_or_else(|_| panic!("bad value for {name}"))).unwrap_or(default)
    }
    fn path(&self, name: &str) -> Option<PathBuf> {
        self.get(name).map(PathBuf::from)
    }
}

fn config(a: &Args) -> Config {
    let stp = match a.get("--stp").unwrap_or("off") {
        "off" => Stp::Off,
        "incoming" => Stp::CancelIncoming,
        "resting" => Stp::CancelResting,
        s => panic!("bad --stp {s}"),
    };
    Config { min_price: a.num("--min-price", 0), levels: a.num("--levels", 1 << 16), max_orders: a.num("--max-orders", 1 << 20), stp }
}

fn read_commands(path: &PathBuf) -> Vec<Command> {
    let bytes = fs::read(path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    if path.extension().is_some_and(|e| e == "bin") {
        assert!(bytes.len() % CMD_BYTES == 0, "{} is not a whole number of records", path.display());
        bytes.chunks_exact(CMD_BYTES).map(|c| decode_command(c).unwrap()).collect()
    } else {
        let text = String::from_utf8(bytes).unwrap();
        text.lines().filter(|l| !l.trim().is_empty() && !l.starts_with('#')).map(|l| command_from_text(l).unwrap()).collect()
    }
}

fn gen(a: &Args) {
    let out = a.path("--out").expect("--out");
    let levels: i64 = a.num("--levels", 1 << 16);
    let mut g = FlowGen::new(a.num("--seed", 1), a.num("--min-price", 0), levels);
    let n: usize = a.num("--n", 1_000_000);
    let mut w = BufWriter::new(fs::File::create(&out).unwrap());
    let bin = out.extension().is_some_and(|e| e == "bin");
    let mut rec = [0u8; CMD_BYTES];
    for _ in 0..n {
        let c = g.next_command();
        if bin {
            encode_command(&c, &mut rec);
            w.write_all(&rec).unwrap();
        } else {
            writeln!(w, "{}", command_to_text(&c)).unwrap();
        }
    }
}

fn run(a: &Args) {
    let cfg = config(a);
    let cmds = read_commands(&a.path("--in").expect("--in"));
    let mut out = BufWriter::new(fs::File::create(a.path("--out").expect("--out")).unwrap());
    let mut text = a.path("--text").map(|p| BufWriter::new(fs::File::create(p).unwrap()));
    let mut depth_out = a.path("--depth-out").map(|p| BufWriter::new(fs::File::create(p).unwrap()));
    let depth: usize = a.num("--depth", 10);
    // Command indices after which to dump depth. Without it, --depth-out dumps after every command.
    let depth_at: Option<Vec<usize>> = a.path("--depth-at").map(|p| {
        fs::read_to_string(p).unwrap().split_whitespace().map(|v| v.parse().unwrap()).collect()
    });
    let mut next_dump = 0usize;
    let mode = a.get("--mode").unwrap_or(if depth_out.is_some() { "direct" } else { "pipeline" });
    let mut hash = Sha256::new();
    let mut rec = [0u8; EVENT_BYTES];
    let mut write = |e: &Event| {
        encode_event(e, &mut rec);
        hash.update(rec);
        out.write_all(&rec).unwrap();
        if let Some(t) = text.as_mut() {
            writeln!(t, "{}", event_to_text(e)).unwrap();
        }
    };
    let start = Instant::now();
    let n_cmds = cmds.len();
    let events = match mode {
        "pipeline" => {
            let pins = Pins { reader: a.get("--pin").map(|_| 2), matcher: a.get("--pin").map(|_| 4), writer: a.get("--pin").map(|_| 6) };
            pipeline::run(cfg, cmds, 1 << 16, pins, &mut write)
        }
        "direct" => {
            let mut book = Book::new(cfg);
            let mut buf = Vec::with_capacity(64);
            let (mut bids, mut asks) = (vec![(0i64, 0u64); depth], vec![(0i64, 0u64); depth]);
            let mut n = 0u64;
            for (i, c) in cmds.iter().enumerate() {
                buf.clear();
                book.process(c, &mut buf);
                for e in &buf {
                    write(e);
                }
                n += buf.len() as u64;
                let due = match &depth_at {
                    None => true,
                    Some(at) => {
                        let hit = next_dump < at.len() && at[next_dump] == i;
                        while next_dump < at.len() && at[next_dump] <= i {
                            next_dump += 1;
                        }
                        hit
                    }
                };
                if let (Some(d), true) = (depth_out.as_mut(), due) {
                    let nb = book.depth(Side::Buy, &mut bids);
                    let na = book.depth(Side::Sell, &mut asks);
                    let fmt = |v: &[(i64, u64)]| v.iter().map(|(p, q)| format!("{p}:{q}")).collect::<Vec<_>>().join(" ");
                    writeln!(d, "{i}|{}|{}", fmt(&bids[..nb]), fmt(&asks[..na])).unwrap();
                }
            }
            n
        }
        m => panic!("bad --mode {m}"),
    };
    let elapsed = start.elapsed();
    drop(write);
    out.flush().unwrap();
    println!(
        "{{\"commands\":{n_cmds},\"events\":{events},\"mode\":\"{mode}\",\"seconds\":{:.3},\"sha256\":\"{:x}\"}}",
        elapsed.as_secs_f64(),
        hash.finalize()
    );
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let a = Args(argv.clone());
    match argv.first().map(|s| s.as_str()) {
        Some("gen") => gen(&a),
        Some("run") => run(&a),
        _ => {
            eprintln!("usage: replay gen --seed S --n N --out FILE | replay run --in FILE --out FILE [--text FILE] [--mode pipeline|direct] [--depth-out FILE [--depth N] [--depth-at FILE]]");
            std::process::exit(2);
        }
    }
}
