"""
Fit a two-dimensional Hawkes process to market order arrivals and simulate from it.

Dimension 0 is buyer-initiated, 1 is seller-initiated. The kernel is a sum of exponentials with shared decay rates.
"""

import numpy as np
from numba import njit
from scipy import optimize, stats


@njit(cache=True)
def _loglik(t, d, T, mu, alpha, beta):
    # alpha[i, j, k]: expected children in dim i from one event in dim j through decay beta[k].
    n_dim, n_k = mu.shape[0], beta.shape[0]
    S = np.zeros((n_dim, n_k))
    ll = 0.0
    last = 0.0
    for n in range(t.shape[0]):
        dt = t[n] - last
        for j in range(n_dim):
            for k in range(n_k):
                S[j, k] *= np.exp(-beta[k] * dt)
        i = d[n]
        lam = mu[i]
        for j in range(n_dim):
            for k in range(n_k):
                lam += alpha[i, j, k] * beta[k] * S[j, k]
        ll += np.log(lam)
        S[i, :] += 1.0
        last = t[n]
    comp = 0.0
    for i in range(n_dim):
        comp += mu[i] * T
    for n in range(t.shape[0]):
        j = d[n]
        for i in range(n_dim):
            for k in range(n_k):
                comp += alpha[i, j, k] * (1.0 - np.exp(-beta[k] * (T - t[n])))
    return ll - comp


@njit(cache=True)
def _residuals(t, d, mu, alpha, beta):
    """Compensator increments between consecutive events of each dimension. Exp(1) if the model is right."""
    n_dim, n_k = mu.shape[0], beta.shape[0]
    S = np.zeros((n_dim, n_k))
    acc = np.zeros(n_dim)
    out = np.empty(t.shape[0])
    last = 0.0
    for n in range(t.shape[0]):
        dt = t[n] - last
        for i in range(n_dim):
            inc = mu[i] * dt
            for j in range(n_dim):
                for k in range(n_k):
                    inc += alpha[i, j, k] * S[j, k] * (1.0 - np.exp(-beta[k] * dt))
            acc[i] += inc
        for j in range(n_dim):
            for k in range(n_k):
                S[j, k] *= np.exp(-beta[k] * dt)
        i = d[n]
        out[n] = acc[i]
        acc[i] = 0.0
        S[i, :] += 1.0
        last = t[n]
    return out


@njit(cache=True)
def _simulate(T, mu, alpha, beta, seed):
    # Ogata thinning. Between events the intensity only decays, so its value right after the last
    # event is a valid upper bound until the next candidate.
    np.random.seed(seed)
    n_dim, n_k = mu.shape[0], beta.shape[0]
    S = np.zeros((n_dim, n_k))
    ts = []
    ds = []
    t = 0.0
    while True:
        lam_bar = 0.0
        for i in range(n_dim):
            lam_bar += mu[i]
            for j in range(n_dim):
                for k in range(n_k):
                    lam_bar += alpha[i, j, k] * beta[k] * S[j, k]
        w = -np.log(np.random.random()) / lam_bar
        t += w
        if t > T:
            break
        for j in range(n_dim):
            for k in range(n_k):
                S[j, k] *= np.exp(-beta[k] * w)
        lams = np.empty(n_dim)
        tot = 0.0
        for i in range(n_dim):
            lam = mu[i]
            for j in range(n_dim):
                for k in range(n_k):
                    lam += alpha[i, j, k] * beta[k] * S[j, k]
            lams[i] = lam
            tot += lam
        u = np.random.random() * lam_bar
        if u <= tot:
            i = 0
            acc = lams[0]
            while u > acc and i < n_dim - 1:
                i += 1
                acc += lams[i]
            ts.append(t)
            ds.append(i)
            S[i, :] += 1.0
    return np.array(ts), np.array(ds)


