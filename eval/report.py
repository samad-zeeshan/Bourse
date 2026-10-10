"""
Write the README's generated blocks and the demo's data file from the result files.

--check fails if the README or site/data/results.json differ from what the files say. That is the drift gate in CI.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SITE = ROOT / "site" / "data" / "results.json"


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def fuzz_log():
    lines = (ROOT / "fuzz" / "logs" / "fuzz-24h.log").read_text().splitlines()
    starts = [l for l in lines if " START " in l]
    last = [l for l in lines if " HEARTBEAT " in l or " END " in l][-1]
    kv = dict(x.split("=") for x in last.split()[2:])
    # A restart appends a new START line. Elapsed time then adds up across the segments.
    segs, cur = [], None
    for l in lines:
        if " START " in l:
            if cur:
                segs.append(cur)
            cur = None
        elif " HEARTBEAT " in l or " END " in l:
            cur = dict(x.split("=") for x in l.split()[2:])
    if cur:
        segs.append(cur)
    tot = lambda k: sum(int(s[k]) for s in segs)
    return {"hours": tot("elapsed_s") / 3600, "cases": tot("cases"), "commands": tot("commands"),
            "violations": tot("violations"), "reference_diffs": tot("reference_diffs"),
            "finished": " END " in last and tot("elapsed_s") >= 24 * 3600, "restarts": len(starts) - 1}


def n(v, digits=0):
    return f"{v:,.{digits}f}"


def r2(v):
    """Two significant figures, for prose."""
    if v == 0:
        return "0"
    from math import floor, log10
    d = max(0, 1 - int(floor(log10(abs(v)))))
    return f"{round(v, d):,.{d}f}"


def speed(b, pub, meth):
    p = b["per_order"]
    rows = ["| Exchange | Machine | Median | 99th percentile | 99.9th percentile | Slowest | Orders per second |",
            "|---|---|---|---|---|---|---|",
            f"| Bourse, matcher alone | {meth['cpu'].replace(' 6-Core Processor', '')}, Windows PC | {p['p50_ns']} ns | {p['p99_ns']} ns | "
            f"{p['p999_ns']} ns | {n(p['max_ns'] / 1000, 1)} µs | {n(b['single_thread_orders_per_sec'] / 1e6, 0)} million |",
            f"| Bourse, three threads with queues | same | | | | | {n(b['pipeline_orders_per_sec'] / 1e6, 0)} million |"]
    for row in pub["rows"]:
        rows.append(f"| {pub['engine']} at {n(row['rate_per_sec'] / 1e6, 0)} million a second | Xeon X5690, tuned server | "
                    f"{n(row['p50_ns'] / 1000, 1)} µs | {n(row['p99_ns'] / 1000, 0)} µs | {n(row['p999_ns'] / 1000, 0)} µs | "
                    f"{n(row['max_ns'] / 1000, 0)} µs | published |")
    return "\n".join(rows)


def method(meth):
    plan = meth["power_plan"].split("(")[-1].rstrip(")")
    return (f"The test PC had an {meth['cpu']} with {meth['logical_cpus']} logical cores. "
            f"It ran {meth['os']} on the {plan} power plan. "
            f"The compiler was {meth['compiler']}. "
            f"The matcher ran on logical core {meth['matcher_cpu']} at time-critical priority. "
            "The test did not isolate that core. "
            f"The clock of the processor gave {meth['tsc_ticks_per_ns']:.2f} ticks per nanosecond. "
            f"The test warmed up with {n(meth['warm_up_orders'])} orders, then timed {n(meth['measured_orders'])} orders one by one. "
            "The cost of the timer shows separately, and the test does not subtract it.")


def correctness(spec, det, fz):
    runs = spec["runs"]
    rows = ["| Check | What it tried | Result |", "|---|---|---|",
            f"| Property tests against the Python reference copy | 3,000 random order streams per run | all match |",
            f"| Fuzz run against the same reference copy | {r2(fz['hours'])} hours, {r2(fz['commands'] / 1e9)} billion orders | "
            f"{fz['violations'] + fz['reference_diffs']} disagreements or broken rules |",
            f"| TLA+ model of the order life and the hand-off | {n(runs['MCBourse']['distinct_states'])} distinct states | "
            f"{'no rule broken' if runs['MCBourse']['result'] == 'pass' else 'rule broken'} |",
            f"| Same model, tail moves before the slot holds the order | planted bug | caught ({', '.join(runs['MCBoursePublishFirst']['violated'])}) |",
            f"| Same model, new orders put at the front | planted bug | caught ({', '.join(runs['MCBourseLifo']['violated'])}) |",
            f"| Replay of {n(det['commands'])} orders in {len(det['runs'])} separate runs | Windows processes and a Linux container | "
            f"{'identical bytes' if det['all_identical'] else 'outputs differ'} |"]
    return "\n".join(rows)


def flow_table(hw):
    rows = ["| Day | Market orders in two hours | Self-excitation | Fit error, buys | Fit error, sells | Fit error with constant rates | Error with the fit of the day before |",
            "|---|---|---|---|---|---|---|"]
    for d in hw["days"]:
        oos = d.get("oos")
        rows.append(f"| {d['day']} | {n(d['events'])} | {d['params']['branching_ratio']:.2f} | {d['ks'][0]:.3f} | {d['ks'][1]:.3f} | "
                    f"{max(d['ks_poisson']):.2f} | {max(oos['ks']):.2f} |" if oos else
                    f"| {d['day']} | {n(d['events'])} | {d['params']['branching_ratio']:.2f} | {d['ks'][0]:.3f} | {d['ks'][1]:.3f} | "
                    f"{max(d['ks_poisson']):.2f} | first day |")
    return "\n".join(rows)


def impact_table(sq):
    rows = ["| How the order is sent | Exponent | 95% interval | Sizes used in the fit |", "|---|---|---|---|"]
    for mode, a in sq["arms"].items():
        if a["delta"] is None:
            rows.append(f"| {sq['labels'][mode]} | no fit possible | | {a['points_used']} of {len(a['points'])} |")
        else:
            rows.append(f"| {sq['labels'][mode]} | {a['delta']:.2f} | {a['delta_ci95'][0]:.2f} to {a['delta_ci95'][1]:.2f} | "
                        f"{a['points_used']} of {len(a['points'])} |")
    return "\n".join(rows)


def strat_table(mm):
    rows = ["| Robot | Profit per hour, USDT | 95% interval | Hourly Sharpe | Robustness grade | Chance it is luck, after the search | Leakage check |",
            "|---|---|---|---|---|---|---|"]
    for s in mm["strategies"].values():
        o = s["oos"]
        rows.append(f"| {s['name']} | {o['pnl_per_hour']:.2f} | {o['pnl_per_hour_ci95'][0]:.2f} to {o['pnl_per_hour_ci95'][1]:.2f} | "
                    f"{o['sharpe_hourly']:.2f} | {s['minerva']['score']:.0f} of 100 | {s['referee']['p_after_search']:.2f} | "
                    f"{'clean' if s['leakage']['verdict'] == 'clean' else 'refused, reads the future'} |")
    return "\n".join(rows)


def texts(b, det, fz, rec, hw, sq, syn, mm):
    split = sq["arms"]["split"]
    cmp_ = mm["comparisons"]["rl_adversarial_minus_avellaneda_stoikov"]
    rl = mm["strategies"]["rl_adversarial"]["oos"]
    av = mm["strategies"]["avellaneda_stoikov"]["oos"]
    cheat = mm["strategies"]["leaky_oracle"]
    found = fz["violations"] + fz["reference_diffs"]
    # A log with no END line means the run stopped early. The generator cannot tell a live run from a dead one,
    # and a claim that the run is still going goes stale the moment it stops.
    if fz["finished"]:
        fz_time = f"The run lasted its full {r2(fz['hours'])} hours."
    else:
        fz_time = (f"The run stopped after about {r2(fz['hours'])} hours, so it did not reach the 24 hours it aimed for. "
                   "The shorter run gives less proof than planned.")
    if mm["verdict"]["candidate_beats_avellaneda_stoikov"]:
        verdict = "The interval does not include zero, so the trained robot is better."
    elif cmp_["ci95"][1] < 0:
        verdict = "The interval does not include zero, so the trained robot is worse."
    else:
        verdict = "The interval includes zero, so the test does not show that the trained robot is better."
    return {
        "speed-summary": f"Half of all orders took {b['per_order']['p50_ns']} nanoseconds or less. "
                         f"Out of every 100 orders, 99 took {b['per_order']['p99_ns']} nanoseconds or less. "
                         f"The goal was one microsecond, so both times are well inside it. "
                         f"One thread handled about {r2(b['single_thread_orders_per_sec'] / 1e6)} million orders a second. "
                         f"The goal was one million, so this is well above it.",
        "fuzz-summary": f"The fuzz run sent about {r2(fz['commands'] / 1e9)} billion random orders to the exchange and to the reference copy. "
                        f"It found {found} problems, which is {'good' if found == 0 else 'bad'} news. " + fz_time,
        "data-summary": f"The order book of the exchange matched the Binance snapshot in {rec['snapshots_matching']} of "
                        f"{rec['snapshots_compared']} checks. Each check compared the top {rec['levels_compared_per_side']} prices on each side. "
                        f"The checks covered {r2(rec['minutes'])} minutes of recorded trades.",
        "impact-summary": f"The test split a large order into 60 pieces. The test then measured how fast the price moves as the order grows. "
                          f"The measured exponent was {split['delta']:.2f}, with a 95% interval from {r2(split['delta_ci95'][0])} to {r2(split['delta_ci95'][1])}. "
                          f"The square-root law predicts 0.5. So this simulator does not reproduce the law, which is a weak point.",
        "strategy-summary": f"The trained robot lost {r2(-rl['pnl_per_hour'])} USDT an hour on average. "
                            f"The textbook robot (Avellaneda-Stoikov) lost {r2(-av['pnl_per_hour'])} USDT an hour. "
                            f"The trained robot did {'better' if cmp_['mean_per_hour'] > 0 else 'worse'} than the textbook robot by "
                            f"{r2(abs(cmp_['mean_per_hour']))} USDT an hour. The 95% interval for this gap is {r2(cmp_['ci95'][0])} to {r2(cmp_['ci95'][1])}. "
                            f"{verdict} The planted cheat made {r2(cheat['oos']['pnl_per_hour'])} USDT an hour and passed the referee, "
                            f"but the leakage check refused it.",
        "fee-summary": f"A fee of one hundredth of a percent on each fill makes every robot lose more. The cheat then loses too, "
                       f"about {r2(-cheat['oos']['pnl_per_hour_with_1bp_fee'])} USDT an hour.",
        "synthetic-summary": "The fit gets worse when the next day uses the model of the day before. It still stays much better than constant rates. "
                             "Synthetic days made from these fits match the real days on how often orders come and how they bunch up. "
                             "They also match on spread, book depth and volume traded. "
                             f"But prices in them move about {r2(syn['price_move_per_minute_ticks']['real'] / syn['price_move_per_minute_ticks']['synthetic'])} "
                             "times less than real prices. Nothing in the simulation reacts to news. This is a weak point of the simulator.",
        "register-summary": f"The project logged every setting it tried before it saw any test result. There were {mm['register']['entries']} settings in all.",
    }


def site_data(b, meth, pub, det, fz, spec, rec, hw, sq, syn, mm):
    split = sq["arms"]["split"]
    strat = {}
    for k, s in mm["strategies"].items():
        o = s["oos"]
        strat[k] = {"name": s["name"], "pnl": o["pnl_per_hour"], "ci": o["pnl_per_hour_ci95"], "sharpe": o["sharpe_hourly"],
                    "grade": s["minerva"]["score"], "p": s["referee"]["p_after_search"], "leak": s["leakage"]["verdict"],
                    "cum": o["cumulative_pnl"]}
    return {
        "latency": {"p50": b["per_order"]["p50_ns"], "p99": b["per_order"]["p99_ns"], "p999": b["per_order"]["p999_ns"],
                    "max": b["per_order"]["max_ns"], "hist": b["histogram"], "single_mps": b["single_thread_orders_per_sec"],
                    "pipeline_mps": b["pipeline_orders_per_sec"], "cpu": meth["cpu"], "samples": b["per_order"]["samples"]},
        "published": pub["rows"],
        "determinism": {"runs": len(det["runs"]), "identical": det["all_identical"], "commands": det["commands"]},
        "fuzz": {k: fz[k] for k in ("hours", "commands", "violations", "reference_diffs", "finished")},
        "tlc_states": spec["runs"]["MCBourse"]["distinct_states"],
        "reconstruction": {k: rec[k] for k in ("snapshots_compared", "snapshots_matching", "minutes")},
        "impact": {"delta": split["delta"], "ci": split["delta_ci95"], "c": split["c"],
                   "points": [p for p in split["points"] if p["used"]], "all": split["points"]},
        "strategies": strat,
        "verdict": mm["verdict"],
        "comparison": mm["comparisons"]["rl_adversarial_minus_avellaneda_stoikov"],
        "register": mm["register"]["entries"],
    }


def render():
    b, meth, pub = load("bench/results.json"), load("bench/methodology.json"), load("bench/published.json")
    det, spec = load("eval/determinism/results.json"), load("eval/spec/results.json")
    rec, hw = load("eval/data/reconstruction.json"), load("eval/flow/hawkes.json")
    sq, syn, mm = load("eval/flow/sqrt_law.json"), load("eval/flow/synthetic_days.json"), load("eval/mm/results.json")
    fz = fuzz_log()
    t = texts(b, det, fz, rec, hw, sq, syn, mm)
    # Each table shares a block with the summary under it, so the README stays short. The blank line ends the table.
    blocks = {"speed": speed(b, pub, meth) + "\n\n" + t["speed-summary"], "method": method(meth),
              "correctness": correctness(spec, det, fz) + "\n\n" + t["fuzz-summary"], "data-summary": t["data-summary"],
              "flow": flow_table(hw) + "\n\n" + t["synthetic-summary"], "impact": impact_table(sq) + "\n\n" + t["impact-summary"],
              "strategies": strat_table(mm) + "\n\n" + t["strategy-summary"] + "\n\n" + t["fee-summary"],
              "register-summary": t["register-summary"]}
    data = site_data(b, meth, pub, det, fz, spec, rec, hw, sq, syn, mm)
    return blocks, json.dumps(data, indent=1) + "\n"


def apply(text, blocks):
    for name, body in blocks.items():
        # The body may be empty, and must never run past its own end marker into the next block.
        pat = re.compile(rf"(<!-- gen:{re.escape(name)} -->\n)(?:(?:(?!<!-- /gen -->).)*?\n)?(<!-- /gen -->)", re.S)
        if not pat.search(text):
            raise SystemExit(f"README has no gen:{name} block")
        text = pat.sub(lambda m: m.group(1) + body + "\n" + m.group(2), text)
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    blocks, data = render()
    old = README.read_text(encoding="utf-8")
    new = apply(old, blocks)
    if a.check:
        bad = []
        if new != old:
            bad.append("README.md")
        if not SITE.exists() or SITE.read_text(encoding="utf-8") != data:
            bad.append(str(SITE.relative_to(ROOT)))
        if bad:
            print("out of date with the results files: " + ", ".join(bad))
            sys.exit(1)
        print("README and site data match the results files")
        return
    README.write_text(new, encoding="utf-8", newline="\n")
    SITE.parent.mkdir(parents=True, exist_ok=True)
    SITE.write_text(data, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
