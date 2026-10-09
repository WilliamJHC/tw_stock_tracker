# -*- coding: utf-8 -*-
"""Rank-IC / ICIR / decay-curve report comparing alternative momentum
formation windows against the current MOMENTUM_DAYS=60, off scripts/
run_factor_backtest.py's per-code cache (cache/factor_run/*.json) -- no
network, no re-fetch. Answers docs/factor_timeframe_optimization_directions.md
S3.1: is MOMENTUM_DAYS=60's negative ICIR (see scripts/report_factor_ic.py)
an artifact of the window overlapping the most recent month, which academic
12-1 momentum construction (Jegadeesh & Titman) skips to avoid short-term
reversal contamination?

Windows compared:
    60d          -- current finlab_factors.MOMENTUM_DAYS, unchanged
    20d          -- pure short-term momentum, no skip
    120d-skip-21 -- ~12-1 momentum bar-count approximation (formation ends
                    21 trading days before the rebalance date instead of at
                    it)

Evaluation-only, same as report_factor_ic.py's "候選因子" section -- this
script does not modify finlab_factors.MOMENTUM_DAYS or composite_scores().
Whether any window replaces the current one is a human call per docs/
factor_timeframe_optimization_directions.md S3.1's governance note (ADR
0003-style evaluate_factors() report first, formal candidate review only if
the report justifies it).

Usage:
    python scripts/report_momentum_window_ic.py [--horizons 1,5,20,60]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import finlab_factors as ff  # noqa: E402
import factor_eval as fe  # noqa: E402
import metrics as m  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report_factor_ic import load_cached_universe, _fmt  # noqa: E402

# (label, days, skip) -- skip = bars between formation-window end and the
# rebalance date itself (0 = formation window ends AT the rebalance date,
# same convention as finlab_factors.momentum_pct()'s single-arg form).
WINDOWS = [
    ("60d (現行 MOMENTUM_DAYS)", 60, 0),
    ("20d (短期動能)", 20, 0),
    ("120d-skip-21 (近似 12-1 動能)", 120, 21),
]


def _momentum_pct_skip(bars, idx, days, skip):
    """momentum_pct() generalized with a `skip` gap between the formation
    window's end and idx -- skip=0 reduces to ff.momentum_pct(bars, idx,
    days). None if idx doesn't have `days + skip` bars of history."""
    end_idx = idx - skip
    if end_idx - days < 0 or end_idx < 0:
        return None
    base = ff._close(bars[end_idx - days])
    if not base:
        return None
    return (ff._close(bars[end_idx]) - base) / base * 100


def _raw_momentum_values(universe_data, date, days, skip):
    """{code: (bars, idx, mom)} at `date` for one (days, skip) window --
    same liquidity filter as factor_eval._raw_factor_values(), momentum-only
    (no yoy/roe co-availability requirement, since this report evaluates
    momentum in isolation, not composite_scores()'s "all three" filter)."""
    raw = {}
    for code, d in universe_data.items():
        bars, bar_dates = d["bars"], d["bar_dates"]
        idx = ff._bar_index_on_or_before(bar_dates, date)
        if idx is None or not ff.liquidity_ok(bars, idx):
            continue
        mom = _momentum_pct_skip(bars, idx, days, skip)
        if mom is None:
            continue
        raw[code] = (bars, idx, mom)
    return raw


def evaluate_momentum_window(universe_data, days, skip, dates, horizons):
    """{horizon: ic_stats_dict} for one (days, skip) momentum window --
    same shape as factor_eval.evaluate_candidate_factor()'s return, so this
    report can reuse report_factor_ic.py's _fmt()."""
    raw_by_date = {date: _raw_momentum_values(universe_data, date, days, skip) for date in dates}
    result = {}
    for horizon in horizons:
        ic_series = []
        for date in dates:
            factor_values, forward_returns = [], []
            for bars, idx, mom in raw_by_date[date].values():
                fr = fe._forward_return(bars, idx, horizon)
                if fr is None:
                    continue
                factor_values.append(mom)
                forward_returns.append(fr)
            ic_series.append(m.rank_ic(factor_values, forward_returns))
        result[horizon] = m.ic_stats(ic_series)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizons", type=str, default="1,5,20,60")
    args = ap.parse_args()
    horizons = tuple(int(h) for h in args.horizons.split(","))

    universe_data = load_cached_universe()
    print(f"{len(universe_data)} codes loaded")
    if not universe_data:
        print("nothing cached yet -- run scripts/run_factor_backtest.py --universe all first")
        return

    dates = fe.rebalance_dates(universe_data)
    print(f"{len(dates)} rebalance dates: {dates[0]} .. {dates[-1]}\n")

    print(f"{'window':<32}" + "".join(f"{'h=' + str(h):>34}" for h in horizons))
    for label, days, skip in WINDOWS:
        result = evaluate_momentum_window(universe_data, days, skip, dates, horizons)
        row = "".join(f"  {_fmt(result[h]):>32}" for h in horizons)
        print(f"{label:<32}{row}")


if __name__ == "__main__":
    main()
