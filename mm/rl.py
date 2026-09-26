"""
Train a linear quoting policy by evolution strategies against an adversary that picks the worst market regime.

Training sees only synthetic tapes calibrated on the training days. Real days are used later, for selection and testing.
"""

import numpy as np

import env
import tape

# Eight regimes the adversary chooses between each iteration: nominal, trends both ways, stronger and weaker
# clustering, stronger impact, more noise, and a trend with strong impact together.
REGIMES = [
    {},
    {"drift": 0.2}, {"drift": -0.2},
    {"clustering": 1.25}, {"clustering": 0.75},
    {"impact": 2.0}, {"noise": 1.4},
    {"drift": 0.2, "impact": 2.0},
]
N_PARAMS = 13
# Real parameter = BASE + SCALE * theta, so one unit of theta is a sensible step for every entry.
BASE = np.array([2.0, 0, 0, 0, 0, 0, 2.0, 0, 0, 0, 0, 0, 0.5])
SCALE = np.array([3.0, 1, 1, 1, 1, 0, 3.0, 1, 1, 1, 1, 0, 1.0])
EPISODE_S = 900.0
INV_PENALTY = 0.5


def feature_scaler(days):
    """Means and spreads of the features on the training days' real tapes, so weights are in z units."""
    rows = []
    for d in days:
        tp = tape.real(d)
        grid = np.arange(0.0, 86400.0, 5.0)
        rows.append(env.features(tp, grid))
    F = np.concatenate(rows)
    mu, sd = F.mean(axis=0), F.std(axis=0)
    mu[4], sd[4] = 0.0, 1.0
    sd[sd == 0] = 1.0
    return mu, sd


def to_params(theta):
    return BASE + SCALE * np.asarray(theta)


def scaled_features(tp, grid, scaler, leak=False):
    mu, sd = scaler
    F = env.features(tp, grid, leak=leak)
    # The future column, when filled, stays in raw ticks.
    F[:, :4] = (F[:, :4] - mu[:4]) / sd[:4]
    return F


def score(theta, tapes, scaler):
    p = to_params(theta)
    tot = 0.0
    for tp in tapes:
        grid = np.arange(0.0, EPISODE_S + 1e-9, 1.0)
        r = env.episode(tp, env.LINEAR, p, seconds=EPISODE_S, F=scaled_features(tp, grid, scaler))
        tot += r["pnl"] - INV_PENALTY * r["mean_abs_inv"]
    return tot / len(tapes)


def train(cal, scaler, iters=200, pop=16, sigma=0.3, lr=0.05, episodes=8, seed=0, log=None):
    """Returns the checkpoints: theta every ten iterations and at the end, with the regime the adversary chose."""
    rng = np.random.default_rng(seed)
    theta = np.zeros(N_PARAMS)
    m, v = np.zeros(N_PARAMS), np.zeros(N_PARAMS)
    checkpoints = []
    for it in range(iters):
        seeds = rng.integers(0, 2**31, episodes)
        banks = [[tape.synthetic(cal, EPISODE_S, int(s), reg) for s in seeds] for reg in REGIMES]
        # The adversary's best response to the current policy, on the same random draws.
        worst = int(np.argmin([score(theta, b, scaler) for b in banks]))
        mix = banks[0] + banks[worst]
        eps = rng.normal(size=(pop, N_PARAMS))
        eps[:, SCALE == 0] = 0.0
        f = np.array([[score(theta + sigma * e, mix, scaler), score(theta - sigma * e, mix, scaler)] for e in eps])
        # Rank-normalised fitness makes the step size independent of how large PnL happens to be.
        ranks = np.argsort(np.argsort(f.ravel())).reshape(f.shape) / (f.size - 1) - 0.5
        g = ((ranks[:, 0] - ranks[:, 1])[:, None] * eps).sum(axis=0) / (pop * sigma)
        m = 0.9 * m + 0.1 * g
        v = 0.999 * v + 0.001 * g * g
        theta = theta + lr * (m / (1 - 0.9 ** (it + 1))) / (np.sqrt(v / (1 - 0.999 ** (it + 1))) + 1e-8)
        if log:
            log(f"iter {it} worst regime {worst} fitness {f.mean():.3f}")
        if (it + 1) % 10 == 0 or it == iters - 1:
            checkpoints.append({"iter": it + 1, "theta": theta.tolist(), "worst_regime": REGIMES[worst]})
    return checkpoints


def leaky_params():
    """The planted cheat: fixed quote parameters that lean toward where the price is about to go.

    Bid offset shrinks and ask offset grows when the next five seconds go up, and the reverse when they go
    down. It is only ever run with the future column filled, and the leakage check must refuse it.
    """
    p = BASE.copy()
    p[5], p[11] = -1.0, 1.0
    return p
