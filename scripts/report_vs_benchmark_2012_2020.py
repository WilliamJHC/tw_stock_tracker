# -*- coding: utf-8 -*-
"""One-off window slice: does any of the 4 existing candidate strategies
(see scripts/report_vs_benchmark.py's CANDIDATES) beat 0050 (含息) over
2012-01-01..2020-12-31 -- the stretch scripts/report_vs_benchmark.py /
docs/regime_switch_vs_0050.md never covered, because that report's 0050
benchmark cache (cache/benchmark_0050.json) only went back to 2021-07-01
and cache/factor_run/*.json's fundamentals used to only go back to 2021.

Both gaps are now closed: cache/factor_run/*.json was long-history-merged
back to ~2012-05 for 1708 codes (see docs/dev_plan_data_integrity.md
priority 5), and this script fetches its OWN longer 0050 cache (separate
file, does not touch cache/benchmark_0050.json which
scripts/report_vs_benchmark.py owns and still needs at its own
start_date="2021-07-01").

Deliberately reuses, rather than reimplements:
  - scripts/report_vs_benchmark.py's CANDIDATES list and
    fetch_benchmark_curves() (called here with an earlier start_date and a
    separate cache_path so the two scripts never fight over one file).
  - scripts/report_factor_ic.py's load_cached_universe() for the merged
    universe.
  - src/portfolio_backtest.py's simulate() with its own PORTFOLIO_PARAMS
    defaults (initial_capital=1,000,000 / lot_size=0 -- NOT overridden
    here).
  - src/benchmark.py's full_period_comparison() for the windowed return.

pure-only (no switch-rule replay): the question this script answers is
"which candidate wins outright," not a regime-conditional allocation rule.

Usage:
    python scripts/report_vs_benchmark_2012_2020.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import benchmark as bm  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report_vs_benchmark as rvb  # noqa: E402
import report_factor_ic as rfi  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_START = "2012-01-01"
WINDOW_END = "2020-12-31"
# Separate cache file from scripts/report_vs_benchmark.py's
# cache/benchmark_0050.json (which is pinned to start_date="2021-07-01") --
# see module docstring.
BENCHMARK_CACHE_PATH = os.path.join(BASE_DIR, "cache", "benchmark_0050_2012.json")
BENCHMARK_START_DATE = "2012-01-01"

# --- Discovered data artifact (2026-09-10), not a real corporate action ---
# yfinance's 0050.TW history, when pulled back this far, has EVERY date
# before 2014-01-02 sitting on a ~4x-too-high scale relative to every date
# from 2014-01-02 onward -- both the "Close" and "Adj Close" columns jump
# by the identical ratio (~0.2494) on exactly this one day, and nowhere
# else in the 2012-2026 series (checked: no other >8% single-day move in
# the whole curve). yf.Ticker("0050.TW").splits returns an EMPTY series --
# 0050 never split in 2014, only in 2025 (the real 1:4 split this module's
# sibling report already documents) -- so this is Yahoo's backend
# mis-dating that 2025 split's retroactive adjustment for data older than
# some internal cutoff, not a real 2014 event. Sanity check: the corrected
# 2013-12-31 Close (58.70 / 4 ~= 14.7) and the corrected 2020-12-31 Close
# (~30.6) both land in the right ballpark for 0050's real, publicly known,
# pre-2025-split price history; the UNcorrected 2013-12-31 Close (58.70)
# does too for the same date. Only the boundary is wrong, not either side.
# ARTIFACT_BOUNDARY_DATE: first date already on the correct (post-artifact)
# scale. Every date strictly before it gets multiplied by the correction
# factor measured directly off this curve at the boundary (not a hardcoded
# 0.25 -- using the exact measured ratio keeps the curve continuous across
# the boundary instead of leaving a small residual jump).
ARTIFACT_BOUNDARY_DATE = "2014-01-02"
ARTIFACT_PRECEDING_DATE = "2013-12-31"


def _fix_yfinance_0050_scale_artifact(curve):
    """Returns (corrected_curve, correction_factor or None). See the
    ARTIFACT_* constants' comment above for what this corrects and why.
    Returns the curve UNCHANGED (factor None) if the expected boundary
    dates aren't both present, rather than guessing -- this is a narrow,
    verified, one-off patch for this exact observed artifact, not a
    general-purpose split detector."""
    by_date = _as_map(curve)
    before_val = by_date.get(ARTIFACT_PRECEDING_DATE)
    after_val = by_date.get(ARTIFACT_BOUNDARY_DATE)
    if before_val is None or after_val is None:
        return curve, None
    factor = after_val / before_val
    if not (0.15 <= factor <= 0.35):
        # Doesn't look like the artifact this was written for (expected
        # ~0.2494) -- leave the curve alone rather than mis-apply a fix.
        return curve, None
    corrected = [
        {"date": row["date"], "value": row["value"] * factor if row["date"] < ARTIFACT_BOUNDARY_DATE else row["value"]}
        for row in curve
    ]
    return corrected, factor


def _as_map(curve):
    return {row["date"]: row["value"] for row in curve}


def main():
    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")
    if not universe_data:
        print("nothing cached -- run scripts/run_factor_backtest.py --universe all first")
        sys.exit(1)

    print(f"抓取 0050 基準線（yfinance, start_date={BENCHMARK_START_DATE}） ...")
    try:
        total_return_curve, _price_curve = rvb.fetch_benchmark_curves(
            start_date=BENCHMARK_START_DATE, cache_path=BENCHMARK_CACHE_PATH,
        )
    except Exception as e:
        print(f"卡點：0050 基準線抓取失敗 -- {e!r}")
        sys.exit(1)
    print(f"  {len(total_return_curve)} bars, {total_return_curve[0]['date']}..{total_return_curve[-1]['date']}")

    total_return_curve, artifact_factor = _fix_yfinance_0050_scale_artifact(total_return_curve)
    if artifact_factor is not None:
        print(f"  [資料異常修正] yfinance 0050 歷史在 {ARTIFACT_PRECEDING_DATE}->{ARTIFACT_BOUNDARY_DATE} 有一個"
              f" ~{artifact_factor:.4f} 倍的尺度斷點（Close/Adj Close 同步出現，yf.Ticker('0050.TW').splits 為空，"
              f"非真實除權息事件）。已將 {ARTIFACT_BOUNDARY_DATE} 之前所有日期乘上此係數以消除斷點，"
              f"詳見腳本內 ARTIFACT_* 常數註解。")
    else:
        print("  [資料異常修正] 未偵測到已知的尺度斷點（邊界日期缺失或比例不符預期），曲線未修改。")

    # Cross-check comes AFTER the candidates below, once we know the actual
    # common-day window full_period_comparison() used (it clips to common
    # trading days with each candidate's equity curve, which can start
    # later than WINDOW_START -- see the candidates loop) -- computing the
    # direct 0050 return over a DIFFERENT (wider, benchmark-only) window
    # and comparing it to full_period_comparison()'s b_ret_pct would be an
    # apples-to-oranges mismatch, not a real cross-check.
    bench_by_date = _as_map(total_return_curve)

    shared_ranks_cache = {}
    rows = []
    for candidate in rvb.CANDIDATES:
        params = {"top_n": candidate["top_n"], "ranking_weights": candidate["ranking_weights"]}
        t0 = time.time()
        sim = pb.simulate(universe_data, params=params, ranks_cache=shared_ranks_cache)
        elapsed = time.time() - t0
        equity_curve = rvb._value_curve(sim["equity_curve"])
        n_trades = len(sim["trades"])
        first_date = equity_curve[0]["date"] if equity_curve else None
        print(f"[{candidate['id']}] {elapsed:.1f}s  n_trades={n_trades}  first_equity_date={first_date}")

        cmp_result = bm.full_period_comparison(equity_curve, total_return_curve, start=WINDOW_START, end=WINDOW_END)
        if not cmp_result:
            rows.append({"id": candidate["id"], "cmp": None, "first_date": first_date})
            print(f"  [{candidate['id']}] 無法計算：窗口內共同交易日不足 2 天（equity curve 起點 {first_date}）")
            continue
        rows.append({"id": candidate["id"], "cmp": cmp_result, "first_date": first_date})

    print()
    print("=== 2012-01-01 ~ 2020-12-31 窗口總報酬 vs 0050（含息） ===")
    print(f"{'candidate':<18}{'window':>24}{'total':>10}{'0050':>10}{'excess':>10}  beat")
    for r in rows:
        c = r["cmp"]
        if c is None:
            print(f"{r['id']:<18}{'(無法計算)':>24}")
            continue
        mark = "*" if c["beat"] else ""
        window_str = f"{c['start']}..{c['end']}"
        print(f"{r['id']:<18}{window_str:>24}{c['a_ret_pct']:>9.1f}%{c['b_ret_pct']:>9.1f}%{c['excess_pct']:>9.1f}%  {mark}")

    # Cross-check: for each candidate that DID produce a comparison, redo
    # 0050's start/end value ratio directly off total_return_curve, using
    # the EXACT [c["start"], c["end"]] window full_period_comparison()
    # reported for that candidate -- both start/end are common trading
    # days, so both are guaranteed present directly in the benchmark curve.
    print("\n交叉驗證（每個候選各自的實際窗口，0050 直接算 vs full_period_comparison b_ret_pct）：")
    for r in rows:
        c = r["cmp"]
        if c is None:
            continue
        b0, b1 = bench_by_date.get(c["start"]), bench_by_date.get(c["end"])
        if b0 is None or b1 is None:
            print(f"  {r['id']}：0050 曲線缺 {c['start']} 或 {c['end']}，無法交叉驗證")
            continue
        direct_ret_pct = (b1 / b0 - 1) * 100
        diff = abs(direct_ret_pct - c["b_ret_pct"])
        print(f"  {r['id']}（{c['start']}..{c['end']}）：直接算 {direct_ret_pct:.4f}% vs b_ret_pct {c['b_ret_pct']:.4f}%"
              f"（差 {diff:.6f} 個百分點）")


if __name__ == "__main__":
    main()
