"""
Generate synthetic two-hour days on the engine and compare them with the real window they were calibrated on.

Writes eval/flow/synthetic_days.json. The Hawkes refit on synthetic arrivals checks the simulator draws what it was given.
"""

import json
import os
import subprocess
from pathlib import Path

import numpy as np

import hawkes
from sqrt_law import CANCEL_SCALE, DAY, LIQUIDITY

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "target" / "release" / ("sim.exe" if os.name == "nt" else "sim")
SECONDS, SEEDS = 7200, 8


def main():
    out = ROOT / "fuzz" / "scratch" / "days.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(SIM), "day", "--day", DAY, "--seeds", str(SEEDS), "--seconds", str(SECONDS),
                    "--liquidity", str(LIQUIDITY), "--cancel-scale", str(CANCEL_SCALE), "--out", str(out)], check=True)
    days = [json.loads(l) for l in out.read_text().splitlines()]
    real = next(d for d in json.loads((ROOT / "eval" / "flow" / "hawkes.json").read_text())["days"] if d["day"] == DAY)
    flow = json.loads((ROOT / "eval" / "flow" / "hawkes.json").read_text())["book_flow"]
    # Refit on the first synthetic day. Sides are not in the day summary, so fit the pooled one-dimensional process.
    t = np.array(days[0]["mo_times"])
    p = hawkes.fit(t, np.zeros(len(t), dtype=np.int64), float(SECONDS), n_dim=1)
    real_br = real["params"]["branching_ratio"]
    sim_move = [float(np.std(np.diff(d["mid_ticks_per_minute"]))) for d in days]
    res = {
        "day": DAY, "seconds": SECONDS, "seeds": SEEDS, "liquidity": LIQUIDITY, "cancel_scale": CANCEL_SCALE,
        "engine_commands_per_day": float(np.mean([d["commands"] for d in days])),
        "market_orders_per_sec": {"synthetic": float(np.mean([d["market_orders"] for d in days])) / SECONDS,
                                  "real": real["events"] / real["seconds"]},
        "refit_branching_ratio_pooled": {"synthetic": hawkes.branching_ratio(p["alpha"]), "calibrated": real_br},
        "median_spread_ticks": {"synthetic": float(np.median([d["median_spread_ticks"] for d in days])),
                                "real": flow["median_spread_ticks"]},
        "depth10_lots_per_side": {"synthetic": float(np.mean([d["depth10_lots"] for d in days])),
                                  "real": flow["depth10_lots_per_side"]},
        "traded_eth_per_sec": {"synthetic": float(np.mean([d["volume_lots"] for d in days])) / SECONDS / 1e4,
                               "real": real["mo_volume_per_sec"]},
        "market_volume_filled_share": float(np.mean([d["bg_fill_ratio"] for d in days])),
        "one_sided_share_of_time": float(np.mean([d["one_sided_seconds"] for d in days])) / SECONDS,
        "price_move_per_minute_ticks": {"synthetic": float(np.mean(sim_move)), "real": real["sigma_1m"] * real["price"] / 0.01},
        "mid_path_ticks_per_minute": days[0]["mid_ticks_per_minute"],
    }
    (ROOT / "eval" / "flow" / "synthetic_days.json").write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps({k: v for k, v in res.items() if k != "mid_path_ticks_per_minute"}, indent=2))


if __name__ == "__main__":
    main()
