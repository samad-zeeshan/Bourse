"""
The fitter must recover known parameters from a simulated path, and the residual test must accept a correct model.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hawkes  # noqa: E402

TRUE = {"mu": np.array([0.8, 0.6]),
        "alpha": np.array([[[0.25, 0.15], [0.05, 0.05]], [[0.05, 0.05], [0.3, 0.1]]]),
        "beta": np.array([15.0, 0.4]), "loglik": 0.0}


def test_fit_recovers_simulated_parameters():
    t, d = hawkes.simulate(TRUE, 20000.0, seed=3)
    p = hawkes.fit(t, d, 20000.0)
    assert np.allclose(p["mu"], TRUE["mu"], rtol=0.2)
    assert abs(hawkes.branching_ratio(p["alpha"]) - hawkes.branching_ratio(TRUE["alpha"])) < 0.06
    assert np.allclose(np.sort(p["beta"]), np.sort(TRUE["beta"]), rtol=0.35)
    assert p["loglik"] > hawkes.poisson(t, d, 20000.0)["loglik"]


def test_residuals_of_the_true_model_look_exponential():
    t, d = hawkes.simulate(TRUE, 5000.0, seed=4)
    for g in hawkes.goodness(TRUE, t, d):
        assert g["p"] > 0.01 and abs(g["mean"] - 1) < 0.05
