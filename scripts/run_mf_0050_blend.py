# -*- coding: utf-8 -*-
"""Step 3 of docs/delisted_survivorship_preregistration_2026-10.md (section 5): does
holding part of the portfolio in the multi-factor strategy and the rest in 0050 beat
pure 0050? The ratio is chosen on 2013-12-02..2019-12-31 and validated from 2020-01-02.

Runs only when the pre-registration is locked AND output/survivorship_study.json (written
by run_survivorship_study.py) says the step-2 gate passed; otherwise it refuses, so the
blend results are never looked at when the registered condition is not met.

Usage (from the project root):
    python scripts/run_mf_0050_blend.py [--study output/survivorship_study.json] [--out output/mf_0050_blend.json]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import benchmark as bm  # noqa: E402
import run_survivorship_study as st  # noqa: E402

BASE_DIR = st.BASE_DIR
RATIOS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]  # share in the multi-factor sleeve; n_trials = 6
LEG_COST = 0.004                          # 0.3% fee + 0.1% slippage, per leg, same as the multi-factor simulation
TRAIN = (st.WINDOW_START, st.TRAIN_END)
TEST_START = "2020-01-02"


def blend_curve(mf_curve, bench_curve, w, start, end):
    """Daily value curve [{date, value}] of a portfolio holding share `w` in the multi-factor
    sleeve and 1-w in 0050 over [start, end] (common trading days only).

    Day 1 sets the target weights at no cost. On every later day each sleeve first earns that
    day's return; then, if the day is the first common trading day of its calendar month, the
    sleeves are reset to the target weights at that day's close. Cost = LEG_COST on the amount
    sold from one sleeve plus LEG_COST on the amount bought into the other (so 2 * LEG_COST of
    the amount shifted), deducted from the total before the reset."""
    days = [d for d in bm.common_trading_days(mf_curve, bench_curve) if start <= d <= end]
    if len(days) < 2:
        return []
    mf, bn = bm.as_map(mf_curve), bm.as_map(bench_curve)
    vm, vb = w, 1.0 - w
    out = [{"date": days[0], "value": 1.0}]
    for prev, day in zip(days, days[1:]):
        vm *= mf[day] / mf[prev]
        vb *= bn[day] / bn[prev]
        if day[:7] != prev[:7]:
            total = vm + vb
            shifted = abs(vm - w * total)
            total -= 2 * LEG_COST * shifted
            vm, vb = w * total, (1.0 - w) * total
        out.append({"date": day, "value": vm + vb})
    return out


def stats(curve):
    return st.curve_stats([r["value"] for r in curve], [r["date"] for r in curve])


def pick_ratio(mf_curve, bench_curve):
    """Ratio with the highest Sharpe over TRAIN (rounded to 3 decimals; ties go to the smaller ratio)."""
    table = {w: stats(blend_curve(mf_curve, bench_curve, w, *TRAIN)) for w in RATIOS}
    best = max(RATIOS, key=lambda w: (round(table[w]["sharpe"], 3), -w))
    return best, table


def validate(mf_curve, bench_curve, w, end):
    """Chosen blend vs pure 0050 over TEST_START..end. Passes only if BOTH Sharpe and CAGR are strictly higher."""
    chosen = stats(blend_curve(mf_curve, bench_curve, w, TEST_START, end))
    pure = stats(blend_curve(mf_curve, bench_curve, 0.0, TEST_START, end))
    ok = chosen["sharpe"] > pure["sharpe"] and chosen["cagr_pct"] > pure["cagr_pct"]
    return {"ratio": w, "blend": chosen, "pure_0050": pure, "pass": ok}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--study", default=os.path.join(BASE_DIR, "output", "survivorship_study.json"))
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "output", "mf_0050_blend.json"))
    args = ap.parse_args()
    if not st.prereg_is_locked():
        sys.exit("pre-registration not locked; fill in its section 8 first")
    with open(args.study, encoding="utf-8") as f:
        study = json.load(f)
    if not study.get("gate_V2", {}).get("pass"):
        sys.exit("step-2 gate did not pass (gate_V2.pass is not true); step 3 is not run, by registration")

    mf_curve = study["V2_curve"]
    bench = st.load_benchmark()
    best, train_table = pick_ratio(mf_curve, bench)
    result = {"chosen_ratio": best, "train_table": train_table}
    if best == 0.0:
        result["validation"] = {"ratio": 0.0, "pass": False, "note": "no multi-factor ratio beat pure 0050 in training"}
    else:
        result["validation"] = validate(mf_curve, bench, best, st.WINDOW_END)
    result["test_table"] = {w: stats(blend_curve(mf_curve, bench, w, TEST_START, st.WINDOW_END)) for w in RATIOS}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("chosen ratio:", best, "| validation pass:", result["validation"]["pass"])


if __name__ == "__main__":
    main()
