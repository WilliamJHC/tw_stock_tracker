# -*- coding: utf-8 -*-
"""Phase 0 evaluation baseline: per-code strategy-vs-buy&hold report off the
existing backtest DB (10 years x 23 watchlist codes, no refetch, no
network). See docs/backtest_plan.md Phase 0 -- this is the "trustworthy
ruler" every later phase's numbers get compared against, so "vs buy&hold"
and exposure are the headline columns, not n_trades/win_rate.

Usage:
    python scripts/report_backtest.py [--run-id RUN_ID]

Without --run-id, uses the most recently computed strategy_runs run
(max(computed_at)) -- the same run backtest.STRATEGY_PARAMS' defaults
would have produced, per docs/backtest_plan.md Phase 0's expected values.
"""
import argparse
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import db  # noqa: E402
import metrics  # noqa: E402


def _latest_run_id(conn):
    row = conn.execute(
        "SELECT run_id FROM strategy_runs ORDER BY computed_at DESC LIMIT 1"
    ).fetchone()
    return row["run_id"] if row else None


def _fmt_pct(x, width=9):
    return f"{x:>{width}.1f}%" if x is not None else " " * (width - 1) + "-"


def build_report(conn, run_id):
    """Returns {"per_code": [...], "summary": {...}, "by_reason": [...]}."""
    runs = [dict(r) for r in conn.execute(
        "SELECT * FROM strategy_runs WHERE run_id = ? ORDER BY code", (run_id,)
    ).fetchall()]
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE run_id = ?", (run_id,)
    ).fetchall()]
    trades_by_code = {}
    for t in trades:
        trades_by_code.setdefault(t["code"], []).append(t)

    per_code = []
    total_hold_days = 0
    total_span_days = 0
    for r in runs:
        code_trades = trades_by_code.get(r["code"], [])
        hold_days = sum(t["hold_days"] for t in code_trades)
        span = len(db.query_daily_bars(conn, r["code"], r["start_date"], r["end_date"]))
        exposure = metrics.exposure_pct(hold_days, span)
        total_hold_days += hold_days
        total_span_days += span
        beat = (
            r["total_return"] is not None and r["buy_hold_return"] is not None
            and r["total_return"] > r["buy_hold_return"]
        )
        per_code.append({
            "code": r["code"],
            "total_return": r["total_return"],
            "buy_hold_return": r["buy_hold_return"],
            "exposure": exposure,
            "beat": beat,
            "n_trades": r["n_trades"],
        })

    strat_rets = [r["total_return"] for r in per_code if r["total_return"] is not None]
    bh_rets = [r["buy_hold_return"] for r in per_code if r["buy_hold_return"] is not None]
    beat_n = sum(1 for r in per_code if r["beat"])

    summary = {
        "n_codes": len(per_code),
        "mean_strategy_return": sum(strat_rets) / len(strat_rets) if strat_rets else None,
        "mean_buy_hold_return": sum(bh_rets) / len(bh_rets) if bh_rets else None,
        "beat_buy_hold": beat_n,
        "exposure": metrics.exposure_pct(total_hold_days, total_span_days),
    }

    by_reason = {}
    for t in trades:
        by_reason.setdefault(t["exit_reason"], []).append(t["ret_pct"])
    reason_rows = []
    for reason, rets in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        n = len(rets)
        wins = sum(1 for x in rets if x > 0)
        reason_rows.append({
            "reason": reason,
            "n": n,
            "win_rate": wins / n * 100,
            "mean": sum(rets) / n,
            "median": statistics.median(rets),
        })

    return {"per_code": per_code, "summary": summary, "by_reason": reason_rows}


def print_report(report):
    print(f"{'code':<8}{'strategy':>10}{'buy&hold':>10}{'exposure':>10}  beat")
    for r in report["per_code"]:
        mark = "*" if r["beat"] else ""
        print(
            f"{r['code']:<8}{_fmt_pct(r['total_return'])}{_fmt_pct(r['buy_hold_return'])}"
            f"{_fmt_pct(r['exposure'])}  {mark}"
        )

    s = report["summary"]
    print()
    print(f"mean strategy {s['mean_strategy_return']:.1f}%")
    print(f"mean buy&hold {s['mean_buy_hold_return']:.1f}%")
    print(f"beat {s['beat_buy_hold']}/{s['n_codes']}")
    print(f"exposure {s['exposure']:.1f}%")

    print()
    print(f"{'exit_reason':<16}{'n':>6}{'win':>8}{'mean':>10}{'median':>10}")
    for row in report["by_reason"]:
        print(
            f"{row['reason']:<16}{row['n']:>6}{row['win_rate']:>7.1f}%"
            f"{row['mean']:>9.2f}%{row['median']:>9.2f}%"
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None, help="strategy_runs run_id; defaults to the latest")
    args = ap.parse_args()

    conn = db.connect()
    run_id = args.run_id or _latest_run_id(conn)
    if not run_id:
        print("no strategy_runs found -- run scripts/run_finlab_backtest.py or similar first")
        sys.exit(1)

    report = build_report(conn, run_id)
    print(f"run_id: {run_id}")
    print()
    print_report(report)


if __name__ == "__main__":
    main()
