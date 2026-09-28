"""
The leakage guard must pass honest strategies and refuse the one that reads the future.
"""

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "mm"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import env  # noqa: E402
import leakage  # noqa: E402
import rl  # noqa: E402
import tape  # noqa: E402

CAL = {"sigma_per_sec_ticks": 10.0, "impact_eta": 0.5, "reach_ticks": list(np.linspace(0, 6, 1001)),
       "size_eth": list(np.linspace(0.01, 2, 1001)),
       "hawkes": {"mu": [1.0, 1.0], "alpha": [[[0.2, 0.1], [0.0, 0.0]], [[0.0, 0.0], [0.2, 0.1]]], "beta": [20.0, 1.0]}}
SCALER = (np.zeros(5), np.ones(5))


def test_honest_strategies_are_clean_and_the_cheat_is_refused():
    a, b = tape.synthetic(CAL, 4000, 1), tape.synthetic(CAL, 4000, 2)
    honest = [{"kind": env.AS, "params": [1e-3, 0.2, 10, 100]}, {"kind": env.MULTI, "params": [1.5, 1, 3, 0.5]},
              {"kind": env.LINEAR, "params": rl.to_params(np.full(rl.N_PARAMS, 0.3)).tolist()}]
    for s in honest:
        assert leakage.check(s, a, b, SCALER, n=30)["verdict"] == "clean"
    cheat = {"kind": env.LINEAR, "params": rl.leaky_params().tolist(), "leak": True}
    assert leakage.check(cheat, a, b, SCALER, n=30)["differing"] > 20


def test_the_splice_keeps_the_past_and_joins_prices():
    a, b = tape.synthetic(CAL, 2000, 3), tape.synthetic(CAL, 2000, 4)
    s = leakage.splice(a, b, 1000.0)
    i = np.searchsorted(a["t"], 1000.0, side="right")
    assert np.array_equal(s["last"][:i], a["last"][:i])
    assert s["last"][i] == a["last"][i - 1]
