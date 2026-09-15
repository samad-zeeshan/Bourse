//! The same command stream must give byte-identical events across separate processes and across modes.

use std::fs;
use std::path::PathBuf;
use std::process::Command;

fn replay(args: &[&str]) -> String {
    let out = Command::new(env!("CARGO_BIN_EXE_replay")).args(args).output().expect("run replay");
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    String::from_utf8(out.stdout).unwrap()
}

#[test]
fn two_processes_and_two_modes_write_the_same_bytes() {
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join("determinism");
    fs::create_dir_all(&dir).unwrap();
    let p = |n: &str| dir.join(n).to_string_lossy().into_owned();
    replay(&["gen", "--seed", "11", "--n", "300000", "--out", &p("flow.bin")]);
    replay(&["run", "--in", &p("flow.bin"), "--out", &p("a.bin")]);
    replay(&["run", "--in", &p("flow.bin"), "--out", &p("b.bin")]);
    replay(&["run", "--in", &p("flow.bin"), "--out", &p("c.bin"), "--mode", "direct"]);
    let a = fs::read(p("a.bin")).unwrap();
    assert!(a.len() > 300_000 * 40);
    assert!(a == fs::read(p("b.bin")).unwrap(), "two pipeline runs differ");
    assert!(a == fs::read(p("c.bin")).unwrap(), "pipeline and direct differ");
}

#[test]
fn text_and_binary_inputs_give_the_same_events() {
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join("determinism-text");
    fs::create_dir_all(&dir).unwrap();
    let p = |n: &str| dir.join(n).to_string_lossy().into_owned();
    replay(&["gen", "--seed", "12", "--n", "50000", "--out", &p("flow.bin")]);
    replay(&["gen", "--seed", "12", "--n", "50000", "--out", &p("flow.csv")]);
    replay(&["run", "--in", &p("flow.bin"), "--out", &p("a.bin")]);
    replay(&["run", "--in", &p("flow.csv"), "--out", &p("b.bin")]);
    assert!(fs::read(p("a.bin")).unwrap() == fs::read(p("b.bin")).unwrap());
}
