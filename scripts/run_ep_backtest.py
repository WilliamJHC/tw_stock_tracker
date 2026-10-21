# -*- coding: utf-8 -*-
"""Run the EP-TW (episodic-pivot gap) daily-proxy event study exactly as pre-registered.

    python scripts/run_ep_backtest.py --prereg docs/ep_backtest_preregistration_2026-10.md

Refuses to touch real data until section 8 of the pre-registration is locked
(no blanks left, no unchecked boxes, src/ep_strategy.py and the pullback
modules it imports unchanged since the locked commit). All rules live in
src/ep_strategy.py; this script only loads, checks, runs and prints.

Universe = V2 of the delisted-survivorship study: cache/factor_run plus the
"ok" entries of cache/factor_run_delisted (built by the same
run_survivorship_study.load_delisted()/build_universe("main+ok")).
"""
import argparse
import json
import os
import re
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

import ep_strategy as ep  # noqa: E402
import pullback_backtest as bt  # noqa: E402
import report_factor_ic as rfi  # noqa: E402
import run_survivorship_study as rss  # noqa: E402

DEFAULT_PREREG = os.path.join(BASE_DIR, "docs", "ep_backtest_preregistration_2026-10.md")
OUT_DIR = os.path.join(BASE_DIR, "logs")
# Files whose behaviour decides the result: a diff against the locked commit aborts the run.
LOCKED_FILES = ("src/ep_strategy.py", "src/pullback_backtest.py", "src/pullback_strategy.py")

SENSITIVITIES = (                      # section 4: reported, never graded, never replace the main setting
    ("cost_0.585%", {"fee_discount": 1.0}),
    ("stop_floor_2%", {"min_stop_pct": 2.0}),
    ("stop_floor_4%", {"min_stop_pct": 4.0}),
)


def git(*args):
    return subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8")


