# -*- coding: utf-8 -*-
"""Run the squeeze-breakout daily-proxy event study exactly as pre-registered.

    python scripts/run_squeeze_backtest.py --prereg docs/squeeze_breakout_preregistration_2026-10.md

Refuses to touch real data until section 8 of the pre-registration is locked
(no blanks left, no unchecked boxes, the locked source files unchanged since the
locked commit). Signal rules live in src/squeeze_breakout.py; stop, entry, exit,
costs, control draw and statistics are the EP-TW study's code (ep_strategy,
pullback_backtest), and the loading / lock / printing helpers are imported from
run_ep_backtest.py -- this script only wires them together.

Universe = V2 (same load_universe() as the EP-TW study).
"""
import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

import ep_strategy as ep  # noqa: E402
import pullback_backtest as bt  # noqa: E402
import run_ep_backtest as epr  # noqa: E402
import squeeze_breakout as sq  # noqa: E402

DEFAULT_PREREG = os.path.join(BASE_DIR, "docs", "squeeze_breakout_preregistration_2026-10.md")
OUT_DIR = os.path.join(BASE_DIR, "logs")
# Files whose behaviour decides the result: a diff against the locked commit aborts the run.
LOCKED_FILES = ("src/squeeze_breakout.py", "src/ep_strategy.py", "src/pullback_backtest.py",
                "src/pullback_strategy.py", "src/finlab_factors.py", "scripts/run_ep_backtest.py", "scripts/run_squeeze_backtest.py")


def check_locked_strategy(commit):
    diff = epr.git("diff", "--quiet", commit, "--", *LOCKED_FILES)
    if diff.returncode != 0:
        raise SystemExit(f"{', '.join(LOCKED_FILES)} 與鎖定 commit {commit} 有差異，不跑。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prereg", default=DEFAULT_PREREG)
    ap.add_argument("--bootstrap-n", type=int, default=bt.BOOTSTRAP_N)
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "squeeze_backtest_result.json"))
    args = ap.parse_args()

    commit = epr.read_lock(args.prereg)
    check_locked_strategy(commit)
    head = epr.git("rev-parse", "HEAD").stdout.strip()
    print(f"預先登記：{os.path.relpath(args.prereg, BASE_DIR)}")
    print(f"目前 commit：{head}；鎖定 commit：{commit}；{', '.join(LOCKED_FILES)} 與鎖定 commit 無差異")
    print("股票池：V2（與 EP-TW 同一份 load_universe()）。回調研究讀 V1，兩者股票池不同，結果不可直接等同比較。")

    raw, adj, revenue, info = epr.load_universe()
    print(f"載入主快取 {info['main']} 檔 + 下市補回 ok {info['delisted_ok']} 檔 → 合併 {info['merged']} 檔；"
          f"非普通股 {info['not_common']} 檔剔除；o/h/l 或 c_adj 缺漏整檔排除 {len(info['incomplete'])} 檔；"
          f"可用 {info['tradable']} 檔；資料指紋 {info['digest']}")
    print(f"資料範圍 {min(b[0]['date'] for b in adj.values())} ~ {max(b[-1]['date'] for b in adj.values())}")

    strong = sq.strong_by_day(raw, adj)
    signals = sq.scan_signals(adj, strong)
    terminal = ep.terminal_codes(adj)
    signal_days = {s["date"] for s in signals}
    control_days = sq.control_pool(strong, signal_days)
    kinds = {k: sum(1 for s in signals if s["pattern"] == k) for k in ("inside", "squeeze", "both")}
    print(f"強勢池日數 {len(strong)}；訊號 {len(signals)} 筆（{len(signal_days)} 個訊號日；型態 {kinds}）；"
          f"視為下市的代碼 {len(terminal)} 檔（未出場時以最後收盤價出場）")

    def run(params=None):
        trades, stats = ep.run_trades(adj, signals, "ep", params, terminal=terminal)
        ctrl, cstats = ep.run_trades(adj, [], "control", params, control_days=control_days, terminal=terminal)
        return bt.dedup_monthly(trades), bt.dedup_monthly(ctrl), trades, stats, cstats

    primary, control, primary_all, pstats, cstats = run()
    print(f"\n== 主要設定（成本 {bt._cost_rate(ep.EP_PARAMS):.3%} 來回，停損下限 {ep.EP_PARAMS['min_stop_pct']:g}%）==")
    print(f"  成交統計：{pstats}；去重前 {len(primary_all)} 筆")
    print(f"  控制組統計：{cstats}")
    crit = bt.evaluate_criteria(primary, control, n=args.bootstrap_n)
    epr.print_criteria(crit)
    epr.describe("緊縮突破組", primary)
    epr.describe("控制組（同日強勢池）", control)
    print("  註：關卡 6（組合層：80/20 vs 0050 的 Sharpe／MDD）與領頭股寬度曝險開關屬組合層，本次不做，留待計畫書步驟 5。")

    print("\n== §4 只報告、不判定 ==")
    hold = ep.hold_distribution(primary)
    print(f"  持有期分布（根）：{hold['hold_bars']}；出場原因：{hold['exit_reason']}")
    widened = sum(1 for t in primary if t["stop_widened"])
    print(f"  停損被放寬到下限的比例：{widened}/{len(primary)}")
    groups = sq.pattern_groups(primary, signals, n=args.bootstrap_n)
    for name, label in (("inside", "inside day"), ("squeeze", "區間收斂"), ("both", "兩者皆是")):
        g = groups[name]
        if g.get("note"):
            print(f"  型態標籤 {label}: {g['n']} 筆，平均 net_r {epr.fmt(g['mean_net_r'])}（{g['note']}）")
        else:
            ci = g["ci"]
            print(f"  型態標籤 {label}: {g['n']} 筆，平均 net_r {epr.fmt(g['mean_net_r'])} "
                  f"CI [{epr.fmt(ci[0])}, {epr.fmt(ci[1])}]，勝率 {g['win_rate']:.1%}")
    sens = {}
    for label, params in epr.SENSITIVITIES:
        tr, ctrl, _, _, _ = run(params)
        c = bt.evaluate_criteria(tr, ctrl, n=args.bootstrap_n)
        sens[label] = c
        print(f"-- 敏感度 {label}")
        epr.print_criteria(c)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"commit": head, "locked_commit": commit, "universe": {k: v for k, v in info.items()},
                   "criteria": crit, "stats": pstats, "control_stats": cstats, "sensitivity": sens,
                   "patterns": groups, "hold": hold, "signals": signals, "trades": primary, "control": control}, f,
                  ensure_ascii=False, indent=1, default=str)
    print(f"\n完整結果：{os.path.relpath(args.out, BASE_DIR)}")


if __name__ == "__main__":
    main()
