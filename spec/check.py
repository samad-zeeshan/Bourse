"""
Run TLC on the order lifecycle and ring handoff model and record what it found.

Writes the raw checker output to spec/output/ and a summary to eval/spec/results.json.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec"
JAR_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
# v1.7.4 is the latest stable release. The newer v1.8.0 tag is a rolling prerelease that changes under
# the same URL, so the checksum pins the exact build that produced the committed output.
JAR_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
RUNS = [
    {"config": "MCBourse.cfg", "expect": "pass"},
    {"config": "MCBoursePublishFirst.cfg", "expect": "violation"},
    {"config": "MCBourseLifo.cfg", "expect": "violation"},
]


def fetch_jar(path):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(JAR_URL, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != JAR_SHA256:
        raise SystemExit(f"{path} has sha256 {digest}, expected {JAR_SHA256}")
    return path


def clean(text):
    # Parse lines carry absolute temp paths from the machine that ran it. They say nothing about the model.
    keep = [line for line in text.splitlines() if not line.startswith(("Parsing file", "Semantic processing", "Linting of"))]
    return "\n".join(keep) + "\n"


def summarize(text, expect):
    def number(pattern):
        # The last match, because progress lines earlier in the output carry partial counts.
        found = re.findall(pattern, text)
        return int(found[-1].replace(",", "")) if found else None

    violated = re.findall(r"Error: (?:Invariant|Temporal properties?) (\w+)? ?(?:is|were) violated", text)
    passed = "Model checking completed. No error has been found." in text
    return {
        "expect": expect,
        "result": "pass" if passed else ("violation" if violated or "Error:" in text else "unknown"),
        "violated": [v for v in violated if v],
        "states_generated": number(r"([\d,]+) states generated"),
        "distinct_states": number(r"([\d,]+) distinct states found"),
        "depth": number(r"depth of the complete state graph search is (\d+)"),
        "trace_length": len(re.findall(r"^State \d+:", text, flags=re.M)) or None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--jar", type=Path, default=SPEC / ".tools" / "tla2tools.jar")
    args = parser.parse_args()
    jar = fetch_jar(args.jar)
    out_dir = SPEC / "output"
    out_dir.mkdir(exist_ok=True)
    summary = {"tool": "TLC", "tla2tools_sha256": JAR_SHA256, "module": "Bourse", "runs": {}}
    ok = True
    # One worker, because with several the run that stops at the first violation reports a different
    # state count each time, and the README table would drift for no reason.
    for run in RUNS:
        proc = subprocess.run(["java", "-XX:+UseParallelGC", "-cp", str(jar), "tlc2.TLC", "-workers", "1",
                               "-cleanup", "-config", run["config"], "Bourse.tla"],
                              cwd=SPEC, capture_output=True, text=True)
        text = clean(proc.stdout + proc.stderr)
        name = run["config"].removesuffix(".cfg")
        (out_dir / f"{name}.txt").write_text(text, encoding="utf-8", newline="\n")
        s = summarize(text, run["expect"])
        summary["runs"][name] = s
        ok &= s["result"] == run["expect"]
        print(f"{name}: {s['result']} (expected {run['expect']}), {s['distinct_states']} distinct states, "
              f"violated {s['violated'] or 'nothing'}")
    target = ROOT / "eval" / "spec" / "results.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