def read_lock(prereg_path):
    """Parse section 8. Returns the locked commit hash; raises SystemExit with
    the reason when the section is missing or not fully filled in."""
    with open(prereg_path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^## 8\. 鎖定\s*$(.*)", text, flags=re.M | re.S)
    if not m:
        raise SystemExit("預先登記沒有 §8 鎖定節，不跑。")
    sec = m.group(1)
    if "______" in sec or re.search(r"^- \[ \]", sec, flags=re.M):
        raise SystemExit("§8 尚未鎖定（仍有空白或未勾選項目）。依 §8：使用者逐項確認並鎖定 → 才能跑回測。")
    commit = re.search(r"commit：\s*`?([0-9a-f]{7,40})`?", sec)
    if not commit:
        raise SystemExit("§8 讀不到鎖定 commit，不跑。")
    return commit.group(1)


def check_locked_strategy(commit):
    diff = git("diff", "--quiet", commit, "--", *LOCKED_FILES)
    if diff.returncode != 0:
        raise SystemExit(f"{', '.join(LOCKED_FILES)} 與鎖定 commit {commit} 有差異，不跑。")


def load_universe():
    """V2 universe -> (raw bars, adjusted bars, revenue series, info). Non-common
    codes (ETFs etc.) are dropped; a code with any bar lacking o/h/l/c_adj is
    excluded whole (same rule as the pullback study, no filling)."""
    main = rfi.load_cached_universe()
    delisted = rss.load_delisted()
    digest = rss.data_digest(main, delisted)
    merged = rss.build_universe("main+ok", main, delisted)
    raw, adj, revenue, incomplete, not_common = {}, {}, {}, [], []
    for code in sorted(merged):
        if not merged[code]["bars"]:
            continue
        if not ep.is_common_stock(code):
            not_common.append(code)
            continue
        bars = sorted(merged[code]["bars"], key=lambda b: b["date"])
        a = bt.adjust_cache_bars(bars)
        if a is None:
            incomplete.append(code)
            continue
        raw[code], adj[code], revenue[code] = bars, a, merged[code].get("revenue_series") or []
    info = {"main": len(main), "delisted_ok": sum(1 for e in delisted.values() if e["status"] == "ok"),
            "merged": len(merged), "tradable": len(adj), "not_common": len(not_common),
            "incomplete": incomplete, "digest": digest}
    return raw, adj, revenue, info


def fmt(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


def print_criteria(c):
    ok = lambda b: "OK" if b else "X"
    print(f"  關卡1 樣本量      去重後 {c['n']} 筆（需 ≥ {bt.MIN_TRADES}）  {ok(c['c1_sample'])}")
    print(f"  關卡2 每筆期望值  平均 net_r = {fmt(c['mean_net_r'])}（需 ≥ +{bt.MIN_MEAN_NET_R}）  {ok(c['c2_mean'])}")
    ci = c["ci"]
    print(f"  關卡3 顯著性      95% CI = [{fmt(ci[0]) if ci else 'n/a'}, {fmt(ci[1]) if ci else 'n/a'}]（下界需 > 0）  {ok(c['c3_ci'])}")
    d = c["diff"]
    print(f"  關卡4 勝過控制組  控制組 {c['n_control']} 筆，平均 {fmt(c['control_mean_net_r'])}；"
          f"差 {fmt(d[0]) if d else 'n/a'}，CI [{fmt(d[1]) if d else 'n/a'}, {fmt(d[2]) if d else 'n/a'}]  {ok(c['c4_vs_control'])}")
    print(f"  關卡5 時間穩定    {bt.SPLIT_DATE} 前 {c['half_n'][0]} 筆平均 {fmt(c['half_mean'][0])}；"
          f"之後 {c['half_n'][1]} 筆平均 {fmt(c['half_mean'][1])}  {ok(c['c5_halves'])}")
    print(f"  結論：{c['verdict']}")


def describe(label, trades):
    s = bt.summarize(trades)
    if not s["n"]:
        print(f"  {label}: 0 筆")
        return
    print(f"  {label}: {s['n']} 筆，平均 net_r {fmt(s['mean_net_r'])}，平均毛 R {fmt(s['mean_gross_r'])}，"
          f"勝率 {s['win_rate']:.1%}，平均持有 {s['mean_hold_bars']:.1f} 根，最大連虧 {s['max_losing_streak']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prereg", default=DEFAULT_PREREG)
    ap.add_argument("--bootstrap-n", type=int, default=bt.BOOTSTRAP_N)
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "ep_backtest_result.json"))
    args = ap.parse_args()

    commit = read_lock(args.prereg)
    check_locked_strategy(commit)
    head = git("rev-parse", "HEAD").stdout.strip()
    print(f"預先登記：{os.path.relpath(args.prereg, BASE_DIR)}")
    print(f"目前 commit：{head}；鎖定 commit：{commit}；{', '.join(LOCKED_FILES)} 與鎖定 commit 無差異")
    print("股票池：V2（cache/factor_run + cache/factor_run_delisted 的 ok 條目）。回調正式回測只讀 V1，"
          "兩者股票池不同，結果不可直接等同比較。")

    raw, adj, revenue, info = load_universe()
    print(f"載入主快取 {info['main']} 檔 + 下市補回 ok {info['delisted_ok']} 檔 → 合併 {info['merged']} 檔；"
          f"非普通股 {info['not_common']} 檔剔除；o/h/l 或 c_adj 缺漏整檔排除 {len(info['incomplete'])} 檔；"
          f"可用 {info['tradable']} 檔；資料指紋 {info['digest']}")
    print(f"資料範圍 {min(b[0]['date'] for b in adj.values())} ~ {max(b[-1]['date'] for b in adj.values())}")

    signals = ep.scan_signals(raw, adj, revenue=revenue)
    terminal = ep.terminal_codes(adj)
    control_days = ep.liquid_pool(raw, {s["date"] for s in signals})
    print(f"訊號 {len(signals)} 筆（{len(control_days)} 個訊號日）；視為下市的代碼 {len(terminal)} 檔（未出場時以最後收盤價出場）")

    def run(params=None):
        trades, stats = ep.run_trades(adj, signals, "ep", params, terminal=terminal)
        ctrl, cstats = ep.run_trades(adj, [], "control", params, control_days=control_days, terminal=terminal)
        return bt.dedup_monthly(trades), bt.dedup_monthly(ctrl), trades, stats, cstats

    primary, control, primary_all, pstats, cstats = run()
    print(f"\n== 主要設定（成本 {bt._cost_rate(ep.EP_PARAMS):.3%} 來回，停損下限 {ep.EP_PARAMS['min_stop_pct']:g}%）==")
    print(f"  成交統計：{pstats}；去重前 {len(primary_all)} 筆")
    print(f"  控制組統計：{cstats}")
    crit = bt.evaluate_criteria(primary, control, n=args.bootstrap_n)
    print_criteria(crit)
    describe("EP 組", primary)
    describe("控制組", control)
    print("  註：關卡 6（組合層：80/20 vs 0050 的 Sharpe／MDD）與領頭股寬度曝險開關屬組合層，本次不做，留待計畫書步驟 5。")

    print("\n== §4 只報告、不判定 ==")
    hold = ep.hold_distribution(primary)
    print(f"  持有期分布（根）：{hold['hold_bars']}；出場原因：{hold['exit_reason']}")
    widened = sum(1 for t in primary if t["stop_widened"])
    print(f"  停損被放寬到下限的比例：{widened}/{len(primary)}")
    catalyst = ep.catalyst_groups(primary, n=args.bootstrap_n)
    for label, name in (("yes", "營收年增≥30%"), ("no", "營收年增<30%"), ("na", "無營收資料")):
        g = catalyst[label]
        if g.get("note"):
            print(f"  催化劑標籤 {name}: {g['n']} 筆，平均 net_r {fmt(g['mean_net_r'])}（{g['note']}）")
        else:
            ci = g["ci"]
            print(f"  催化劑標籤 {name}: {g['n']} 筆，平均 net_r {fmt(g['mean_net_r'])} CI [{fmt(ci[0])}, {fmt(ci[1])}]，勝率 {g['win_rate']:.1%}")
    sens = {}
    for label, params in SENSITIVITIES:
        tr, ctrl, _, _, _ = run(params)
        c = bt.evaluate_criteria(tr, ctrl, n=args.bootstrap_n)
        sens[label] = c
        print(f"-- 敏感度 {label}")
        print_criteria(c)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"commit": head, "locked_commit": commit, "universe": {k: v for k, v in info.items()},
                   "criteria": crit, "stats": pstats, "control_stats": cstats, "sensitivity": sens,
                   "catalyst": catalyst, "hold": hold, "trades": primary, "control": control}, f,
                  ensure_ascii=False, indent=1, default=str)
    print(f"\n完整結果：{os.path.relpath(args.out, BASE_DIR)}")


if __name__ == "__main__":
    main()
