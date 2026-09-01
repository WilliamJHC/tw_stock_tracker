# -*- coding: utf-8 -*-
"""CLI driver for breakout_strategy.py -- read-only against the existing
backtest DB's daily_bars/chip_daily (populated by
`python src/run_backtest.py --codes ... --refresh-data`, run that first if
a code has no data yet). Does NOT write strategy_runs/trades -- this
strategy stays fully independent of backtest.py's engine (see
breakout_strategy.py's module docstring), so results go to JSON instead.

Usage:
    python scripts/run_breakout_backtest.py [--codes CODES] [--json-out PATH]

--codes: comma-separated codes; defaults to the full watchlist (same
    default run_backtest.py's --codes uses).
--json-out: defaults to cache/breakout_backtest/{run_id}.json.
"""
import argparse
import datetime as dt
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import breakout_strategy as bs  # noqa: E402
import db  # noqa: E402
import metrics  # noqa: E402
import pipeline as pl  # noqa: E402
import run_backtest as rb  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "breakout_backtest")


def _fmt_pct(x, width=9):
    return f"{x:>{width}.1f}%" if x is not None else " " * (width - 1) + "-"


def run_one(conn, code):
    """Returns a per-code result dict, or None if there's no usable data
    (mirrors run_backtest.py's compute_signal_stats()/run_replay_and_simulate()
    skip-with-message convention)."""
    bars = db.query_daily_bars(conn, code)
    if not bars:
        print(f"  {code}: 尚無日線資料，略過（先跑 python src/run_backtest.py --codes {code} --refresh-data）")
        return None
    try:
        bars = rb._adjusted_bars(bars)
    except ValueError as e:
        print(f"  {e}，略過")
        return None

    chip_rows = db.query_chip_daily(conn, code)
    inst, margin, _shareholding, _sbl = rb._split_chip_rows(chip_rows)

    rows = bs.replay_breakout(bars, inst, margin)
    if not rows:
        print(f"  {code}: 日線不足 warmup 天數，略過")
        return None

    sim = bs.simulate_breakout(rows, bars)
    hold_days = sum(t["hold_days"] for t in sim["trades"])
    span = len(bars)
    exposure = metrics.exposure_pct(hold_days, span)
    return {"code": code, **sim["summary"], "exposure": exposure, "trades": sim["trades"]}


def build_report(conn, codes):
    """Returns {"per_code": [...], "summary": {...}, "by_reason": [...]}."""
    per_code = []
    for code in codes:
        r = run_one(conn, code)
        if r:
            per_code.append(r)

    rets = [r["total_return"] for r in per_code if r["total_return"] is not None]
    bh_rets = [r["buy_hold_return"] for r in per_code if r["buy_hold_return"] is not None]

    all_trades = [t for r in per_code for t in r["trades"]]
    by_reason = {}
    for t in all_trades:
        by_reason.setdefault(t["exit_reason"], []).append(t["ret_pct"])
    reason_rows = []
    for reason, vals in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        n = len(vals)
        wins = sum(1 for x in vals if x > 0)
        reason_rows.append({"reason": reason, "n": n, "win_rate": wins / n * 100,
                             "mean": sum(vals) / n, "median": statistics.median(vals)})

    summary = {
        "n_codes": len(per_code),
        "mean_total_return": sum(rets) / len(rets) if rets else None,
        "mean_buy_hold_return": sum(bh_rets) / len(bh_rets) if bh_rets else None,
        "beat_buy_hold": sum(1 for r in per_code if r["total_return"] is not None
                              and r["buy_hold_return"] is not None
                              and r["total_return"] > r["buy_hold_return"]),
        "total_trades": len(all_trades),
    }
    return {"per_code": per_code, "summary": summary, "by_reason": reason_rows}


def print_report(report):
    print(f"{'code':<8}{'trades':>8}{'total_ret':>12}{'buy&hold':>12}{'exposure':>10}")
    for r in report["per_code"]:
        print(f"{r['code']:<8}{r['n_trades']:>8}{_fmt_pct(r['total_return'], 11)}"
              f"{_fmt_pct(r['buy_hold_return'], 11)}{_fmt_pct(r['exposure'])}")

    s = report["summary"]
    print()
    print(f"{s['n_codes']} 檔有結果，共 {s['total_trades']} 筆交易")
    if s["mean_total_return"] is not None:
        print(f"mean total_return {s['mean_total_return']:.1f}%")
    if s["mean_buy_hold_return"] is not None:
        print(f"mean buy&hold      {s['mean_buy_hold_return']:.1f}%")
    print(f"beat buy&hold {s['beat_buy_hold']}/{s['n_codes']}")

    if report["by_reason"]:
        print()
        print(f"{'exit_reason':<16}{'n':>6}{'win':>8}{'mean':>10}{'median':>10}")
        for row in report["by_reason"]:
            print(f"{row['reason']:<16}{row['n']:>6}{row['win_rate']:>7.1f}%"
                  f"{row['mean']:>9.2f}%{row['median']:>9.2f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--codes", type=str, default=None, help="逗號分隔的股票代碼，預設為完整 watchlist")
    ap.add_argument("--json-out", type=str, default=None, help="輸出 JSON 路徑，預設 cache/breakout_backtest/{run_id}.json")
    args = ap.parse_args()

    codes = args.codes.split(",") if args.codes else [s["code"] for s in pl.load_watchlist()]

    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"突破動能策略回測（run_id={run_id}，{len(codes)} 檔）...")
    report = build_report(conn, codes)
    print()
    print_report(report)

    out_path = args.json_out or os.path.join(OUT_DIR, f"{run_id}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    payload = {
        "run_id": run_id,
        "generated_at": dt.datetime.now().isoformat(),
        "params": bs.BREAKOUT_PARAMS,
        "per_code": report["per_code"],
        "summary": report["summary"],
        "by_reason": report["by_reason"],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n已寫入 {out_path}")


if __name__ == "__main__":
    main()
