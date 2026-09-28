"""
Statistics for judging a strategy: block bootstrap, deflated Sharpe, overfitting probability, SPA, MinTRL and a betting referee.

The robustness grade follows the recipe in arXiv 2608.23808 as its abstract describes it. The paper's own
thresholds and calibration are not reproduced, so grades here are not comparable with its published ones.
"""

import numpy as np
from scipy import stats

EULER = 0.5772156649


def sharpe(x):
    x = np.asarray(x, dtype=float)
    s = x.std(ddof=1)
    return float(x.mean() / s) if s > 0 else 0.0


def block_indices(n, block, rng):
    starts = rng.integers(0, n, int(np.ceil(n / block)))
    idx = (starts[:, None] + np.arange(block)[None, :]).ravel() % n
    return idx[:n]


def bootstrap(fn, arrays, block=4, reps=5000, seed=0):
    """Moving block bootstrap with wraparound. Returns the estimate and a 95% interval.

    Blocks of four hourly episodes keep the within-day correlation that plain resampling would break.
    """
    rng = np.random.default_rng(seed)
    arrays = [np.asarray(a, dtype=float) for a in arrays]
    n = len(arrays[0])
    est = fn(*arrays)
    draws = np.empty(reps)
    for r in range(reps):
        idx = block_indices(n, block, rng)
        draws[r] = fn(*[a[idx] for a in arrays])
    return float(est), [float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))]


def psr(x, sr_star):
    """Probabilistic Sharpe ratio: chance the true Sharpe exceeds sr_star, allowing for skew and fat tails."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    sr = sharpe(x)
    g3 = stats.skew(x)
    g4 = stats.kurtosis(x, fisher=False)
    den = np.sqrt(max(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2, 1e-12))
    return float(stats.norm.cdf((sr - sr_star) * np.sqrt(n - 1) / den))


def deflated_sharpe(x, trial_sharpes):
    """PSR against the best Sharpe expected from luck alone, given how many trials were run and how they spread."""
    n_trials = max(len(trial_sharpes), 2)
    v = float(np.var(trial_sharpes, ddof=1)) if len(trial_sharpes) > 1 else 0.0
    z = (1 - EULER) * stats.norm.ppf(1 - 1 / n_trials) + EULER * stats.norm.ppf(1 - 1 / (n_trials * np.e))
    sr0 = np.sqrt(v) * z
    return psr(x, sr0), float(sr0)


def pbo(M, blocks=8):
    """Probability of backtest overfitting by combinatorially symmetric cross-validation.

    M is configurations by periods. For every way to split the period blocks in half, the best configuration
    in one half is ranked in the other. PBO is how often it lands in the bottom half.
    """
    from itertools import combinations

    M = np.asarray(M, dtype=float)
    n_cfg, T = M.shape
    if n_cfg < 2:
        return None
    parts = np.array_split(np.arange(T), blocks)
    below = total = 0
    for chosen in combinations(range(blocks), blocks // 2):
        ins = np.concatenate([parts[i] for i in chosen])
        oos = np.concatenate([parts[i] for i in range(blocks) if i not in chosen])
        best = np.argmax([sharpe(M[c, ins]) for c in range(n_cfg)])
        perf = np.array([sharpe(M[c, oos]) for c in range(n_cfg)])
        rank = (perf < perf[best]).sum() / (n_cfg - 1)
        below += rank < 0.5
        total += 1
    return below / total


def stationary_indices(n, mean_block, rng):
    idx = np.empty(n, dtype=int)
    idx[0] = rng.integers(n)
    for i in range(1, n):
        idx[i] = rng.integers(n) if rng.random() < 1 / mean_block else (idx[i - 1] + 1) % n
    return idx


def spa(losses_vs_bench, reps=2000, mean_block=4, seed=0):
    """Hansen's SPA test, consistent version. Rows are models, columns periods, entries model minus benchmark.

    Small p means at least one model beats the benchmark after allowing for having tried all of them.
    """
    d = np.asarray(losses_vs_bench, dtype=float)
    k, n = d.shape
    rng = np.random.default_rng(seed)
    mean = d.mean(axis=1)
    boots = np.empty((reps, k))
    for r in range(reps):
        boots[r] = d[:, stationary_indices(n, mean_block, rng)].mean(axis=1)
    omega = np.sqrt(n) * boots.std(axis=0) + 1e-12
    t = max(float(np.max(np.sqrt(n) * mean / omega)), 0.0)
    # Models far below the benchmark are recentred at zero only if they could plausibly be at zero.
    keep = np.sqrt(n) * mean / omega >= -np.sqrt(2 * np.log(np.log(n)))
    mu_c = np.where(keep, mean, 0.0)
    tb = np.max(np.sqrt(n) * (boots - mu_c) / omega, axis=1)
    return float(np.mean(np.maximum(tb, 0) >= t)), t


def min_trl(x, sr_star=0.0, conf=0.95):
    x = np.asarray(x, dtype=float)
    sr = sharpe(x)
    if sr <= sr_star:
        return float("inf")
    g3, g4 = stats.skew(x), stats.kurtosis(x, fisher=False)
    z = stats.norm.ppf(conf)
    return float(1 + (1 - g3 * sr + (g4 - 1) / 4 * sr ** 2) * (z / (sr - sr_star)) ** 2)


def eprocess(x, scale):
    """A betting e-process for H0: mean of x is at most zero. Valid at every stopping time.

    Each outcome is scaled into [-1, 1] and the bet size is chosen from past outcomes only, by online
    Newton steps, so looking at the running value never inflates the error rate. Returns the path.
    """
    x = np.clip(np.asarray(x, dtype=float) / scale, -1, 1)
    wealth = 1.0
    lam, a = 0.0, 1.0
    path = []
    for v in x:
        wealth *= 1 + lam * v
        path.append(wealth)
        # Online Newton step on the log-wealth loss, bets kept within [0, 1/2] so wealth stays positive.
        g = -v / (1 + lam * v)
        a += g * g
        lam = float(np.clip(lam - 2 / (2 - np.log(3)) * g / a, 0.0, 0.5))
    return np.array(path)


def minerva(components):
    """Signed margins from each gate, averaged, mapped to 0-100. 80 or more only when all five gates pass."""
    margins = [c["margin"] for c in components.values()]
    raw = float(np.mean([np.clip(m, -1, 1) for m in margins]))
    score = 50 + 50 * raw
    seal = all(c["pass"] for c in components.values())
    if not seal:
        score = min(score, 79.0)
    return {"score": round(float(np.clip(score, 0, 100)), 1), "raw": raw, "seal": seal}
