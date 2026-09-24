"""
Read the Binance recordings and daily trade archives into plain arrays in engine units.

Prices become integer ticks of 0.01 USDT and quantities integer lots of 0.0001 ETH, the symbol's own steps.
"""

import gzip
import io
import json
import zipfile
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "binance"
TICK = 0.01
LOT = 0.0001


DAYS_DEFAULT = [f"2026-09-{d:02d}" for d in range(18, 25)]


def ticks(price):
    return int(round(float(price) / TICK))


def lots(qty):
    return int(round(float(qty) / LOT))


def read_recording(path):
    """Yield recorded lines in order. A recording cut off mid-write ends quietly at the last whole line."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    return
    except (EOFError, zlib.error, gzip.BadGzipFile):
        return


def recording(symbol="ethusdt"):
    files = sorted(RAW.glob(f"{symbol}-*.jsonl.gz"))
    if not files:
        raise SystemExit(f"no recording for {symbol} under {RAW}")
    return files[-1]


def load_aggtrades(day, symbol="ETHUSDT"):
    """One day of aggregated trades. `side` is +1 when the buyer took liquidity and -1 when the seller did."""
    path = RAW / "aggTrades" / f"{symbol}-aggTrades-{day}.zip"
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None,
                     names=["agg_id", "price", "qty", "first", "last", "ts_us", "buyer_maker", "best"])
    df["buyer_maker"] = df["buyer_maker"].astype(str).str.lower() == "true"
    return pd.DataFrame({
        "t": (df["ts_us"].to_numpy() - df["ts_us"].iloc[0]) / 1e6,
        "ts_us": df["ts_us"].to_numpy(),
        "price": df["price"].to_numpy(),
        "qty": df["qty"].to_numpy(),
        "side": np.where(df["buyer_maker"].to_numpy(), -1, 1),
    })


def market_orders(trades):
    """Group fills that share a timestamp and a taker side into one market order.

    One aggressive order that walks several price levels shows up as several aggregated trades with the
    same time, and for arrival modelling it is one event, not several.
    """
    key = (trades["ts_us"].to_numpy() != np.roll(trades["ts_us"].to_numpy(), 1)) | (
        trades["side"].to_numpy() != np.roll(trades["side"].to_numpy(), 1))
    key[0] = True
    grp = np.cumsum(key) - 1
    g = trades.groupby(grp)
    return pd.DataFrame({
        "t": g["t"].first().to_numpy(),
        "side": g["side"].first().to_numpy(),
        "qty": g["qty"].sum().to_numpy(),
        "first_price": g["price"].first().to_numpy(),
        "last_price": g["price"].last().to_numpy(),
        "levels": g["price"].nunique().to_numpy(),
    })
