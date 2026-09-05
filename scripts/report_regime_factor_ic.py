# -*- coding: utf-8 -*-
"""Regime-bucketed factor-IC and signal-win-rate report (docs/
regime_strategy_research.md Phase B) -- the evidence behind the bull/bear/
consolidation weight and STRATEGY_PARAMS proposals in that doc.

Two independent halves, printed by one script because they share the same
TAIEX regime classification as their bucketing key:

1. Factor IC by regime (feeds a RANKING_WEIGHTS-by-regime proposal) --
   off scripts/run_factor_backtest.py's per-code cache (cache/factor_run/
   *.json), same source scripts/report_factor_ic.py reads, no re-fetch.
2. Signal win-rate by regime (feeds a STRATEGY_PARAMS-by-regime proposal) --
   off data/backtest.db's daily_bars for whichever watchlist.json codes
   src/run_backtest.py has already imported, pooled across codes. Degrades
   gracefully (prints a note, doesn't crash) if that db is empty -- same
   stance as src/pipeline.py's build_suggestions()/build_recommendations().

TAIEX itself is fetched fresh on every run via data_sources.get_index_daily()
-- it's a single small series, not worth caching across runs the way the
2000+-code universe is.

Usage:
    python scripts/report_regime_factor_ic.py [--horizons 1,5,20,60] [--confirm-days 3]
"""
import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import backtest as bt  # noqa: E402
import data_sources as ds  # noqa: E402
import db  # noqa: E402
import factor_eval as fe  # noqa: E402
import market_regime as mr  # noqa: E402
import run_backtest as rb  # noqa: E402

import report_factor_ic as rfi  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")


def _watchlist_codes():
    try:
        with open(WATCHLIST_PATH, encoding="utf-8") as f:
            return [row["code"] for row in json.load(f)]
    except (OSError, ValueError):
        return []


def _daily_from_db(conn, code):
    """db.query_daily_bars()'s rows -> signal_win_rates_by_regime()'s
    {t,o,c,v} columnar shape, split/dividend-adjusted the same way
    src/run_backtest.py's own replay does (rb._adjusted_bars()). None if
    this code has no rows yet (run_backtest.py --refresh-data hasn't
    imported it), or if its adj_factor column is only partially populated
    (rb._adjusted_bars() raises ValueError in that case -- see its
    docstring; this report skips the code and reports it rather than
    crashing the whole run)."""
    rows = db.query_daily_bars(conn, code)
    if not rows:
        return None
    try:
        adjusted = rb._adjusted_bars(rows)
    except ValueError as e:
        print(f"  [skip] {code}: {e}")
        return None
    times = [bt._epoch(r["date"]) for r in adjusted]
    return {"t": times, "o": [r["o"] for r in adjusted], "c": [r["c"] for r in adjusted], "v": [r["v"] for r in adjusted]}


