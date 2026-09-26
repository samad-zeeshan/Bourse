"""
Fill rule, accounting and the past-only promise of the features.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import env  # noqa: E402


def tape(rows):
    t, side, qty, first, last = map(np.array, zip(*rows))
    bid = np.where(side > 0, last - 1, last)
    ask = np.where(side > 0, last, last + 1)
    return {"t": t.astype(float), "side": side.astype(np.int64), "qty": qty.astype(float), "first": first.astype(np.int64),
            "last": last.astype(np.int64), "bid": bid.astype(np.int64), "ask": ask.astype(np.int64)}


def test_a_quote_fills_only_when_an_order_trades_through_it():
    # The touch before the buy is 100 / 101, so the mid is 100.5 and two asks sit at 102 and 103.
    tp = tape([(0.5, 1, 1.0, 101, 103)])
    r = env.episode(tp, env.MULTI, [1.5, 1.0, 2, 0.0], size=0.1, qmax=5.0, seconds=1.0)
    # The buy stops at 103, so 102 is traded through and fills. 103 is only reached, and does not.
    assert r["fills"] == 1
    assert abs(r["inv"][-1] + 0.1) < 1e-12


def test_pnl_is_cash_plus_marked_inventory_minus_the_exit_cost():
    # Touch 100 / 101 before the sell. The bid sits at 100 and the sell walks down to 97.
    tp = tape([(0.5, -1, 1.0, 100, 97)])
    r = env.episode(tp, env.MULTI, [0.5, 1.0, 1, 0.0], size=0.1, qmax=5.0, seconds=1.0)
    assert r["fills"] == 1
    expected = (-0.1 * 100 + 0.1 * 97.5 - 0.1 * 0.5) * env.TICK_USDT
    assert abs(r["pnl"] - expected) < 1e-12


def test_features_do_not_move_when_the_future_changes():
    rng = np.random.default_rng(1)
    n = 5000
    t = np.sort(rng.uniform(0, 1000, n))
    side = rng.choice([-1, 1], n)
    last = 100000 + np.cumsum(side * rng.integers(0, 3, n))
    base = {"t": t, "side": side, "qty": rng.exponential(1, n), "first": last, "last": last, "bid": last - 1, "ask": last}
    grid = np.arange(0, 1000.0)
    F = env.features(base, grid)
    cut = np.searchsorted(t, 500.0, side="right")
    alt = {k: v.copy() for k, v in base.items()}
    alt["last"][cut:] += 777
    alt["bid"][cut:] += 777
    alt["ask"][cut:] += 777
    alt["qty"][cut:] *= 3
    G = env.features(alt, grid)
    assert np.array_equal(F[:501], G[:501])
    L1, L2 = env.features(base, grid, leak=True), env.features(alt, grid, leak=True)
    assert not np.array_equal(L1[:501, 4], L2[:501, 4])
