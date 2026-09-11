# -*- coding: utf-8 -*-
"""One-off: day-weighted expected annual excess return per candidate, vs both
0050 and 0051 -- turns scripts/report_vs_benchmark.py's regime_bucket table
(bull/bear/consolidation annualized excess, background-statistic only, not a
switch-rule evaluation) into a single number: if you just held the "pure"
(no-switching) candidate through however this 5y sample's regime mix actually
played out, what expected annual alpha does that day-mix imply. This is the
same arithmetic the user did by hand for baseline_top10 vs 0050
(37.4%*-20.0% + 52.0%*-10.7% + 10.6%*+58.8% ~= -6.8%/yr) -- this script
reruns it for all 4 CANDIDATES x {0050, 0051} instead of one by hand.

Deliberately reuses, not reimplements: scripts/report_vs_benchmark.py's
CANDIDATES / fetch_benchmark_curves(), src/portfolio_backtest.simulate(),
src/benchmark.value_curve(), and src/benchmark.regime_bucket_comparison()
(called exactly like scripts/report_vs_benchmark.py:170 does for its "pure"
line). This script assumes both cache/benchmark_0050.json and
cache/benchmark_0051.json already exist (from prior runs this session) and
deliberately does NOT refetch over the network if either is missing -- see
_require_cached_benchmark() below, which fails fast with a clear message
instead of silently falling through to a live yfinance call.

Usage:
    python scripts/regime_day_weighted_alpha.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import benchmark as bm  # noqa: E402
import data_sources as ds  # noqa: E402
import market_regime as mr  # noqa: E402
import metrics as mt  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report_vs_benchmark as rvb  # noqa: E402
import report_factor_ic as rfi  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCHMARKS = ["0050", "0051"]
BUCKETS = ("bull", "bear", "consolidation")


def _require_cached_benchmark(symbol):
    """Loads symbol's benchmark curve strictly from its on-disk cache
    (cache/benchmark_{symbol}.json, matching fetch_benchmark_curves()'s own
    default cache_path), reading the JSON file directly rather than calling
    fetch_benchmark_curves() -- that function falls through to a live
    yfinance fetch on ANY cache-miss condition (missing file, symbol/
    start_date mismatch), which would silently violate this script's "never
    touches the network" premise. sys.exit(1) with a clear message if the
    file is missing or unreadable; this script's whole point is "reuse what
    an earlier report_vs_benchmark.py run already fetched this session," so
    a missing/broken cache means that premise doesn't hold and the run
    should stop, not quietly fetch anyway."""
    cache_path = os.path.join(BASE_DIR, "cache", f"benchmark_{symbol}.json")
    if not os.path.exists(cache_path):
        print(f"卡點：{cache_path} 不存在 -- 這支腳本假設基準線快取已由之前的跑動產生，"
              f"不會自己連網補抓。請先跑一次 report_vs_benchmark.py --benchmark-symbol {symbol}。")
        sys.exit(1)
    with open(cache_path, encoding="utf-8") as f:
        cached = json.load(f)
    return cached["total_return_curve"], cached["price_curve"], cached.get("resolved_ticker", f"{symbol}.TW")


def day_weighted_annual_excess(bucket_result):
    """Sum(n_days_i / total_n_days * excess_annualized_pct_i) over buckets
    with data. Returns (weighted_pct, total_n_days, per_bucket_share) or
    (None, 0, {}) if every bucket is empty."""
    total_n = sum(bucket_result[r]["n_days"] for r in BUCKETS if bucket_result[r]["n_days"])
    if total_n == 0:
        return None, 0, {}
    weighted = 0.0
    shares = {}
    for r in BUCKETS:
        n = bucket_result[r]["n_days"]
        shares[r] = n / total_n
        if n and bucket_result[r]["excess_annualized_pct"] is not None:
            weighted += shares[r] * bucket_result[r]["excess_annualized_pct"]
    return weighted, total_n, shares


def main():
    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")

    taiex = ds.get_index_daily("TAIEX")
    raw_regime_by_date = dict(mr.regime_series(taiex))

    benchmark_curves = {}
    for symbol in BENCHMARKS:
        curve, _price, resolved_ticker = _require_cached_benchmark(symbol)
        benchmark_curves[symbol] = curve
        print(f"{symbol} ({resolved_ticker}): {len(curve)} bars, {curve[0]['date']}..{curve[-1]['date']}")

    shared_ranks_cache = {}
    rows = []
    for candidate in rvb.CANDIDATES:
        params = {"top_n": candidate["top_n"], "ranking_weights": candidate["ranking_weights"], "lot_size": 0}
        sim = pb.simulate(universe_data, params=params, ranks_cache=shared_ranks_cache)
        equity_curve = bm.value_curve(sim["equity_curve"])
        print(f"[{candidate['id']}] n_trades={len(sim['trades'])} first_equity_date={equity_curve[0]['date'] if equity_curve else None}")

        for symbol in BENCHMARKS:
            bucket_result = bm.regime_bucket_comparison(equity_curve, benchmark_curves[symbol], raw_regime_by_date)
            weighted, total_n, shares = day_weighted_annual_excess(bucket_result)
            rows.append({
                "candidate": candidate["id"], "benchmark": symbol,
                "bucket_result": bucket_result, "weighted": weighted,
                "total_n": total_n, "shares": shares,
            })

    print()
    print("=== 逐候選逐盤勢：年化超額（annualized excess）與日數占比 ===")
    for r in rows:
        print(f"\n[{r['candidate']} vs {r['benchmark']}]  (n_days={r['total_n']})")
        for bucket in BUCKETS:
            b = r["bucket_result"][bucket]
            if b["n_days"] == 0:
                print(f"  {bucket:<14} n=0（無資料）")
                continue
            share = r["shares"][bucket] * 100
            print(f"  {bucket:<14} n={b['n_days']:<4} 占比={mt.fmt_pct(share, 6)}  "
                  f"年化超額={mt.fmt_pct(b['excess_annualized_pct'], 8)}  "
                  f"(candidate年化={mt.fmt_pct(b['a_annualized_pct'], 8)}, benchmark年化={mt.fmt_pct(b['b_annualized_pct'], 8)})")
        print(f"  --> 日數加權預期年化超額 = {mt.fmt_pct(r['weighted'], 8)}/年")

    print()
    print("=== 彙總表 ===")
    print(f"{'candidate':<18}{'benchmark':>10}{'bull%':>8}{'bear%':>8}{'consol%':>9}{'日數加權年化超額':>18}")
    for r in rows:
        s = r["shares"]
        print(f"{r['candidate']:<18}{r['benchmark']:>10}"
              f"{mt.fmt_pct(s.get('bull', 0) * 100, 7)}{mt.fmt_pct(s.get('bear', 0) * 100, 7)}{mt.fmt_pct(s.get('consolidation', 0) * 100, 8)}"
              f"{mt.fmt_pct(r['weighted'], 18)}")


if __name__ == "__main__":
    main()
