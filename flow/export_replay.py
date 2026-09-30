"""
Export a few minutes of the recorded depth stream as engine commands for the demo page, with Binance's snapshots inside that window.

Writes site/data/replay.json. The page feeds the commands to the WebAssembly engine and compares its book with each snapshot.
"""

import json
from pathlib import Path

from binance import read_recording, recording, ticks
from reconstruct import DEPTH, HALF_RANGE, Builder, top

ROOT = Path(__file__).resolve().parents[1]
MINUTES = 3


def main():
    path = recording("ethusdt")
    snaps, events = [], []
    for r in read_recording(path):
        if r["k"] == "snap":
            snaps.append(r["d"])
        elif r["k"] == "depth":
            events.append(r["d"])
    events.sort(key=lambda e: e["u"])
    base = next(s for s in snaps if any(e["U"] <= s["lastUpdateId"] + 1 <= e["u"] for e in events))
    b = Builder()
    for p, q in base["bids"]:
        b.set_level("B", ticks(p), int(round(float(q) / 0.0001)))
    for p, q in base["asks"]:
        b.set_level("S", ticks(p), int(round(float(q) / 0.0001)))
    seed_cmds = list(b.cmds)
    mid = (ticks(base["bids"][0][0]) + ticks(base["asks"][0][0])) // 2
    batches, t0 = [], None
    for e in events:
        if e["u"] <= base["lastUpdateId"]:
            continue
        t0 = t0 or e["E"]
        if e["E"] - t0 > MINUTES * 60_000:
            break
        n = len(b.cmds)
        b.apply(e["b"], e["a"])
        batches.append({"t": e["E"] - t0, "u": e["u"], "c": "\n".join(b.cmds[n:])})
    checks = []
    for s in snaps:
        exact = [i for i, bt in enumerate(batches) if bt["u"] == s["lastUpdateId"]]
        if exact:
            checks.append({"after": exact[0], "bids": top(s["bids"], DEPTH), "asks": top(s["asks"], DEPTH)})
    out = {"symbol": "ETHUSDT", "tick": 0.01, "lot": 0.0001, "min_price": mid - HALF_RANGE, "levels": 2 * HALF_RANGE,
           "seed": "\n".join(seed_cmds), "batches": batches, "checks": checks,
           "source": "Binance public depth stream, recorded 2026-09-26"}
    dest = ROOT / "site" / "data" / "replay.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, separators=(",", ":")))
    print(len(batches), "batches", len(checks), "checks", dest.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
