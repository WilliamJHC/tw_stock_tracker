# -*- coding: utf-8 -*-
"""Regime-bucketed rank-IC for the same three momentum formation windows as
scripts/report_momentum_window_ic.py -- the follow-up docs/
factor_ic_candidate_and_momentum_window_report.md's "下一步" section asks
for: does 120d-skip-21's h=60 improvement over the current 60d window
(t=+2.94 pooled) hold up in EACH of bull/bear/consolidation separately, or
is it one regime's effect dominating a pooled average?

Same TAIEX regime classification and bucketing convention as
scripts/report_regime_factor_ic.py (market_regime.regime_series(), no
confirm-days debounce needed here since factor_eval.evaluate_factors_by_
regime()'s own bucketing already only needs the raw classification, not the
debounced "effective" series that alerts.py/regime-switch logic uses).

Usage:
    python scripts/report_momentum_window_regime_ic.py [--horizons 1,5,20,60]
"""
import argparse
import bisect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import data_sources as ds  # noqa: E402
import factor_eval as fe  # noqa: E402
import market_regime as mr  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report_factor_ic import load_cached_universe, _fmt  # noqa: E402
from report_momentum_window_ic import WINDOWS, evaluate_momentum_window  # noqa: E402


def _bucket_dates_by_regime(dates, raw_regime_series):
    """{regime: [dates...]} -- same "as of" lookup as
    factor_eval._regime_as_of(), reimplemented locally rather than imported
    since that function is private to factor_eval.py."""
    series_dates = [d for d, _ in raw_regime_series]
    buckets = {regime: [] for regime in fe.REGIMES}
    for date in dates:
        idx = bisect.bisect_right(series_dates, date) - 1
        regime = raw_regime_series[idx][1] if idx >= 0 else None
        if regime in buckets:
            buckets[regime].append(date)
    return buckets


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizons", type=str, default="1,5,20,60")
    args = ap.parse_args()
    horizons = tuple(int(h) for h in args.horizons.split(","))

    print("抓取 TAIEX 日線 ...")
    taiex = ds.get_index_daily("TAIEX")
    raw_regime_series = mr.regime_series(taiex)
    print(f"TAIEX source={taiex['source']}, {len(taiex['c'])} 根K棒\n")

    universe_data = load_cached_universe()
    print(f"{len(universe_data)} codes loaded")
    if not universe_data:
        print("nothing cached yet -- run scripts/run_factor_backtest.py --universe all first")
        return

    dates = fe.rebalance_dates(universe_data)
    buckets = _bucket_dates_by_regime(dates, raw_regime_series)

    for regime in fe.REGIMES:
        bucket_dates = buckets[regime]
        print(f"\n-- {regime} (sample_dates={len(bucket_dates)}) --")
        if not bucket_dates:
            print("  樣本數為 0，無法評估")
            continue
        print(f"  {'window':<32}" + "".join(f"{'h=' + str(h):>34}" for h in horizons))
        for label, days, skip in WINDOWS:
            result = evaluate_momentum_window(universe_data, days, skip, bucket_dates, horizons)
            row = "".join(f"  {_fmt(result[h]):>32}" for h in horizons)
            print(f"  {label:<32}{row}")


if __name__ == "__main__":
    main()
