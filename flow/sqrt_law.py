"""
Run metaorders of many sizes through the engine against calibrated flow and fit impact = c * (Q/V)^delta.

Three arms: split into 60 children (the main result), split while refills stop following the price, and one order.
Writes eval/flow/sqrt_law.json and eval/flow/sqrt_law.svg.
"""

import json
import os
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "target" / "release" / ("sim.exe" if os.name == "nt" else "sim")
DAY = "2026-09-21"
# Chosen on synthetic days so the book matches the recorded one: one-tick median spread and about the
# same resting quantity within ten ticks of the touch. See the README's flow section for the sweep.
LIQUIDITY, CANCEL_SCALE = 4, 0.75
SIZES = [100_000, 200_000, 400_000, 800_000, 1_600_000, 3_200_000, 6_400_000, 12_800_000]
SEEDS = 300
BOOT = 2000


def run(mode, out):
    subprocess.run([str(SIM), "impact", "--day", DAY, "--seeds", str(SEEDS), "--mode", mode,
                    "--liquidity", str(LIQUIDITY), "--cancel-scale", str(CANCEL_SCALE),
                    "--sizes", ",".join(map(str, SIZES)), "--out", str(out)], check=True, cwd=ROOT)
    return [json.loads(l) for l in out.read_text().splitlines()]


def fit(x, y):
    keep = (x > 0) & (y > 0)
    if keep.sum() < 3:
        return np.nan, np.nan
    b, a = np.polyfit(np.log(x[keep]), np.log(y[keep]), 1)
    return b, np.exp(a)


def analyse(rows, rng):
    rows = [r for r in rows]
    sizes = sorted({r["q"] for r in rows})
    by = {q: np.array([[r["impact_end_ticks"], r["window_volume"], r["filled"], r["base_move_ticks"]]
                       for r in rows if r["q"] == q]) for q in sizes}
    # Scale: the spread of the no-metaorder price move over the same ten minutes, the model's own volatility.
    sigma = float(np.std(np.concatenate([by[q][:, 3] for q in sizes])))
    vol = float(np.mean(np.concatenate([by[q][:, 1] for q in sizes])))
    x = np.array([np.mean(by[q][:, 2]) / vol for q in sizes])
    y = np.array([np.mean(by[q][:, 0]) / sigma for q in sizes])
    se = np.array([np.std(by[q][:, 0]) / np.sqrt(len(by[q])) / sigma for q in sizes])
    fill = np.array([np.mean(by[q][:, 2]) / q for q in sizes])
    # Fit only sizes whose mean impact is at least two standard errors above zero and that filled at least
    # 95%. Below that the points are noise, and a partly filled order measures book depth, not impact.
    used = (y > 2 * se) & (fill >= 0.95)
    delta, c = fit(x[used], y[used])
    boots = []
    for _ in range(BOOT):
        yb = np.array([np.mean(by[q][rng.integers(0, len(by[q]), len(by[q])), 0]) / sigma for q in sizes])
        boots.append(fit(x[used], yb[used])[0])
    boots = np.array(boots)
    boots = boots[np.isfinite(boots)]
    ok = used.sum() >= 3 and len(boots) > BOOT // 2
    return {"delta": float(delta) if ok else None, "c": float(c) if ok else None,
            "delta_ci95": [float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))] if ok else None,
            "points_used": int(used.sum()), "sigma_ticks_10min": sigma, "window_volume_lots": vol,
            "points": [{"q_lots": q, "q_over_v": float(xi), "impact_over_sigma": float(yi), "se": float(s),
                        "fill_ratio": float(f), "used": bool(u)} for q, xi, yi, s, f, u in zip(sizes, x, y, se, fill, used)]}


def plot(res, path):
    fig, ax = plt.subplots(figsize=(6.4, 4.2), dpi=100)
    colors = {"split": "#e8ff47", "no_follow": "#9aa0a6", "single": "#5f6368"}
    for mode, r in res["arms"].items():
        pts = [p for p in r["points"] if p["used"]]
        if r["delta"] is None:
            continue
        x = np.array([p["q_over_v"] for p in pts])
        y = np.array([p["impact_over_sigma"] for p in pts])
        s = np.array([p["se"] for p in pts])
        ax.errorbar(x, y, yerr=1.96 * s, fmt="o", ms=4, color=colors[mode], label=f"{res['labels'][mode]}, fitted exponent {r['delta']:.2f}")
        xx = np.geomspace(x.min(), x.max(), 50)
        ax.plot(xx, r["c"] * xx ** r["delta"], color=colors[mode], lw=1)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("order size as a share of volume traded meanwhile")
    ax.set_ylabel("price move, in units of normal ten-minute moves")
    ax.set_facecolor("#0b0b0b")
    fig.patch.set_facecolor("#0b0b0b")
    for s in ax.spines.values():
        s.set_color("#5f6368")
    ax.tick_params(colors="#d0d0d0")
    ax.xaxis.label.set_color("#d0d0d0")
    ax.yaxis.label.set_color("#d0d0d0")
    leg = ax.legend(frameon=False, fontsize=8)
    for t in leg.get_texts():
        t.set_color("#d0d0d0")
    fig.tight_layout()
    fig.savefig(path, format="svg", metadata={"Date": None})
    plt.close(fig)


def main():
    rng = np.random.default_rng(20260926)
    scratch = ROOT / "fuzz" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    arms = {}
    for mode in ["split", "no_follow", "single"]:
        rows = run(mode, scratch / f"impact-{mode}.jsonl")
        arms[mode] = analyse(rows, rng)
        print(mode, arms[mode]["delta"], arms[mode]["delta_ci95"], arms[mode]["points_used"])
    res = {"day": DAY, "liquidity": LIQUIDITY, "cancel_scale": CANCEL_SCALE, "seeds_per_size": SEEDS,
           "children": 60, "execution_seconds": 600, "bootstrap": BOOT,
           "labels": {"split": "split into 60 pieces", "no_follow": "split, refills do not follow the price", "single": "one order"},
           "arms": arms}
    out = ROOT / "eval" / "flow" / "sqrt_law.json"
    out.write_text(json.dumps(res, indent=2) + "\n")
    plot(res, ROOT / "eval" / "flow" / "sqrt_law.svg")


if __name__ == "__main__":
    main()