def unpack(x, n_dim, n_k):
    mu = np.exp(x[:n_dim])
    alpha = np.exp(x[n_dim:n_dim + n_dim * n_dim * n_k]).reshape(n_dim, n_dim, n_k)
    beta = np.exp(x[n_dim + n_dim * n_dim * n_k:])
    return mu, alpha, beta


def branching_ratio(alpha):
    return float(np.max(np.abs(np.linalg.eigvals(alpha.sum(axis=2)))))


def fit(t, d, T, n_k=2, n_dim=2):
    """Maximum likelihood with log parameters. Starts with one fast and one slow decay."""
    t = np.ascontiguousarray(t, dtype=np.float64)
    d = np.ascontiguousarray(d, dtype=np.int64)
    rate = np.array([(d == i).sum() / T for i in range(n_dim)])
    betas0 = np.array([20.0, 0.5][:n_k] if n_k <= 2 else np.geomspace(50, 0.1, n_k))
    x0 = np.concatenate([np.log(rate * 0.4), np.log(np.full(n_dim * n_dim * n_k, 0.6 / (n_dim * n_k))), np.log(betas0)])

    def nll(x):
        mu, alpha, beta = unpack(x, n_dim, n_k)
        if branching_ratio(alpha) >= 0.999:
            return 1e18
        return -_loglik(t, d, T, mu, alpha, beta)

    r = optimize.minimize(nll, x0, method="L-BFGS-B", options={"maxiter": 500})
    # A second pass from Nelder-Mead's answer guards against L-BFGS-B stopping on a flat numerical gradient.
    r2 = optimize.minimize(nll, r.x, method="Nelder-Mead", options={"maxiter": 4000, "xatol": 1e-6, "fatol": 1e-3})
    best = r2 if r2.fun < r.fun else r
    mu, alpha, beta = unpack(best.x, n_dim, n_k)
    return {"mu": mu, "alpha": alpha, "beta": beta, "loglik": -best.fun, "n": len(t), "T": T}


def loglik(p, t, d, T):
    return _loglik(np.ascontiguousarray(t, dtype=np.float64), np.ascontiguousarray(d, dtype=np.int64), T,
                   p["mu"], p["alpha"], p["beta"])


def goodness(p, t, d):
    """Time-rescaling check per dimension: KS distance of the residuals from Exp(1), plus QQ points."""
    r = _residuals(np.ascontiguousarray(t, dtype=np.float64), np.ascontiguousarray(d, dtype=np.int64),
                   p["mu"], p["alpha"], p["beta"])
    out = []
    for i in range(len(p["mu"])):
        ri = r[d == i]
        ks = stats.kstest(ri, "expon")
        q = np.linspace(0.005, 0.995, 60)
        out.append({"ks": float(ks.statistic), "p": float(ks.pvalue), "mean": float(ri.mean()),
                    "qq_model": [float(v) for v in stats.expon.ppf(q)],
                    "qq_data": [float(v) for v in np.quantile(ri, q)]})
    return out


def poisson(t, d, T, n_dim=2):
    """The no-clustering baseline: constant rates, fitted exactly."""
    mu = np.array([(d == i).sum() / T for i in range(n_dim)])
    ll = float(sum(((d == i).sum() * np.log(mu[i]) - mu[i] * T) for i in range(n_dim)))
    return {"mu": mu, "alpha": np.zeros((n_dim, n_dim, 1)), "beta": np.ones(1), "loglik": ll}


def simulate(p, T, seed):
    return _simulate(float(T), p["mu"], p["alpha"], p["beta"], int(seed))


def to_json(p):
    return {"mu": p["mu"].tolist(), "alpha": p["alpha"].tolist(), "beta": p["beta"].tolist(),
            "branching_ratio": branching_ratio(p["alpha"]), "loglik": float(p["loglik"])}


def from_json(j):
    return {"mu": np.array(j["mu"]), "alpha": np.array(j["alpha"]), "beta": np.array(j["beta"]), "loglik": j.get("loglik", 0.0)}
