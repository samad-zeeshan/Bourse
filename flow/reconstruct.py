"""
Replay the recorded Binance depth stream through the engine and compare the engine's book with Binance's own snapshots.

Writes eval/data/reconstruction.json. Needs the release build of tools/replay.
"""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from binance import lots, read_recording, recording, ticks

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "target" / "release" / ("replay.exe" if os.name == "nt" else "replay")
DEPTH = 20
HALF_RANGE = 1 << 19


def level_id(side, price):
    # One resting order stands for one price level, so the level's price makes a stable, unique id.
    return price * 2 + (0 if side == "B" else 1)


class Builder:
    def __init__(self):
        self.book = {"B": {}, "S": {}}
        self.cmds = []
        self.crossing_adds = 0

    def best(self, side):
        lv = self.book[side]
        if not lv:
            return None
        return max(lv) if side == "B" else min(lv)

    def set_level(self, side, price, qty):
        cur = self.book[side].get(price, 0)
        oid = level_id(side, price)
        if qty == cur:
            return
        if qty == 0:
            self.cmds.append(f"C,{oid}")
            del self.book[side][price]
        elif cur == 0:
            opp = self.best("S" if side == "B" else "B")
            # Binance never shows a crossed book once a batch is applied. An add that crosses here would
            # trade inside the engine, so it is counted and left out rather than allowed to fake a fill.
            if opp is not None and ((side == "B" and price >= opp) or (side == "S" and price <= opp)):
                self.crossing_adds += 1
                return
            self.cmds.append(f"L,{oid},0,{side},{price},{qty},GTC")
            self.book[side][price] = qty
        else:
            self.cmds.append(f"R,{oid},{price},{qty}")
            self.book[side][price] = qty

    def apply(self, bids, asks):
        ups = [("B", ticks(p), lots(q)) for p, q in bids] + [("S", ticks(p), lots(q)) for p, q in asks]
        # Removals and cuts first, then adds. Inside one 100 ms batch the order between sides is not
        # given, and doing adds first can briefly cross the book.
        shrink = [u for u in ups if u[2] < self.book[u[0]].get(u[1], 0)]
        grow = [u for u in ups if u[2] >= self.book[u[0]].get(u[1], 0)]
        for side, p, q in shrink + grow:
            self.set_level(side, p, q)


def top(levels, n):
    return [(ticks(p), lots(q)) for p, q in levels if float(q) > 0][:n]


def parse_depth_line(line):
    idx, b, a = line.rstrip("\n").split("|")
    side = lambda s: [tuple(map(int, x.split(":"))) for x in s.split()] if s else []
    return int(idx), side(b), side(a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recording", type=Path)
    a = ap.parse_args()
    path = a.recording or recording("ethusdt")
    snaps, events, gaps = [], [], 0
    for r in read_recording(path):
        if r["k"] == "snap":
            snaps.append(r["d"])
        elif r["k"] == "depth":
            events.append(r["d"])
        elif r["k"] == "gap":
            gaps += 1
    events.sort(key=lambda e: e["u"])

    # Start from the first snapshot the diff stream actually covers, per Binance's documented procedure.
    base = next(s for s in snaps if any(e["U"] <= s["lastUpdateId"] + 1 <= e["u"] for e in events))
    x0 = base["lastUpdateId"]
    b = Builder()
    for p, q in base["bids"]:
        b.set_level("B", ticks(p), lots(q))
    for p, q in base["asks"]:
        b.set_level("S", ticks(p), lots(q))
    seeded = len(b.cmds)
    mid = (ticks(base["bids"][0][0]) + ticks(base["asks"][0][0])) // 2

    applied, after, breaks, prev_u = [], [], 0, None
    for e in events:
        if e["u"] <= x0:
            continue
        if prev_u is not None and e["U"] != prev_u + 1:
            breaks += 1
            break
        b.apply(e["b"], e["a"])
        applied.append(e)
        after.append(len(b.cmds) - 1)
        prev_u = e["u"]

    checks = []
    for s in snaps:
        x = s["lastUpdateId"]
        if x <= x0 or not applied or x > applied[-1]["u"]:
            continue
        k = max(i for i, e in enumerate(applied) if e["u"] <= x) if applied[0]["u"] <= x else -1
        before = after[k] if k >= 0 else seeded - 1
        exact = k >= 0 and applied[k]["u"] == x
        nxt = after[k + 1] if not exact and k + 1 < len(applied) else None
        checks.append({"x": x, "before": before, "next": nxt, "exact": exact,
                       "bids": top(s["bids"], DEPTH), "asks": top(s["asks"], DEPTH)})

    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / "cmds.csv").write_text("\n".join(b.cmds) + "\n")
        at = sorted({c["before"] for c in checks} | {c["next"] for c in checks if c["next"] is not None})
        (t / "at.txt").write_text(" ".join(map(str, at)) + "\n")
        run = subprocess.run([str(EXE), "run", "--in", str(t / "cmds.csv"), "--out", str(t / "ev.bin"),
                              "--text", str(t / "ev.txt"), "--depth-out", str(t / "depth.txt"),
                              "--depth", str(DEPTH), "--depth-at", str(t / "at.txt"),
                              "--min-price", str(mid - HALF_RANGE), "--levels", str(2 * HALF_RANGE),
                              "--max-orders", str(1 << 20)], check=True, capture_output=True, text=True)
        summary = json.loads(run.stdout)
        depth = {i: (bd, ak) for i, bd, ak in map(parse_depth_line, (t / "depth.txt").read_text().splitlines())}
        trades = sum(1 for l in (t / "ev.txt").read_text().splitlines() if l.startswith("T "))
        rejects = sum(1 for l in (t / "ev.txt").read_text().splitlines() if l.startswith("J "))

    exact_ok = consistent_ok = level_mismatches = compared = 0
    for c in checks:
        eb, ea = depth[c["before"]]
        if c["exact"]:
            ok = eb == c["bids"] and ea == c["asks"]
            exact_ok += ok
            level_mismatches += sum(x != y for x, y in zip(eb + ea, c["bids"] + c["asks"]))
        else:
            # The snapshot's id falls inside one 100 ms batch, so each level must match the book either just
            # before or just after that batch.
            nb, na = depth.get(c["next"], (eb, ea))
            ok = all(l in (x, y) for l, x, y in zip(c["bids"], eb, nb)) and all(
                l in (x, y) for l, x, y in zip(c["asks"], ea, na)) and len(c["bids"]) <= max(len(eb), len(nb))
            consistent_ok += ok
        compared += 1
    res = {
        "source": "Binance spot ETHUSDT public depth stream (100 ms diffs) and REST snapshots, recorded locally",
        "recording": path.name,
        "minutes": round((applied[-1]["E"] - applied[0]["E"]) / 60000, 1) if applied else 0,
        "depth_batches": len(applied),
        "level_updates": sum(len(e["b"]) + len(e["a"]) for e in applied),
        "engine_commands": summary["commands"],
        "engine_events": summary["events"],
        "engine_trades": trades,
        "engine_rejects": rejects,
        "crossing_adds_skipped": b.crossing_adds,
        "sequence_breaks": breaks,
        "recorder_gaps": gaps,
        "levels_compared_per_side": DEPTH,
        "snapshots_compared": compared,
        "snapshots_exactly_aligned": sum(c["exact"] for c in checks),
        "exact_matches": exact_ok,
        "straddling_consistent": consistent_ok,
        "snapshots_matching": exact_ok + consistent_ok,
        "level_mismatches_in_exact": level_mismatches,
    }
    out = ROOT / "eval" / "data" / "reconstruction.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
