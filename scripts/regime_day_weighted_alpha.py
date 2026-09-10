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
CANDIDATES / fetch_benchmark_curves() (cache-only here, no refresh -- both
cache/benchmark_0050.json and cache/benchmark_0051.json already exist from
prior runs this session), src/portfolio_backtest.simulate(), and
src/benchmark.regime_bucket_comparison() (called exactly like
scripts/report_vs_benchmark.py:170 does for its "pure" line).

Usage:
    python scripts/regime_day_weighted_alpha.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import benchmark as bm  # noqa: E402
import data_sources as ds  # noqa: E402
import market_regime as mr  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report_vs_benchmark as rvb  # noqa: E402
import report_factor_ic as rfi  # noqa: E402

BENCHMARKS = ["0050", "0051"]
BUCKETS = ("bull", "bear", "consolidation")


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
        curve, _price = rvb.fetch_benchmark_curves(symbol=symbol)  # refresh=False -> cache-only, both already exist
        benchmark_curves[symbol] = curve
        print(f"{symbol}: {len(curve)} bars, {curve[0]['date']}..{curve[-1]['date']}")

    shared_ranks_cache = {}
    rows = []
    for candidate in rvb.CANDIDATES:
        params = {"top_n": candidate["top_n"], "ranking_weights": candidate["ranking_weights"], "lot_size": 0}
        sim = pb.simulate(universe_data, params=params, ranks_cache=shared_ranks_cache)
        equity_curve = rvb._value_curve(sim["equity_curve"])
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
            print(f"  {bucket:<14} n={b['n_days']:<4} 占比={share:5.1f}%  "
                  f"年化超額={b['excess_annualized_pct']:+7.1f}%  "
                  f"(candidate年化={b['a_annualized_pct']:+7.1f}%, benchmark年化={b['b_annualized_pct']:+7.1f}%)")
        w = r["weighted"]
        print(f"  --> 日數加權預期年化超額 = {w:+.2f}%/年" if w is not None else "  --> 無法計算（無有效盤勢資料）")

    print()
    print("=== 彙總表 ===")
    print(f"{'candidate':<18}{'benchmark':>10}{'bull%':>8}{'bear%':>8}{'consol%':>9}{'日數加權年化超額':>18}")
    for r in rows:
        s = r["shares"]
        w = r["weighted"]
        w_str = f"{w:+.2f}%" if w is not None else "N/A"
        print(f"{r['candidate']:<18}{r['benchmark']:>10}"
              f"{s.get('bull', 0) * 100:7.1f}%{s.get('bear', 0) * 100:7.1f}%{s.get('consolidation', 0) * 100:8.1f}%"
              f"{w_str:>18}")


if __name__ == "__main__":
    main()
