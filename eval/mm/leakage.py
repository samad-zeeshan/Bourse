"""
Structural leakage check: rewrite everything after a moment and see whether the strategy's quotes at that moment change.

A strategy that only reads the past must quote identically. Statistics cannot do this job, since a leak can look like skill.
"""

import numpy as np

import env
import rl


def splice(a, b, cut_t):
    """Tape `a` up to cut_t, then tape `b` from cut_t on, shifted up so prices join without a jump."""
    i = np.searchsorted(a["t"], cut_t, side="right")
    j = np.searchsorted(b["t"], cut_t, side="right")
    shift = int(a["last"][i - 1] - b["last"][j]) if i > 0 and j < len(b["t"]) else 0
    out = {}
    for k in a:
        tail = b[k][j:] + (shift if k in ("first", "last", "bid", "ask") else 0)
        out[k] = np.concatenate([a[k][:i], tail])
    return out


def check(strategy, tape_a, tape_b, scaler, n=100, seed=0):
    """Returns how many of n sampled moments gave different quotes on the rewritten future."""
    rng = np.random.default_rng(seed)
    moments = np.sort(rng.uniform(600, min(tape_a["t"][-1], tape_b["t"][-1]) - 600, n))
    b1, a1, b2, a2 = (np.empty(3) for _ in range(4))
    differ = 0
    for t in moments:
        alt = splice(tape_a, tape_b, t)
        grid = np.arange(np.floor(t) - 60, np.floor(t) + 1.0)
        fa = strategy_features(strategy, tape_a, grid, scaler)[-1]
        fb = strategy_features(strategy, alt, grid, scaler)[-1]
        ma = env.mids(tape_a)[np.searchsorted(tape_a["t"], grid[-1], side="right") - 1]
        mb = env.mids(alt)[np.searchsorted(alt["t"], grid[-1], side="right") - 1]
        changed = ma != mb
        for inv in (-5.0, 0.0, 5.0):
            env.quotes(strategy["kind"], np.asarray(strategy["params"], float), fa, inv, ma, b1, a1)
            env.quotes(strategy["kind"], np.asarray(strategy["params"], float), fb, inv, mb, b2, a2)
            changed |= not (np.array_equal(b1, b2, equal_nan=True) and np.array_equal(a1, a2, equal_nan=True))
        differ += bool(changed)
    return {"moments": n, "differing": differ, "verdict": "clean" if differ == 0 else "reads the future"}


def strategy_features(strategy, tp, grid, scaler):
    if strategy["kind"] == env.LINEAR:
        return rl.scaled_features(tp, grid, scaler, leak=strategy.get("leak", False))
    return env.features(tp, grid, leak=strategy.get("leak", False))
