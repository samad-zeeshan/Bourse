"""
Build the market order tape the market maker trades against, from real days or from calibrated synthetic flow.

A tape is five arrays over market orders (time, side, size, first and last price in ticks) plus the touch after each.
"""

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "flow"))
import hawkes  # noqa: E402
from binance import DAYS_DEFAULT, load_aggtrades, market_orders  # noqa: E402

CACHE = ROOT / "data" / "raw" / "tapes"
DAYS = DAYS_DEFAULT


def _touch(side, first, last):
    """Best bid and ask after each order, inferred from the trades alone.

    A buy stops at the level it left behind, so that level is the new ask. The other side is set one tick
    away. The recorded snapshots have a one-tick median spread, and carrying the other side's last seen
    price instead went stale between trades and made the spread look fifteen ticks wide on average.
    """
    bid = np.where(side > 0, last - 1, last)
    ask = np.where(side > 0, last, last + 1)
    return bid.astype(np.int64), ask.astype(np.int64)


def real(day):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{day}.npz"
    if not path.exists():
        tr = load_aggtrades(day)
        mo = market_orders(tr)
        side = mo["side"].to_numpy().astype(np.int64)
        first = np.round(mo["first_price"].to_numpy() / 0.01).astype(np.int64)
        last = np.round(mo["last_price"].to_numpy() / 0.01).astype(np.int64)
        bid, ask = _touch(side, first, last)
        np.savez_compressed(path, t=mo["t"].to_numpy(), side=side, qty=mo["qty"].to_numpy(), first=first, last=last,
                            bid=bid, ask=ask)
    z = np.load(path)
    return {k: z[k] for k in z.files}


def window(tape, start, end):
    i, j = np.searchsorted(tape["t"], [start, end])
    out = {k: v[i:j] for k, v in tape.items()}
    out["t"] = out["t"] - start
    return out


def calibrate(days):
    """What the synthetic tape needs, from training days only."""
    hw = {d["day"]: d for d in json.loads((ROOT / "eval" / "flow" / "hawkes.json").read_text())["days"]}
    sig, eta, reach, sizes, spreads = [], [], [], [], []
    for d in days:
        tp = real(d)
        mid = (tp["bid"] + tp["ask"]) / 2
        # Price noise per second from the touch sampled once a second.
        grid = np.arange(0, tp["t"][-1], 1.0)
        m = mid[np.clip(np.searchsorted(tp["t"], grid) - 1, 0, None)]
        sig.append(np.std(np.diff(m)))
        # Impact per order: how far the mid moves across one order, against signed square-root size.
        x = tp["side"][1:] * np.sqrt(tp["qty"][1:])
        y = np.diff(mid)
        eta.append(float(np.dot(x, y) / np.dot(x, x)))
        reach.append(np.abs(tp["last"] - tp["first"]))
        sizes.append(tp["qty"])
        spreads.append(np.median(tp["ask"] - tp["bid"]))
    last = hw[days[-1]]["params"]
    return {
        "days": list(days),
        "sigma_per_sec_ticks": float(np.mean(sig)),
        "impact_eta": float(np.mean(eta)),
        "reach_ticks": np.quantile(np.concatenate(reach), np.linspace(0, 1, 1001)).tolist(),
        "size_eth": np.quantile(np.concatenate(sizes), np.linspace(0, 1, 1001)).tolist(),
        "hawkes": {"mu": last["mu"], "alpha": last["alpha"], "beta": last["beta"]},
    }


def synthetic(cal, seconds, seed, regime=None):
    """One synthetic tape. `regime` scales drift, clustering, impact and noise for the adversary."""
    r = {"drift": 0.0, "clustering": 1.0, "impact": 1.0, "noise": 1.0, **(regime or {})}
    p = hawkes.from_json(cal["hawkes"])
    p["alpha"] = p["alpha"] * r["clustering"]
    # Hold the average arrival rate fixed when clustering changes, so the adversary tests timing, not volume.
    br0 = hawkes.branching_ratio(hawkes.from_json(cal["hawkes"])["alpha"])
    br = hawkes.branching_ratio(p["alpha"])
    if br >= 0.97:
        p["alpha"] = p["alpha"] * 0.97 / br
        br = 0.97
    p["mu"] = p["mu"] * (1 - br) / (1 - br0)
    t, d = hawkes.simulate(p, seconds, seed)
    rng = np.random.default_rng(seed + 7)
    n = len(t)
    side = np.where(d == 0, 1, -1).astype(np.int64)
    qty = np.interp(rng.random(n), np.linspace(0, 1, 1001), cal["size_eth"])
    reach = np.round(np.interp(rng.random(n), np.linspace(0, 1, 1001), cal["reach_ticks"])).astype(np.int64)
    # Mid in half ticks: noise at one-second resolution plus drift, plus each order's own push.
    # The measured per-second noise already contains the orders' pushes, so only the remainder is drawn here.
    rate = n / seconds
    push_var = rate * (cal["impact_eta"] * r["impact"]) ** 2 * float(np.mean(qty))
    sd = np.sqrt(max(cal["sigma_per_sec_ticks"] ** 2 - push_var, 0.25 * cal["sigma_per_sec_ticks"] ** 2)) * r["noise"]
    noise = np.cumsum(rng.normal(0, sd, int(seconds) + 2)) + r["drift"] * np.arange(int(seconds) + 2)
    push = np.cumsum(side * cal["impact_eta"] * r["impact"] * np.sqrt(qty))
    mid = 250_000 + noise[np.floor(t).astype(np.int64)] + push
    bid = np.floor(mid).astype(np.int64)
    ask = bid + 1
    first = np.where(side > 0, np.roll(ask, 1), np.roll(bid, 1))
    first[0] = ask[0] if side[0] > 0 else bid[0]
    last = first + side * reach
    return {"t": t, "side": side, "qty": qty, "first": first, "last": last, "bid": bid, "ask": ask}
