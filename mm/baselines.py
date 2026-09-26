"""
The two baseline families and the grids they are tuned over: Avellaneda-Stoikov and multi-level quoting.

Grids are fixed before any test day is looked at, and every point tried goes into the register.
"""

import itertools

import numpy as np

import env


def fill_decay(cal):
    """AS's k: the rate at which the chance of a market order reaching a quote falls per tick of distance."""
    reach = np.asarray(cal["reach_ticks"])
    d = np.arange(1, 6)
    p = np.array([(reach >= x).mean() for x in d])
    p = np.clip(p, 1e-6, 1)
    return float(-np.polyfit(d, np.log(p), 1)[0])


def as_grid(cal):
    sig2 = cal["sigma_per_sec_ticks"] ** 2
    k0 = fill_decay(cal)
    out = []
    for gamma, kmul, tau in itertools.product([1e-4, 3e-4, 1e-3, 3e-3, 1e-2], [0.5, 1.0, 2.0], [10.0, 60.0]):
        out.append({"family": "avellaneda_stoikov", "kind": env.AS,
                    "params": [gamma, k0 * kmul, tau, sig2],
                    "label": f"gamma={gamma:g} k={k0 * kmul:.3f} tau={tau:g}"})
    return out


def multi_grid(cal):
    out = []
    for d0, step, levels, skew in itertools.product([0.5, 1.5, 3.0, 6.0, 12.0, 24.0], [1.0, 3.0], [2, 3], [0.5, 2.0]):
        out.append({"family": "multi_level", "kind": env.MULTI, "params": [d0, step, levels, skew],
                    "label": f"d0={d0:g} step={step:g} levels={levels} skew={skew:g}"})
    return out