def _pool_signal_stats(per_code_results):
    """[{"bull": [rows...], "bear": [...], "consolidation": [...]}, ...]
    (one dict per code, an.signal_win_rates_by_regime()'s own return shape)
    -> one pooled dict of the same shape, summing n/wins across codes for
    each (signal, horizon) and weighted-averaging avg_ret by n. That
    weighted average is EXACT, not an approximation: avg_ret_i * n_i is
    exactly that code's total (not average) return over its n samples, so
    summing those and dividing by total n reproduces the pooled average
    return as if every code's individual trades had been combined into one
    list from the start."""
    pooled = {regime: {} for regime in bt.REGIMES}
    for result in per_code_results:
        for regime, rows in result.items():
            for row in rows:
                key = (row["signal"], row["horizon"])
                acc = pooled[regime].setdefault(key, {"n": 0, "wins": 0, "ret_sum": 0.0})
                acc["n"] += row["n"]
                acc["wins"] += row["wins"] or 0
                acc["ret_sum"] += (row["avg_ret"] or 0.0) * row["n"]
    out = {}
    for regime, by_key in pooled.items():
        rows = []
        for (signal, horizon), acc in by_key.items():
            n = acc["n"]
            rows.append({
                "signal": signal, "horizon": horizon, "n": n,
                "wins": acc["wins"] if n else None,
                "win_rate": (acc["wins"] / n * 100) if n else None,
                "avg_ret": (acc["ret_sum"] / n) if n else None,
            })
        out[regime] = rows
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizons", type=str, default="1,5,20,60")
    ap.add_argument("--confirm-days", type=int, default=mr.CONFIRM_DAYS)
    args = ap.parse_args()
    horizons = tuple(int(h) for h in args.horizons.split(","))

    print("抓取 TAIEX 日線 ...")
    taiex = ds.get_index_daily("TAIEX")
    raw_regime_series = mr.regime_series(taiex)
    classified = [(d, r) for d, r in raw_regime_series if r is not None]
    print(f"TAIEX source={taiex['source']}, {len(taiex['c'])} 根K棒"
          + (f"，可分類區間 {classified[0][0]} .. {classified[-1][0]}（{len(classified)} 天）" if classified else "，樣本不足，尚無法分類"))
    raw_counts = Counter(r for _, r in raw_regime_series)
    print(f"原始（未防抖）分類天數：{dict(raw_counts)}")
    effective_series = mr.effective_regime_series(raw_regime_series, confirm_days=args.confirm_days)
    eff_counts = Counter(r for _, r in effective_series)
    print(f"防抖後（confirm_days={args.confirm_days}）生效天數：{dict(eff_counts)}")

    # ---------------------------------------- 因子 IC 分盤勢（RANKING_WEIGHTS）--
    print("\n\n=== 因子 IC 分盤勢 ===")
    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")
    if not universe_data:
        print("nothing cached -- run scripts/run_factor_backtest.py --universe all first")
    else:
        dates = fe.rebalance_dates(universe_data)
        result = fe.evaluate_factors_by_regime(universe_data, raw_regime_series, dates=dates, horizons=horizons)
        for regime in fe.REGIMES:
            bucket = result[regime]
            print(f"\n-- {regime} (sample_dates={bucket['sample_dates']}) --")
            if not bucket["factors"]:
                print("  樣本數為 0，無法評估")
                continue
            print(f"  {'factor':<10}" + "".join(f"{'h=' + str(h):>34}" for h in horizons))
            for factor_name in sorted(bucket["factors"]):
                row = "".join(f"  {rfi._fmt(bucket['factors'][factor_name][h]):>32}" for h in horizons)
                print(f"  {factor_name:<10}{row}")

    # ------------------------------------ 訊號勝率分盤勢（STRATEGY_PARAMS）--
    print("\n\n=== 訊號勝率分盤勢（watchlist 個股池化）===")
    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)
    codes = _watchlist_codes()
    per_code_results = []
    for code in codes:
        daily = _daily_from_db(conn, code)
        if daily is None:
            continue
        per_code_results.append(bt.signal_win_rates_by_regime(daily, raw_regime_series, horizons=horizons))
    conn.close()

    if not per_code_results:
        print("data/backtest.db 尚無任何 watchlist 個股資料 -- 先跑 src/run_backtest.py 匯入資料")
        return

    print(f"{len(per_code_results)}/{len(codes)} 檔 watchlist 個股已有資料，池化統計如下：")
    pooled = _pool_signal_stats(per_code_results)
    for regime in bt.REGIMES:
        print(f"\n-- {regime} --")
        for row in sorted(pooled[regime], key=lambda r: (r["signal"], r["horizon"])):
            wr = f"{row['win_rate']:.1f}%" if row["win_rate"] is not None else "n/a"
            ar = f"{row['avg_ret']:+.2f}%" if row["avg_ret"] is not None else "n/a"
            print(f"  {row['signal']:<14} h={row['horizon']:<4} n={row['n']:<5} win_rate={wr:<8} avg_ret={ar}")


if __name__ == "__main__":
    main()
