"""
Checks on the statistics where the right answer is known.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stats as st  # noqa: E402


def test_the_referee_rarely_admits_noise_and_admits_a_real_edge():
    rng = np.random.default_rng(0)
    false = sum(st.eprocess(rng.normal(0, 1, 200), 4).max() >= 20 for _ in range(300))
    # Ville's inequality bounds this at 1 in 20 for any stopping rule. Allow sampling slack above 15 of 300.
    assert false <= 20
    assert st.eprocess(rng.normal(0.5, 1, 200), 4).max() > 1000


def test_pbo_is_high_when_every_configuration_is_noise():
    rng = np.random.default_rng(1)
    # One draw swings between 0.2 and 0.8 because the splits overlap, so check the average.
    assert abs(np.mean([st.pbo(rng.normal(0, 1, (30, 96))) for _ in range(20)]) - 0.5) < 0.1
    good = rng.normal(0, 1, (30, 96))
    good[0] += 1.0
    assert st.pbo(good) < 0.1


def test_deflation_grows_with_the_number_of_trials():
    rng = np.random.default_rng(2)
    x = rng.normal(0.2, 1, 96)
    few = st.deflated_sharpe(x, rng.normal(0, 0.1, 3))[0]
    many = st.deflated_sharpe(x, rng.normal(0, 0.1, 300))[0]
    assert many < few


def test_spa_rejects_only_when_a_model_is_better():
    rng = np.random.default_rng(3)
    assert st.spa(rng.normal(0, 1, (10, 96)))[0] > 0.05
    d = rng.normal(0, 1, (10, 96))
    d[3] += 0.8
    assert st.spa(d)[0] < 0.01


def test_the_grade_is_capped_without_every_gate():
    comps = {k: {"margin": 1.0, "pass": True} for k in "abcde"}
    assert st.minerva(comps)["score"] == 100
    comps["e"] = {"margin": 1.0, "pass": False}
    assert st.minerva(comps)["score"] == 79
