# -*- coding: utf-8 -*-
"""CLI for src/optimize.py's purged walk-forward parameter sweep, off the
existing backtest DB (no network, no refetch -- same DB run_backtest.py
already populated). See docs/https-finlab-finance-blog-python-get-tai-
memoized-lighthouse.md Phase 5.

>>> Scope note <<<
This only wires up backtest.STRATEGY_PARAMS' single-code knobs
(exit_composite/atr_mult/max_hold_days), matching src/optimize.py's own
worked example. Optimizing finlab_factors.RANKING_WEIGHTS or
analysis.SCORE_WEIGHTS needs Phase 4's full-market rank-IC results first
(see src/optimize.py's module docstring on why) -- not implemented here.

Usage:
    python scripts/run_optimize.py --codes 2330,2317 --param exit_composite \
        --candidates -80,-70,-60,-50,-40,-30,-20,-10
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import db  # noqa: E402
import optimize as opt  # noqa: E402
import pipeline as pl  # noqa: E402
import run_backtest as rb  # noqa: E402


def _fmt(x, width=8, pct=True):
    if x is None:
        return " " * (width - 1) + "-"
    return f"{x:>{width}.1f}{'%' if pct else ''}"


def run_for_code(conn, code, param_name, candidates, n_folds, purge_bars, mode, base_params):
    bars = db.query_daily_bars(conn, code)
    if not bars:
        print(f"{code}: 尚無日線資料，略過")
        return None
    bars = rb._adjusted_bars(bars)
    chip_rows = db.query_chip_daily(conn, code)
    inst, margin, shareholding, sbl = rb._split_chip_rows(chip_rows)

    result = opt.sweep_param_walk_forward(
        bars, inst, margin, shareholding, sbl, rs_by_date=None,
        param_name=param_name, candidate_values=candidates, base_params=base_params,
        n_folds=n_folds, purge_bars=purge_bars, mode=mode,
    )
    if result is None:
        print(f"{code}: 資料量不足以切出至少一個 purged fold（n_folds={n_folds}, purge_bars={purge_bars}）")
        return None
    return result


def print_report(code, result):
    plateau = result["plateau"]
    point_best = result["point_best"]
    print(f"\n=== {code}: {result['param_name']} ===")
    if plateau:
        print(f"高原：{plateau['range']} 中心 {plateau['center']:+.1f}"
              f"（寬度佔搜尋範圍 {plateau['width_frac']:.0%}）")
    else:
        print("高原：偵測不到（資料點不足或全平）")
    print(f"各 fold argmax：{result['fold_argmax']}")
    warn = f"  ⚠ {point_best.get('warning')}" if point_best.get("warning") else ""
    print(f"全樣本 argmax：{point_best['value']} (score={_fmt(point_best['score'])}){warn}")
    print(f"DSR：{_fmt(result['dsr'], pct=False)}   PBO：{_fmt(result['pbo'], pct=False)}   "
          f"n_trials={result['n_trials']}")
    vbh = result["vs_buy_hold"]
    print(f"vs 買進持有：選定值 {vbh['chosen_value']} 的 OOS 平均報酬 {_fmt(vbh['chosen_oos_mean_return_pct'])} "
          f"vs 買進持有 {_fmt(vbh['buy_hold_return_pct'])}")
    decision = result["decision"]
    verdict = "採用" if decision["accept"] else "拒絕"
    print(f"判定：{verdict}" + (f"（{'; '.join(decision['reasons'])}）" if decision["reasons"] else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--codes", type=str, default=None, help="逗號分隔股票代碼，預設為完整 watchlist")
    ap.add_argument("--param", type=str, default="exit_composite",
                     choices=["exit_composite", "atr_mult", "max_hold_days"])
    ap.add_argument("--candidates", type=str, required=True, help="逗號分隔候選值")
    ap.add_argument("--n-folds", type=int, default=4)
    ap.add_argument("--purge-bars", type=int, default=opt.PURGE_BARS_DEFAULT)
    ap.add_argument("--mode", choices=["anchored", "rolling"], default="anchored")
    args = ap.parse_args()

    is_float = args.param != "max_hold_days"
    candidates = [float(c) if is_float else int(c) for c in args.candidates.split(",")]
    codes = args.codes.split(",") if args.codes else [s["code"] for s in pl.load_watchlist()]

    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_opt"

    next_trial_id = 0  # running counter across the whole --codes loop: optimization_runs'
    # PRIMARY KEY is (run_id, trial_id) with code as a plain non-key column, so a per-code
    # enumerate() restarting at 0 would let later codes' INSERT OR REPLACE silently overwrite
    # earlier codes' rows at the same key.
    for code in codes:
        result = run_for_code(conn, code, args.param, candidates, args.n_folds,
                              args.purge_bars, args.mode, base_params=None)
        if result is None:
            continue
        print_report(code, result)
        now = dt.datetime.now().isoformat()
        db.upsert_optimization_trials(conn, run_id, [
            {
                "trial_id": i, "code": code, "param_name": args.param, "fold_idx": t["fold_idx"],
                "params_json": json.dumps({args.param: t["value"]}), "score": t["score"], "computed_at": now,
            }
            for i, t in enumerate(result["trials"], start=next_trial_id)
        ])
        next_trial_id += len(result["trials"])

    print(f"\nrun_id={run_id}，每筆試驗（含被拒絕的候選值）已寫入 optimization_runs，"
          f"可用 db.count_optimization_trials(conn, run_id) 事後核對真實 n_trials。")


if __name__ == "__main__":
    main()
