"""
Download daily aggregated trade files for one symbol from Binance's public archive and verify their checksums.

Files land in data/raw/binance/aggTrades/, which is not committed.
"""

import argparse
import datetime as dt
import hashlib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://data.binance.vision/data/spot/daily/aggTrades/{s}/{s}-aggTrades-{d}.zip"
DAYS = [f"2026-09-{d:02d}" for d in range(18, 25)]


def fetch(symbol, day, out_dir):
    url = BASE.format(s=symbol, d=day)
    out = out_dir / url.rsplit("/", 1)[1]
    if not out.exists():
        urllib.request.urlretrieve(url, out)
    want = urllib.request.urlopen(url + ".CHECKSUM", timeout=30).read().decode().split()[0]
    got = hashlib.sha256(out.read_bytes()).hexdigest()
    if got != want:
        out.unlink()
        raise SystemExit(f"{out.name}: sha256 {got} does not match the published {want}")
    return out, got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--days", nargs="*", default=DAYS)
    a = ap.parse_args()
    out_dir = ROOT / "data" / "raw" / "binance" / "aggTrades"
    out_dir.mkdir(parents=True, exist_ok=True)
    for d in a.days:
        dt.date.fromisoformat(d)
        path, digest = fetch(a.symbol, d, out_dir)
        print(path.name, digest)


if __name__ == "__main__":
    main()
