"""
Calibrate market order arrivals per day and the book's add and cancel flow from the recorded depth stream.

Writes eval/flow/hawkes.json (fits and goodness of fit) and flow/params.json (what the simulator reads).
"""

import json
from pathlib import Path

import numpy as np

import hawkes
from binance import DAYS_DEFAULT, load_aggtrades, lots, market_orders, read_recording, recording, ticks

ROOT = Path(__file__).resolve().parents[1]
# Two hours in the middle of the UTC day. A constant baseline rate cannot follow a whole day's
# seasonality, so a fixed window keeps every day's fit comparable.
WINDOW = (12 * 3600, 14 * 3600)


def window_events(day):
    tr = load_aggtrades(day)
    sod = (tr["ts_us"].to_numpy() // 1_000_000) % 86400
    keep = (sod >= WINDOW[0]) & (sod < WINDOW[1])
    w = tr[keep].reset_index(drop=True)
    w["t"] = (w["ts_us"] - w["ts_us"].iloc[0]) / 1e6
    mo = market_orders(w)
    t = mo["t"].to_numpy() + np.arange(len(mo)) * 1e-9  # strict order for ties across sides
    d = np.where(mo["side"].to_numpy() > 0, 0, 1)
    T = float(WINDOW[1] - WINDOW[0])
    minute = (w["t"] // 60).astype(int)
    px = w.groupby(minute)["price"].last().to_numpy()
    ret = np.diff(np.log(px))
    return {"t": t, "d": d, "T": T, "mo": mo, "sigma_1m": float(ret.std()), "price": float(w["price"].median())}


def book_flow(path):
    """Add and cancel rates and sizes near the touch, from the 100 ms depth diffs."""
    book = {"B": {}, "S": {}}
    adds, cancels, add_sizes, add_offsets, spreads, depth10 = [], [], [], [], [], []
    t0 = t1 = None
    started = False
    for r in read_recording(path):
        if r["k"] == "snap" and started:
            # Mean resting quantity within ten ticks of the touch, both sides, from Binance's own snapshots.
            bids, asks = r["d"]["bids"], r["d"]["asks"]
            bb, ba = ticks(bids[0][0]), ticks(asks[0][0])
            depth10.append((sum(lots(q) for p, q in bids if bb - ticks(p) < 10)
                            + sum(lots(q) for p, q in asks if ticks(p) - ba < 10)) / 2)
            continue
        if r["k"] == "snap" and not started:
            for p, q in r["d"]["bids"]:
                book["B"][ticks(p)] = lots(q)
            for p, q in r["d"]["asks"]:
                book["S"][ticks(p)] = lots(q)
            started = True
            continue
        if r["k"] != "depth" or not started:
            continue
        e = r["d"]
        t0 = t0 or e["E"]
        t1 = e["E"]
        bb, ba = max(book["B"]), min(book["S"])
        spreads.append(ba - bb)
        for side, ups in (("B", e["b"]), ("S", e["a"])):
            for p, q in ups:
                p, q = ticks(p), lots(q)
                off = (bb - p) if side == "B" else (p - ba)
                cur = book[side].get(p, 0)
                if 0 <= off < 20:
                    if q > cur:
                        adds.append(off)
                        add_sizes.append(q - cur)
                    elif q < cur:
                        cancels.append(cur - q)
                if q == 0:
                    book[side].pop(p, None)
                else:
                    book[side][p] = q
    secs = (t1 - t0) / 1000
    offs = np.bincount(np.array(adds), minlength=20)[:20]
    return {
        "seconds": secs,
        "add_rate_per_side": len(adds) / secs / 2,
        "size_decrease_rate_per_side": len(cancels) / secs / 2,
        "add_offset_probs": (offs / offs.sum()).tolist(),
        "add_size_lots_quantiles": np.quantile(add_sizes, np.linspace(0, 1, 1001)).tolist(),
        "depth10_lots_per_side": float(np.mean(depth10)),
        "median_spread_ticks": float(np.median(spreads)),
        "note": "Changes near the touch seen through 100 ms diffs. Several orders inside one batch look like one, so the rates are lower bounds.",
    }


def main():
    fits, prev = [], None
    for day in DAYS_DEFAULT:
        ev = window_events(day)
        p = hawkes.fit(ev["t"], ev["d"], ev["T"])
        po = hawkes.poisson(ev["t"], ev["d"], ev["T"])
        g = hawkes.goodness(p, ev["t"], ev["d"])
        gp = hawkes.goodness(po, ev["t"], ev["d"])
        n = len(ev["t"])
        row = {
            "day": day, "events": n, "buy_events": int((ev["d"] == 0).sum()), "seconds": ev["T"],
            "params": hawkes.to_json(p),
            "loglik_per_event": p["loglik"] / n, "poisson_loglik_per_event": po["loglik"] / n,
            "aic_hawkes": 2 * 12 - 2 * p["loglik"], "aic_poisson": 2 * 2 - 2 * po["loglik"],
            "ks": [x["ks"] for x in g], "ks_p": [x["p"] for x in g], "ks_poisson": [x["ks"] for x in gp],
            "qq": {"model": g[0]["qq_model"], "buy": g[0]["qq_data"], "sell": g[1]["qq_data"],
                   "poisson_buy": gp[0]["qq_data"]},
            "sigma_1m": ev["sigma_1m"], "price": ev["price"],
            "mo_size_quantiles": np.quantile(ev["mo"]["qty"].to_numpy(), np.linspace(0, 1, 1001)).tolist(),
            "mo_volume_per_sec": float(ev["mo"]["qty"].sum() / ev["T"]),
            "mo_levels_mean": float(ev["mo"]["levels"].mean()),
        }
        # Out of sample: yesterday's parameters scored on today's window, against yesterday's constant rates.
        if prev is not None:
            p0, po0 = prev
            oos = hawkes.goodness(p0, ev["t"], ev["d"])
            row["oos"] = {"from_day": fits[-1]["day"],
                          "loglik_per_event": hawkes.loglik(p0, ev["t"], ev["d"], ev["T"]) / n,
                          "poisson_loglik_per_event": hawkes.loglik(po0, ev["t"], ev["d"], ev["T"]) / n,
                          "ks": [x["ks"] for x in oos]}
        fits.append(row)
        prev = (p, po)
        print(day, n, "BR", round(row["params"]["branching_ratio"], 3), "KS", [round(k, 3) for k in row["ks"]],
              "Poisson KS", [round(k, 3) for k in row["ks_poisson"]])
    flow = book_flow(recording("ethusdt"))
    out = ROOT / "eval" / "flow" / "hawkes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"window_utc": "12:00-14:00", "symbol": "ETHUSDT", "kernel": "sum of two exponentials",
                               "days": fits, "book_flow": flow}, indent=2) + "\n")
    params = {"symbol": "ETHUSDT", "days": {f["day"]: {**f["params"], "sigma_1m": f["sigma_1m"],
                                                        "mo_size_quantiles": f["mo_size_quantiles"]} for f in fits},
              "book_flow": flow}
    (ROOT / "flow" / "params.json").write_text(json.dumps(params, indent=2) + "\n")


if __name__ == "__main__":
    main()
