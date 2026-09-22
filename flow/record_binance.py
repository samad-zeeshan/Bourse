"""
Record Binance's public depth updates, trades and periodic book snapshots for one symbol.

Writes gzipped JSON lines under data/raw/binance/. Each line carries the local receive time in nanoseconds.
"""

import argparse
import asyncio
import gzip
import json
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
REST = "https://api.binance.com/api/v3/depth?symbol={sym}&limit={limit}"
WS = "wss://stream.binance.com:9443/stream?streams={s}@depth@100ms/{s}@trade"


def snapshot(sym, limit):
    with urllib.request.urlopen(REST.format(sym=sym.upper(), limit=limit), timeout=20) as r:
        return json.loads(r.read())


async def record(sym, hours, every, limit, out):
    stop = time.time() + hours * 3600
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "at", encoding="utf-8") as f:
        def write(kind, payload):
            f.write(json.dumps({"t": time.time_ns(), "k": kind, "d": payload}, separators=(",", ":")) + "\n")

        while time.time() < stop:
            try:
                async with websockets.connect(WS.format(s=sym.lower()), max_size=2**22, ping_interval=20) as ws:
                    write("open", {})
                    # The snapshot is taken after the stream opens, so the first diffs overlap it. That overlap is
                    # what Binance's own procedure needs to line the two up by update id.
                    next_snap = 0.0
                    while time.time() < stop:
                        if time.time() >= next_snap:
                            snap = await asyncio.to_thread(snapshot, sym, limit)
                            write("snap", snap)
                            next_snap = time.time() + every
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        m = json.loads(msg)
                        write("depth" if m["stream"].endswith("100ms") else "trade", m["data"])
            except Exception as e:  # a dropped socket leaves a marker so replay knows the diffs have a gap
                write("gap", {"error": repr(e)})
                await asyncio.sleep(2)
        write("close", {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ethbtc")
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--snapshot-every", type=float, default=60.0)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    out = a.out or ROOT / "data" / "raw" / "binance" / f"{a.symbol}-{time.strftime('%Y%m%dT%H%M%S')}.jsonl.gz"
    asyncio.run(record(a.symbol, a.hours, a.snapshot_every, a.limit, out))


if __name__ == "__main__":
    main()
