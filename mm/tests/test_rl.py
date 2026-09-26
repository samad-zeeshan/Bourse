"""
Evolution strategies must improve the training objective, and the cheat's quotes must depend on the future.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import env  # noqa: E402
import rl  # noqa: E402
import tape  # noqa: E402

CAL = {"sigma_per_sec_ticks": 10.0, "impact_eta": 0.5, "reach_ticks": list(np.linspace(0, 6, 1001)),
       "size_eth": list(np.linspace(0.01, 2, 1001)),
       "hawkes": {"mu": [1.0, 1.0], "alpha": [[[0.2, 0.1], [0.0, 0.0]], [[0.0, 0.0], [0.2, 0.1]]], "beta": [20.0, 1.0]}}
SCALER = (np.zeros(5), np.ones(5))


def test_training_beats_the_starting_policy_on_its_own_objective():
    ck = rl.train(CAL, SCALER, iters=60, seed=1)
    held = [tape.synthetic(CAL, rl.EPISODE_S, 900 + i) for i in range(16)]
    assert len(ck) == 6
    assert rl.score(ck[-1]["theta"], held, SCALER) > rl.score(np.zeros(rl.N_PARAMS), held, SCALER)


def test_the_cheat_quotes_differently_when_only_the_future_changes():
    b1, a1, b2, a2 = (np.empty(3) for _ in range(4))
    f = np.zeros(5)
    env.quotes(env.LINEAR, rl.leaky_params(), f, 0.0, 1000.5, b1, a1)
    f[4] = 10.0
    env.quotes(env.LINEAR, rl.leaky_params(), f, 0.0, 1000.5, b2, a2)
    assert b2[0] > b1[0] and a2[0] > a1[0]
