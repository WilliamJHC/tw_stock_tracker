# -*- coding: utf-8 -*-
"""Third-party cross-check of this project's self-built adj_factor pipeline
(docs/archive/backtest_plan.md Phase 1, validation item 5) against FinLab's
`etl:adj_close` dataset -- a free FinLab account can query it, giving an
independent back-adjusted close series to compare our own against without
paying for FinLab access.

Method: rather than compare absolute adjusted-price *levels* (FinLab's
"today" reference and ours don't necessarily agree, since FinLab's own
adjustment factor is computed from ITS full corporate-action history, not
truncated to what the free tier exposes), compare the RATIO
finlab_adj_close(date) / our_adj_close(date) across the overlapping date
range for each code. If both sides captured the exact same corporate
actions, this ratio is a flat constant -- any step within the window means
one side has an event the other doesn't (a real discrepancy worth
investigating), while a *uniform* offset (no steps) just reflects the two
series being anchored to different "today"s and is not evidence of error.

Usage:
    python scripts/verify_finlab_adj.py [--codes 2330,2317]

Requires FINLAB_API_TOKEN in .env or the environment (src/finlab_strategy.py
already documents how to get a free one). Read-only: makes no DB writes.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import db  # noqa: E402
import finlab_strategy  # noqa: E402
import pipeline as pl  # noqa: E402

STEP_THRESHOLD = 0.02  # matches build_adj_factors.py's cross-source warning bar


def _detect_ratio_steps(dates_sorted, ratio_by_date, threshold=STEP_THRESHOLD):
    steps = []
    prev = None
    for d in dates_sorted:
        r = ratio_by_date[d]
        if prev is not None and prev:
            step = r / prev
            if abs(step - 1.0) > threshold:
                steps.append((d, prev, r))
        prev = r
    return steps


def verify_code(conn, code, finlab_adj_close):
    bars = db.query_daily_bars(conn, code)
    if not bars:
        return None
    our_by_date = {
        b["date"]: b["c"] * (b["adj_factor"] if b["adj_factor"] is not None else 1.0)
        for b in bars
    }
    finlab_by_date = {
        ts.strftime("%Y-%m-%d"): v
        for ts, v in finlab_adj_close.items()
        if v == v  # drop NaN
    }
    common_dates = sorted(set(our_by_date) & set(finlab_by_date))
    if not common_dates:
        return {"code": code, "n_overlap": 0, "steps": []}

    ratio_by_date = {}
    for d in common_dates:
        ours = our_by_date[d]
        if ours:
            ratio_by_date[d] = finlab_by_date[d] / ours
    steps = _detect_ratio_steps(common_dates, ratio_by_date)
    return {
        "code": code, "n_overlap": len(common_dates),
        "date_range": (common_dates[0], common_dates[-1]),
        "steps": steps,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=str, default=None)
    args = ap.parse_args()
    codes = args.codes.split(",") if args.codes else [s["code"] for s in pl.load_watchlist()]

    print("登入 FinLab ...")
    finlab_strategy.login()
    from finlab import data

    print("下載 etl:adj_close（免費版，資料範圍會被縮短）...")
    adj_close = data.get("etl:adj_close")
    print(f"  FinLab 資料範圍：{adj_close.index.min().date()} ~ {adj_close.index.max().date()}")

    conn = db.connect()
    total_steps = 0
    for code in codes:
        if code not in adj_close.columns:
            print(f"{code}: FinLab 無此代碼，略過")
            continue
        result = verify_code(conn, code, adj_close[code])
        if result is None:
            print(f"{code}: DB 無日線資料，略過")
            continue
        if result["n_overlap"] == 0:
            print(f"{code}: 與 FinLab 無重疊日期，略過")
            continue
        n_steps = len(result["steps"])
        total_steps += n_steps
        lo, hi = result["date_range"]
        status = "OK" if n_steps == 0 else f"⚠ {n_steps} 個未解釋的階梯"
        print(f"{code}: 重疊 {result['n_overlap']} 天（{lo} ~ {hi}）-- {status}")
        for d, prev, r in result["steps"]:
            print(f"    {d}: ratio {prev:.4f} -> {r:.4f} ({(r / prev - 1) * 100:+.1f}%)")

    print(f"\n合計 {total_steps} 個未解釋的階梯（0 = 自建還原因子與 FinLab 在重疊區間完全一致）")


if __name__ == "__main__":
    main()
