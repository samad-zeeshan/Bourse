"""
Walk-forward evaluation of every market maker, written to assume each result is luck or leakage until shown otherwise.

Each fold tunes and trains on earlier days only, selects on the last of them, and tests once on the next day.
Every configuration evaluated anywhere is appended to eval/mm/register.jsonl. Results go to eval/mm/results.json.
"""

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mm"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import baselines  # noqa: E402
import env  # noqa: E402
import leakage  # noqa: E402
import rl  # noqa: E402
import stats as st  # noqa: E402
import tape  # noqa: E402

OUT = ROOT / "eval" / "mm"
FIRST_TEST = 3
HOURS = 24
FEE_SENSITIVITY_BPS = 1.0
NAMES = {"avellaneda_stoikov": "Avellaneda-Stoikov", "multi_level": "Multi-level", "rl_adversarial": "RL candidate",
         "leaky_oracle": "Planted cheat"}


class Register:
    def __init__(self, path):
        self.path = path
        self.path.write_text("")
        self.n = 0

    def add(self, **row):
        self.n += 1
        row = {"n": self.n, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), **row}
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")


def hourly(day_tape, strat, scaler, fee_bps=0.0):
    """One episode per UTC hour of a day. Returns per-hour PnL, mean absolute inventory and fills."""
    rows = []
    for h in range(HOURS):
        w = tape.window(day_tape, h * 3600, (h + 1) * 3600)
        grid = np.arange(0.0, 3601.0)
        F = leakage.strategy_features(strat, w, grid, scaler)
        r = env.episode(w, strat["kind"], strat["params"], seconds=3600.0, F=F, fee_bps=fee_bps)
        rows.append((r["pnl"], r["mean_abs_inv"], r["fills"]))
    a = np.array(rows)
    return a[:, 0], a[:, 1], a[:, 2]


