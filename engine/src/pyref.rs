//! Client for the Python reference book, used by the property tests and the differential fuzzer.
//!
//! One long-lived Python process answers every case, so a test run pays interpreter startup once.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command as Proc, Stdio};

use crate::book::Config;
use crate::codec::command_to_text;
use crate::types::{Command, Stp};

pub struct PyRef {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

/// The interpreter to use: `BOURSE_PYTHON` if set, else the repo's `.venv`, else whatever is on PATH.
pub fn python(root: &Path) -> PathBuf {
    if let Ok(p) = std::env::var("BOURSE_PYTHON") {
        return p.into();
    }
    for rel in [".venv/Scripts/python.exe", ".venv/bin/python"] {
        let p = root.join(rel);
        if p.exists() {
            return p;
        }
    }
    if cfg!(windows) { "python".into() } else { "python3".into() }
}

impl PyRef {
    pub fn spawn(root: &Path) -> std::io::Result<PyRef> {
        let mut child = Proc::new(python(root))
            .arg(root.join("tests/property/reference_book.py"))
            .arg("--serve")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()?;
        let stdin = child.stdin.take().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        Ok(PyRef { child, stdin, stdout })
    }

    /// Run one case from an empty book. Returns the event lines for each command.
    pub fn run(&mut self, cfg: &Config, cmds: &[Command]) -> std::io::Result<Vec<Vec<String>>> {
        let stp = match cfg.stp {
            Stp::Off => "off",
            Stp::CancelIncoming => "incoming",
            Stp::CancelResting => "resting",
        };
        let mut line = format!(
            "{{\"cfg\":{{\"min_price\":{},\"levels\":{},\"max_orders\":{},\"stp\":\"{stp}\"}},\"cmds\":[",
            cfg.min_price, cfg.levels, cfg.max_orders
        );
        for (i, c) in cmds.iter().enumerate() {
            if i > 0 {
                line.push(',');
            }
            line.push('"');
            line.push_str(&command_to_text(c));
            line.push('"');
        }
        line.push_str("]}\n");
        self.stdin.write_all(line.as_bytes())?;
        self.stdin.flush()?;
        let mut reply = String::new();
        if self.stdout.read_line(&mut reply)? == 0 {
            return Err(std::io::Error::other("reference process exited"));
        }
        Ok(parse_nested(&reply))
    }
}

impl Drop for PyRef {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

// The reply is a JSON list of lists of plain strings with no quotes or escapes inside, so a
// bracket counter is enough and keeps the engine crate free of dependencies.
fn parse_nested(s: &str) -> Vec<Vec<String>> {
    let mut out = Vec::new();
    let mut cur: Option<Vec<String>> = None;
    let mut in_str = false;
    let mut buf = String::new();
    let mut depth = 0;
    for ch in s.trim().chars() {
        match ch {
            '"' if in_str => {
                cur.as_mut().unwrap().push(std::mem::take(&mut buf));
                in_str = false;
            }
            '"' => in_str = true,
            c if in_str => buf.push(c),
            '[' => {
                depth += 1;
                if depth == 2 {
                    cur = Some(Vec::new());
                }
            }
            ']' => {
                if depth == 2 {
                    out.push(cur.take().unwrap());
                }
                depth -= 1;
            }
            _ => {}
        }
    }
    out
}

#[cfg(test)]
mod tests {
    #[test]
    fn parses_nested_string_lists() {
        let v = super::parse_nested("[[\"A 1 5 B 10 3\"], [], [\"T 2 6 5 S 10 3\", \"X 3 6 1 ioc\"]]\n");
        assert_eq!(v, vec![vec!["A 1 5 B 10 3".to_string()], vec![], vec!["T 2 6 5 S 10 3".into(), "X 3 6 1 ioc".into()]]);
    }
}
