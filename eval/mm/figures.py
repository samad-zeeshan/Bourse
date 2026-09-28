"""
Draw the strategy comparison and cumulative profit figures from eval/mm/results.json.
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT = Path(__file__).resolve().parent
INK, MUTED, ACCENT, SIGNAL, BG = "#e8e6e1", "#8b8984", "#e8ff47", "#ff4d3d", "#0b0b0b"


def style(ax, fig):
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    for s in ax.spines.values():
        s.set_color(MUTED)
    ax.tick_params(colors=INK)
    ax.xaxis.label.set_color(INK)
    ax.yaxis.label.set_color(INK)


def main():
    r = json.loads((OUT / "results.json").read_text())
    S = r["strategies"]
    names = list(S)
    fig, ax = plt.subplots(figsize=(6.4, 3.2), dpi=100)
    style(ax, fig)
    for i, k in enumerate(names):
        s = S[k]["oos"]
        c = SIGNAL if S[k]["leakage"]["verdict"] != "clean" else ACCENT
        ax.plot(s["pnl_per_hour_ci95"], [i, i], color=c, lw=6, solid_capstyle="butt")
        ax.plot([s["pnl_per_hour"]], [i], "o", color=INK, ms=5)
    ax.axvline(0, color=MUTED, ls="--", lw=1)
    ax.set_yticks(range(len(names)), [S[k]["name"] for k in names])
    ax.invert_yaxis()
    ax.set_xlabel("profit per hour on unseen days, USDT, with 95% interval")
    fig.tight_layout()
    fig.savefig(OUT / "strategies.svg", format="svg", metadata={"Date": None})
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.4, 3.2), dpi=100)
    style(ax, fig)
    for k in names:
        cum = S[k]["oos"]["cumulative_pnl"]
        c = SIGNAL if S[k]["leakage"]["verdict"] != "clean" else (ACCENT if k == "rl_adversarial" else MUTED)
        ax.plot(range(1, len(cum) + 1), cum, color=c, lw=1.5, label=S[k]["name"])
    ax.axhline(0, color=MUTED, ls="--", lw=1)
    ax.set_xlabel("hours of unseen trading")
    ax.set_ylabel("running profit, USDT")
    leg = ax.legend(frameon=False, fontsize=8)
    for t in leg.get_texts():
        t.set_color(INK)
    fig.tight_layout()
    fig.savefig(OUT / "cumulative.svg", format="svg", metadata={"Date": None})
    plt.close(fig)


if __name__ == "__main__":
    main()