def main():
    days = tape.DAYS
    reg = Register(OUT / "register.jsonl")
    fam_val = {f: [] for f in NAMES}       # per fold: configs x 24 validation PnL
    fam_test_all = {f: [] for f in NAMES}  # per fold: configs x 24 test PnL, for SPA only
    oos = {f: {"pnl": [], "inv": [], "fills": [], "fee_pnl": [], "day": []} for f in NAMES}
    val_scale = {}
    folds = []
    submitted = {}
    for k in range(FIRST_TEST, len(days)):
        train_days, test_day = days[:k], days[k]
        val_day = train_days[-1]
        t0 = time.time()
        cal = tape.calibrate(train_days)
        scaler = rl.feature_scaler(train_days)
        val_tape, test_tape = tape.real(val_day), tape.real(test_day)
        cands = {"avellaneda_stoikov": baselines.as_grid(cal), "multi_level": baselines.multi_grid(cal)}
        ck = rl.train(cal, scaler, seed=k)
        cands["rl_adversarial"] = [{"family": "rl_adversarial", "kind": env.LINEAR, "params": rl.to_params(c["theta"]).tolist(),
                                    "label": f"checkpoint {c['iter']}", "worst_regime": c["worst_regime"]} for c in ck]
        cands["leaky_oracle"] = [{"family": "leaky_oracle", "kind": env.LINEAR, "params": rl.leaky_params().tolist(),
                                  "leak": True, "label": "future five seconds, weight 1"}]
        chosen = {}
        for fam, cfgs in cands.items():
            M = []
            for c in cfgs:
                pnl, inv, fills = hourly(val_tape, c, scaler)
                M.append(pnl)
                reg.add(fold=k - FIRST_TEST, stage="validation", day=val_day, family=fam, label=c["label"],
                        params=[round(float(x), 6) for x in c["params"]], sharpe=st.sharpe(pnl),
                        pnl_per_hour=float(pnl.mean()), mean_abs_inv=float(inv.mean()), fills_per_hour=float(fills.mean()),
                        note="planted cheat, weight picked by looking at 2026-09-21" if fam == "leaky_oracle" else "")
            M = np.array(M)
            fam_val[fam].append(M)
            best = int(np.argmax([st.sharpe(r) for r in M]))
            chosen[fam] = cfgs[best]
            if k == FIRST_TEST:
                # The referee's scale must be fixed before the first test outcome is seen.
                val_scale[fam] = 4 * float(np.std(M[best])) + 1e-9
            pnl, inv, fills = hourly(test_tape, cfgs[best], scaler)
            fee_pnl, _, _ = hourly(test_tape, cfgs[best], scaler, fee_bps=FEE_SENSITIVITY_BPS)
            oos[fam]["pnl"].extend(pnl.tolist())
            oos[fam]["inv"].extend(inv.tolist())
            oos[fam]["fills"].extend(fills.tolist())
            oos[fam]["fee_pnl"].extend(fee_pnl.tolist())
            oos[fam]["day"].extend([test_day] * HOURS)
            reg.add(fold=k - FIRST_TEST, stage="submitted", day=test_day, family=fam, label=cfgs[best]["label"],
                    params=[round(float(x), 6) for x in cfgs[best]["params"]], sharpe=st.sharpe(pnl),
                    pnl_per_hour=float(pnl.mean()), mean_abs_inv=float(inv.mean()), fills_per_hour=float(fills.mean()), note="")
            # Every configuration on the test day as well, for the SPA test only. Never used to choose.
            fam_test_all[fam].append(np.array([hourly(test_tape, c, scaler)[0] for c in cfgs]))
            submitted.setdefault(fam, []).append(cfgs[best])
        folds.append({"test_day": test_day, "train_days": train_days, "validation_day": val_day,
                      "chosen": {f: c["label"] for f, c in chosen.items()}, "seconds": round(time.time() - t0, 1)})
        print(f"fold {k - FIRST_TEST} test {test_day} done in {time.time() - t0:.0f}s:",
              {f: round(float(np.mean(oos[f]["pnl"][-HOURS:])), 2) for f in NAMES})

    # Leakage: splice a different day's future onto the last test day and compare quotes.
    last_scaler = rl.feature_scaler(days[:-1])
    leak = {f: leakage.check(submitted[f][-1], tape.real(days[-1]), tape.real(days[-2]), last_scaler) for f in NAMES}

    trial_sharpes = [json.loads(l)["sharpe"] for l in (OUT / "register.jsonl").read_text().splitlines()
                     if json.loads(l)["stage"] == "validation"]
    n_trials = len(trial_sharpes)
    hypotheses = len(NAMES) + 2
    res_strats = {}
    for fam in NAMES:
        o = {kk: np.array(v) if kk != "day" else v for kk, v in oos[fam].items()}
        pnl = o["pnl"]
        mean_ci = st.bootstrap(np.mean, [pnl])
        sh_ci = st.bootstrap(st.sharpe, [pnl])
        inv_ci = st.bootstrap(np.mean, [o["inv"]])
        dsr, sr0 = st.deflated_sharpe(pnl, trial_sharpes)
        Mval = np.concatenate(fam_val[fam], axis=1)
        p_bo = st.pbo(Mval)
        Mtest = np.concatenate(fam_test_all[fam], axis=1)
        spa_p, _ = st.spa(Mtest)
        trl = st.min_trl(pnl)
        by_day = [float(np.mean(pnl[i * HOURS:(i + 1) * HOURS])) for i in range(len(folds))]
        frac_pos = float(np.mean([d > 0 for d in by_day]))
        comps = {
            "deflated_sharpe": {"value": dsr, "threshold": 0.95, "pass": dsr >= 0.95, "margin": (dsr - 0.95) / 0.05},
            "overfitting_probability": {"value": p_bo, "threshold": 0.5, "pass": p_bo is not None and p_bo < 0.5,
                                        "margin": -1.0 if p_bo is None else (0.5 - p_bo) / 0.5},
            "superior_predictive_ability_p": {"value": spa_p, "threshold": 0.05, "pass": spa_p < 0.05, "margin": (0.05 - spa_p) / 0.05},
            "min_track_record_hours": {"value": None if not np.isfinite(trl) else trl, "threshold": len(pnl),
                                       "pass": bool(np.isfinite(trl) and trl <= len(pnl)),
                                       "margin": -1.0 if not np.isfinite(trl) else (len(pnl) - trl) / len(pnl)},
            "regime_stability": {"value": frac_pos, "threshold": 0.75, "pass": frac_pos >= 0.75, "margin": (frac_pos - 0.75) / 0.25},
        }
        e = st.eprocess(pnl, val_scale[fam])
        res_strats[fam] = {
            "name": NAMES[fam],
            "chosen_by_fold": [f["chosen"][fam] for f in folds],
            "oos": {"episodes": int(len(pnl)), "pnl_per_hour": mean_ci[0], "pnl_per_hour_ci95": mean_ci[1],
                    "total_pnl": float(pnl.sum()), "sharpe_hourly": sh_ci[0], "sharpe_hourly_ci95": sh_ci[1],
                    "mean_abs_inventory_eth": inv_ci[0], "mean_abs_inventory_ci95": inv_ci[1],
                    "fills_per_hour": float(o["fills"].mean()), "pnl_per_hour_by_day": by_day,
                    "pnl_per_hour_with_1bp_fee": float(o["fee_pnl"].mean()),
                    "cumulative_pnl": np.cumsum(pnl).round(3).tolist()},
            "minerva": {**st.minerva(comps), "components": comps, "deflation_benchmark_sharpe": sr0},
            "referee": {"max_evalue": float(e.max()), "p_anytime": float(min(1.0, 1 / e.max())),
                        "p_after_search": float(min(1.0, n_trials / e.max())), "scale": val_scale[fam]},
            "leakage": leak[fam],
        }
    comparisons = {}
    for a, b in [("rl_adversarial", "avellaneda_stoikov"), ("rl_adversarial", "multi_level")]:
        d = np.array(oos[a]["pnl"]) - np.array(oos[b]["pnl"])
        m, ci = st.bootstrap(np.mean, [d])
        e = st.eprocess(d, val_scale[a] + val_scale[b])
        comparisons[f"{a}_minus_{b}"] = {"mean_per_hour": m, "ci95": ci, "max_evalue": float(e.max()),
                                         "p_anytime": float(min(1.0, 1 / e.max())),
                                         "p_after_search": float(min(1.0, n_trials / e.max()))}
    main_cmp = comparisons["rl_adversarial_minus_avellaneda_stoikov"]
    beats = main_cmp["ci95"][0] > 0 and main_cmp["p_after_search"] < 0.05
    certified = [f for f, r in res_strats.items() if r["leakage"]["verdict"] == "clean" and r["referee"]["p_after_search"] < 0.05
                 and r["oos"]["pnl_per_hour_ci95"][0] > 0]
    res = {
        "symbol": "ETHUSDT", "days": days, "hours_per_test_day": HOURS, "quote_size_eth": 0.1, "inventory_cap_eth": 1.0,
        "fee_bps": 0.0, "folds": folds, "strategies": res_strats, "comparisons": comparisons,
        "register": {"entries": reg.n, "validation_trials": n_trials, "hypotheses_submitted": hypotheses,
                     "by_family": {f: sum(1 for l in (OUT / "register.jsonl").read_text().splitlines() if json.loads(l)["family"] == f) for f in NAMES}},
        "verdict": {"candidate_beats_avellaneda_stoikov": bool(beats), "certified_strategies": certified},
        "rl_cut": {"policy": "linear over five features and inventory, 13 numbers", "training": "evolution strategies, 200 iterations, 16 antithetic pairs, 8 episodes of 15 minutes",
                   "adversary": "best response over 8 fixed regimes each iteration, not a learned network", "no_lstm": True},
    }
    # numpy scalars sneak in through comparisons and means. Turn them into plain JSON values.
    plain = lambda o: o.item() if hasattr(o, "item") else str(o)  # noqa: E731
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=plain) + "\n")
    print(json.dumps({"verdict": res["verdict"], "main": main_cmp,
                      "per_hour": {f: [round(r["oos"]["pnl_per_hour"], 2), [round(x, 2) for x in r["oos"]["pnl_per_hour_ci95"]],
                                       r["minerva"]["score"], round(r["referee"]["p_after_search"], 3), r["leakage"]["verdict"]]
                                   for f, r in res_strats.items()}}, indent=1))


if __name__ == "__main__":
    main()
