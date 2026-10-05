"""
Replay one seeded stream in separate processes and record the output hash for each.

--docker adds a run inside a Linux container. --check regenerates here and fails if the hash differs from the committed one.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval" / "determinism" / "results.json"
SEED, N = 20260926, 5_000_000
EXE = ROOT / "target" / "release" / ("replay.exe" if os.name == "nt" else "replay")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def replay(*args):
    out = subprocess.run([str(EXE), *args], check=True, capture_output=True, text=True).stdout.strip()
    return json.loads(out) if out else None


def local_runs(tmp):
    flow = tmp / "flow.bin"
    replay("gen", "--seed", str(SEED), "--n", str(N), "--out", str(flow))
    runs = []
    for i, mode in enumerate(["pipeline", "pipeline", "direct"]):
        out = tmp / f"events-{i}.bin"
        r = replay("run", "--in", str(flow), "--out", str(out), "--mode", mode)
        runs.append({"where": f"{platform.system()} process {i + 1}", "mode": mode, "events": r["events"],
                     "sha256": sha(out), "bytes": out.stat().st_size})
    return sha(flow), runs


def docker_run():
    # Same seed and code, built by a Linux toolchain inside a container. Same physical CPU, different OS,
    # compiler target and C library, which is as close to a second machine as this PC gets.
    script = (f"cargo build -q --release -p replay --target-dir /tmp/t && "
              f"/tmp/t/release/replay gen --seed {SEED} --n {N} --out /tmp/flow.bin && "
              f"/tmp/t/release/replay run --in /tmp/flow.bin --out /tmp/ev.bin && sha256sum /tmp/flow.bin /tmp/ev.bin && "
              f"rustc --version")
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    out = subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT.as_posix()}:/src", "-w", "/src", "rust:1.98-slim",
                          "bash", "-c", script], check=True, capture_output=True, text=True, env=env).stdout
    lines = out.strip().splitlines()
    run = json.loads(next(l for l in lines if l.startswith("{")))
    hashes = [l.split()[0] for l in lines if l.endswith(".bin")]
    return hashes[0], {"where": "Linux container (rust:1.98-slim)", "mode": "pipeline", "events": run["events"],
                       "sha256": hashes[1], "toolchain": lines[-1]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docker", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as t:
        flow_sha, runs = local_runs(Path(t))
    if a.check:
        want = json.loads(OUT.read_text())
        got = runs[0]["sha256"]
        ok = got == want["output_sha256"] and all(r["sha256"] == got for r in runs)
        print(f"output {got} {'matches' if ok else 'DIFFERS FROM'} committed {want['output_sha256']}")
        sys.exit(0 if ok else 1)
    if a.docker:
        dflow, drun = docker_run()
        assert dflow == flow_sha, "the container generated a different input stream"
        runs.append(drun)
    hashes = {r["sha256"] for r in runs}
    res = {
        "seed": SEED, "commands": N, "input_sha256": flow_sha,
        "output_sha256": runs[0]["sha256"], "all_identical": len(hashes) == 1,
        "runs": runs,
        "note": "Separate processes on one Windows PC plus one Linux container on the same PC. "
                "CI repeats the check on a GitHub Linux runner against output_sha256.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps({k: res[k] for k in ("output_sha256", "all_identical")}))


if __name__ == "__main__":
    main()
