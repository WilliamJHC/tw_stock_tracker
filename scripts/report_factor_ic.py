# -*- coding: utf-8 -*-
"""Rank-IC / ICIR / decay-curve report for the ranking strategy's three
factors (revenue-YoY/ROE/momentum), off scripts/run_factor_backtest.py's
per-code cache (cache/factor_run/*.json) -- no network, no re-fetch. See
docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md Phase 4
("自建 rank-IC 因子評估，取代 Qlib") -- this is the report that verification
step asks for, backed by src/factor_eval.py's evaluate_factors().

Decoupled from run_factor_backtest.py on purpose: the cache is populated
incrementally and survives a killed/still-running fetch (see that script's
docstring), so this report can be run on a partial universe while a long
--universe all fetch is still going, not just after it finishes.

Usage:
    python scripts/report_factor_ic.py [--horizons 1,5,20,60]
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import factor_eval as fe  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_CACHE_DIR = os.path.join(BASE_DIR, "cache", "factor_run")


def load_cached_universe():
    """{code: {bars, bar_dates, revenue_series, roe_series}} from every
    non-skipped cache/factor_run/*.json -- same shape fetch_universe_data()
    builds in memory, reconstructed from disk instead."""
    universe_data = {}
    for path in glob.glob(os.path.join(RUN_CACHE_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if data.get("skip"):
            continue
        code = os.path.splitext(os.path.basename(path))[0]
        universe_data[code] = {
            "bars": data["bars"],
            "bar_dates": [b["date"] for b in data["bars"]],
            "revenue_series": data["revenue_series"],
            "roe_series": data["roe_series"],
            # Phase 6b candidate-factor series -- absent on a cache entry
            # from before Phase 6b existed and never backfilled (margin
            # trend isn't retroactively backfilled, see
            # run_factor_backtest.py's fetch_universe_data() docstring);
            # .get() default keeps this report running on a mixed-vintage
            # cache instead of crashing on the older entries.
            "value_series": data.get("per_pbr_series", []),
            "gross_margin_trend_series": data.get("gross_margin_trend_series", []),
            "operating_margin_trend_series": data.get("operating_margin_trend_series", []),
        }
    return universe_data


def _fmt(stats):
    if stats["mean_ic"] is None:
        return f"{'n/a':>8} (n={stats['n']})"
    return f"{stats['mean_ic']:>+7.3f}  t={stats['t_stat']:>+6.2f}  icir={stats['icir']:>+6.3f}  n={stats['n']}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizons", type=str, default="1,5,20,60")
    args = ap.parse_args()
    horizons = tuple(int(h) for h in args.horizons.split(","))

    universe_data = load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {RUN_CACHE_DIR}")
    if not universe_data:
        print("nothing cached yet -- run scripts/run_factor_backtest.py --universe all first")
        return

    dates = fe.rebalance_dates(universe_data)
    print(f"{len(dates)} rebalance dates: {dates[0]} .. {dates[-1]}\n")

    result = fe.evaluate_factors(universe_data, dates=dates, horizons=horizons)
    print(f"{'factor':<10}" + "".join(f"{'h=' + str(h):>34}" for h in horizons))
    for factor_name in sorted(result):
        row = "".join(f"  {_fmt(result[factor_name][h]):>32}" for h in horizons)
        print(f"{factor_name:<10}{row}")

    # Phase 6b candidate factors -- NOT part of RANKING_WEIGHTS, evaluation
    # only (see finlab_factors.py's value_series()/gross_margin_trend_series()/
    # operating_margin_trend_series() docstrings). Printed the same way so a
    # human can eyeball whether any of them clears the same IC/ICIR bar the
    # three adopted factors are held to.
    candidates = {
        "gross_margin_trend (候選)": ("gross_margin_trend_series", "trend_pct"),
        "operating_margin_trend (候選)": ("operating_margin_trend_series", "trend_pct"),
        "value/pbr (候選)": ("value_series", "value_score"),
    }
    print("\n候選因子（Phase 6b，先評估、不保證採用）：")
    for label, (series_key, value_key) in candidates.items():
        candidate_result = fe.evaluate_candidate_factor(universe_data, series_key, value_key, dates=dates, horizons=horizons)
        row = "".join(f"  {_fmt(candidate_result[h]):>32}" for h in horizons)
        print(f"{label:<28}{row}")

    print("\n目前 RANKING_WEIGHTS 建議（僅供參考，不自動套用）：")
    for horizon in horizons:
        suggestion = fe.suggest_weights(result, horizon=horizon)
        print(f"  h={horizon}: {suggestion if suggestion else '無因子 ICIR 有訊號，維持現有權重'}")


if __name__ == "__main__":
    main()
