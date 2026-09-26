"""
The market making environment: past-only features, a quote rule per strategy family, and the fill loop.

Quotes are set once a second and cannot move inside the second. A quote fills only when a market order trades
strictly through its price, which treats the quote as last in the queue at its own price.
"""

import numpy as np
from numba import njit

N_FEAT = 5
FEATURES = ["return_10s", "imbalance_10s", "intensity_10s", "noise_60s", "future_5s"]
AS, MULTI, LINEAR = 0, 1, 2
TICK_USDT = 0.01


def mids(tape):
    return (tape["bid"] + tape["ask"]) / 2.0


def features(tape, grid, leak=False):
    """One row per quote time. Every column but the last uses only orders at or before that time.

    The last column is the mid five seconds ahead. It stays zero unless `leak` is set, which exists only
    to build the planted cheat that the leakage check has to catch.
    """
    t, m = tape["t"], mids(tape)
    idx = np.searchsorted(t, grid, side="right")
    m0 = m[0] if len(m) else 0.0

    def mid_at(g):
        i = np.searchsorted(t, g, side="right")
        return np.where(i > 0, m[np.maximum(i - 1, 0)], m0)

    csv = np.concatenate([[0.0], np.cumsum(tape["side"] * tape["qty"])])
    cv = np.concatenate([[0.0], np.cumsum(tape["qty"])])
    i10 = np.searchsorted(t, grid - 10, side="right")
    i600 = np.searchsorted(t, grid - 600, side="right")
    F = np.zeros((len(grid), N_FEAT))
    F[:, 0] = mid_at(grid) - mid_at(grid - 10)
    F[:, 1] = (csv[idx] - csv[i10]) / (cv[idx] - cv[i10] + 1e-9)
    n10 = idx - i10
    rate = (idx - i600) / np.minimum(np.maximum(grid, 1.0), 600.0)
    F[:, 2] = np.log((n10 + 1.0) / (rate * 10 + 1.0))
    per_sec = mid_at(grid)
    d = np.abs(np.diff(per_sec, prepend=per_sec[0]))
    c = np.concatenate([[0.0], np.cumsum(d)])
    j = np.arange(len(grid))
    F[:, 3] = (c[j + 1] - c[np.maximum(j + 1 - 60, 0)]) / np.minimum(j + 1, 60)
    if leak:
        F[:, 4] = mid_at(grid + 5) - mid_at(grid)
    return F


@njit(cache=True)
def quotes(kind, p, f, q_units, mid, out_bid, out_ask):
    """Fill up to three bid and ask prices in ticks. NaN means no quote at that level."""
    for i in range(3):
        out_bid[i] = np.nan
        out_ask[i] = np.nan
    if kind == AS:
        gamma, k, tau, sig2 = p[0], p[1], p[2], p[3]
        r = mid - q_units * gamma * sig2 * tau
        half = 0.5 * gamma * sig2 * tau + np.log(1.0 + gamma / k) / gamma
        out_bid[0] = np.floor(r - half)
        out_ask[0] = np.ceil(r + half)
    elif kind == MULTI:
        d0, step, levels, skew = p[0], p[1], int(p[2]), p[3]
        for i in range(levels):
            out_bid[i] = np.floor(mid - d0 - i * step - skew * q_units)
            out_ask[i] = np.ceil(mid + d0 + i * step - skew * q_units)
    else:
        # Linear policy: p = [b0, wb(5), a0, wa(5), inventory weight].
        db = p[0]
        da = p[6]
        for j in range(5):
            db += p[1 + j] * f[j]
            da += p[7 + j] * f[j]
        db += p[12] * q_units
        da -= p[12] * q_units
        db = min(max(db, 0.5), 60.0)
        da = min(max(da, 0.5), 60.0)
        out_bid[0] = np.floor(mid - db)
        out_ask[0] = np.ceil(mid + da)
    # Never quote through the other side of our own quotes.
    for i in range(3):
        if not np.isnan(out_bid[i]) and not np.isnan(out_ask[0]) and out_bid[i] >= out_ask[0]:
            out_bid[i] = out_ask[0] - 1


@njit(cache=True)
def run(t, side, qty, last, mid_ev, grid, F, kind, p, size, qmax, fee_bps, mid0):
    """Returns per-step mark-to-market PnL in USDT, inventory after each step, fill count and traded volume."""
    n_steps = len(grid) - 1
    pnl = np.zeros(n_steps)
    inv_path = np.zeros(n_steps)
    bid = np.empty(3)
    ask = np.empty(3)
    rem_b = np.empty(3)
    rem_a = np.empty(3)
    cash = 0.0
    inv = 0.0
    fills = 0
    volume = 0.0
    e = 0
    n = len(t)
    while e < n and t[e] <= grid[0]:
        e += 1
    mid = mid0 if e == 0 else mid_ev[e - 1]
    value = 0.0
    for s in range(n_steps):
        quotes(kind, p, F[s], inv / size, mid, bid, ask)
        for i in range(3):
            rem_b[i] = size if (not np.isnan(bid[i]) and inv < qmax - 1e-12) else 0.0
            rem_a[i] = size if (not np.isnan(ask[i]) and inv > -qmax + 1e-12) else 0.0
        end = grid[s + 1]
        while e < n and t[e] <= end:
            left = qty[e]
            if side[e] > 0:
                for i in range(3):
                    if rem_a[i] > 0 and ask[i] < last[e] and left > 0:
                        f = min(rem_a[i], left, inv + qmax)
                        if f > 0:
                            cash += f * ask[i] * TICK_USDT * (1 - fee_bps * 1e-4)
                            inv -= f
                            rem_a[i] -= f
                            left -= f
                            fills += 1
                            volume += f
            else:
                for i in range(3):
                    if rem_b[i] > 0 and bid[i] > last[e] and left > 0:
                        f = min(rem_b[i], left, qmax - inv)
                        if f > 0:
                            cash -= f * bid[i] * TICK_USDT * (1 + fee_bps * 1e-4)
                            inv += f
                            rem_b[i] -= f
                            left -= f
                            fills += 1
                            volume += f
            mid = mid_ev[e]
            e += 1
        v = cash + inv * mid * TICK_USDT
        if s == n_steps - 1:
            # Flatten at the end: cross half a tick for every unit left.
            v -= abs(inv) * 0.5 * TICK_USDT
        pnl[s] = v - value
        value = v
        inv_path[s] = inv
    return pnl, inv_path, fills, volume


def episode(tape, kind, params, size=0.1, qmax=1.0, fee_bps=0.0, leak=False, seconds=None, F=None):
    if seconds is None:
        seconds = float(np.ceil(tape["t"][-1])) if len(tape["t"]) else 0.0
    grid = np.arange(0.0, seconds + 1e-9, 1.0)
    if F is None:
        F = features(tape, grid, leak=leak)
    m = mids(tape)
    # The touch before the first order, from that order's own first price. Using m[0] here would quote
    # the first second off a price that has not happened yet.
    mid0 = float(tape["first"][0] - 0.5 * tape["side"][0]) if len(m) else 0.0
    pnl, inv, fills, vol = run(tape["t"], tape["side"], tape["qty"], tape["last"].astype(np.float64), m, grid, F, kind,
                               np.asarray(params, dtype=np.float64), size, qmax, fee_bps, mid0)
    return {"pnl": float(pnl.sum()), "step_pnl": pnl, "inv": inv, "fills": int(fills), "volume": float(vol),
            "mean_abs_inv": float(np.mean(np.abs(inv))), "max_abs_inv": float(np.max(np.abs(inv)))}
