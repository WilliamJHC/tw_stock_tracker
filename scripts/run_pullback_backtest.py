# -*- coding: utf-8 -*-
"""Run the pullback daily-proxy backtest exactly as pre-registered.

    .venv\\Scripts\\python scripts\\run_pullback_backtest.py --prereg docs\\pullback_backtest_preregistration_2026-10.md

Refuses to touch real data until section 8 of the pre-registration is locked
(no blanks left, `src/pullback_strategy.py` unchanged since the locked
commit, PULLBACK_PARAMS equal to section 2.2). All rules live in
src/pullback_backtest.py; this script only loads, checks, runs and prints.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

import pullback_backtest as bt  # noqa: E402
import pullback_strategy as ps  # noqa: E402

CACHE_DIR = os.path.join(BASE_DIR, "cache", "factor_run")
BENCH_PATH = os.path.join(BASE_DIR, "cache", "benchmark_0050_2012.json")
DEFAULT_PREREG = os.path.join(BASE_DIR, "docs", "pullback_backtest_preregistration_2026-10.md")
OUT_DIR = os.path.join(BASE_DIR, "logs")


def git(*args):
    return subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8")


def read_lock(prereg_path):
    """Parse section 8. Returns (locked_commit, excluded_codes); raises
    SystemExit with the reason when the section is not fully filled in."""
    with open(prereg_path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^## 8\. 鎖定\s*$(.*)", text, flags=re.M | re.S)
    if not m:
        raise SystemExit("預先登記沒有 §8 鎖定節，不跑。")
    sec = m.group(1)
    if "______" in sec or re.search(r"^- \[ \]", sec, flags=re.M):
        raise SystemExit("§8 尚未鎖定（仍有空白或未勾選項目）。依 §8：回補完成 → 鎖定 → 才能跑回測。")
    commit = re.search(r"commit：\s*`?([0-9a-f]{7,40})`?", sec)
    codes = re.search(r"排除清單[^：
]*：\s*([0-9A-Za-z、,，]+)", sec)
    if not commit or not codes:
        raise SystemExit("§8 讀不到鎖定 commit 或排除清單，不跑。")
    excluded = [c for c in re.split(r"[、,，\s]+", codes.group(1).strip()) if c]
    return commit.group(1), excluded


def check_locked_strategy(commit):
    diff = git("diff", "--quiet", commit, "--", "src/pullback_strategy.py")
    if diff.returncode != 0:
        raise SystemExit(f"src/pullback_strategy.py 與鎖定 commit {commit} 有差異，不跑。")
    d = bt.params_diff()
    if d:
        raise SystemExit("PULLBACK_PARAMS 與 §2.2 不一致，不跑：" + "; ".join(f"{k}: 登記={a} 現況={b}" for k, a, b in d))


def load_universe():
    raw, adj, incomplete = {}, {}, []
    for path in sorted(glob.glob(os.path.join(CACHE_DIR, "*.json"))):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("skip") or not data.get("bars"):
            continue
        code = os.path.splitext(os.path.basename(path))[0]
        bars = sorted(data["bars"], key=lambda b: b["date"])
        a = bt.adjust_cache_bars(bars)
        if a is None:
            incomplete.append(code)
            continue
        raw[code], adj[code] = bars, a
    return raw, adj, incomplete


def fmt(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


def print_criteria(c):
    ok = lambda b: "OK" if b else "X"
    print(f"  1 樣本量      去重後 {c['n']} 筆（需 ≥ {bt.MIN_TRADES}）                    {ok(c['c1_sample'])}")
    print(f"  2 每筆期望值  平均 net_r = {fmt(c['mean_net_r'])}（需 ≥ +{bt.MIN_MEAN_NET_R}）          {ok(c['c2_mean'])}")
    ci = c["ci"]
    print(f"  3 顯著性      95% CI = [{fmt(ci[0]) if ci else 'n/a'}, {fmt(ci[1]) if ci else 'n/a'}]（下界需 > 0）  {ok(c['c3_ci'])}")
    d = c["diff"]
    print(f"  4 勝過控制組  控制組 {c['n_control']} 筆，平均 {fmt(c['control_mean_net_r'])}；"
          f"差 {fmt(d[0]) if d else 'n/a'}，CI [{fmt(d[1]) if d else 'n/a'}, {fmt(d[2]) if d else 'n/a'}]  {ok(c['c4_vs_control'])}")
    print(f"  5 時間穩定    {bt.SPLIT_DATE} 前 {c['half_n'][0]} 筆平均 {fmt(c['half_mean'][0])}；"
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
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "pullback_backtest_result.json"))
    args = ap.parse_args()

    commit, excluded = read_lock(args.prereg)
    check_locked_strategy(commit)
    head = git("rev-parse", "HEAD").stdout.strip()
    print(f"預先登記：{os.path.relpath(args.prereg, BASE_DIR)}")
    print(f"目前 commit：{head}；鎖定時 pullback_strategy.py commit：{commit}；PULLBACK_PARAMS 與 §2.2 一致")
    print(f"排除清單（§3）：{', '.join(excluded)}")

    raw, adj, incomplete = load_universe()
    print(f"載入 {len(adj)} 檔；o/h/l 或 c_adj 有缺漏而整檔排除：{len(incomplete)} 檔")
    last_date = max(b[-1]["date"] for b in adj.values())
    first_date = min(b[0]["date"] for b in adj.values())
    print(f"資料範圍 {first_date} ~ {last_date}")

    def progress(k, n, day, n_sig):
        print(f"  掃描 {k}/{n} {day} 累計訊號 {n_sig}", flush=True)

    signals, strong_by_day = bt.scan_signals(raw, adj, progress=progress)
    kept_signals = [s for s in signals if s["code"] not in set(excluded)]
    signal_days = {s["date"] for s in kept_signals}
    control_days = {d: strong_by_day[d] for d in signal_days if d in strong_by_day}
    print(f"訊號 {len(signals)} 筆（排除清單後 {len(kept_signals)} 筆，{len(signal_days)} 個訊號日）")

    def run(mode, params=None, excl=excluded):
        trades, stats = bt.run_trades(adj, signals, mode, params, excluded=excl, control_days=control_days)
        return bt.dedup_monthly(trades), trades, stats

    primary, primary_all, pstats = run("pullback")
    control, _, _ = run("control")
    print(f"\n== 主要設定（stop_atr_mult={ps.PULLBACK_PARAMS['stop_atr_mult']}，已排除 §3 清單）==")
    print(f"  成交統計：{pstats}；去重前 {len(primary_all)} 筆")
    crit = bt.evaluate_criteria(primary, control, n=args.bootstrap_n)
    print_criteria(crit)
    describe("回調組", primary)
    describe("控制組", control)
    reasons = {}
    for t in primary:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    print(f"  出場原因分布：{reasons}")
    first = [t for t in primary if t["retest_first"]]
    describe("其中 first_retest（只報告）", first)

    if os.path.exists(BENCH_PATH):
        with open(BENCH_PATH, encoding="utf-8") as f:
            curve = [(r["date"], r["value"]) for r in json.load(f)["total_return_curve"]]
        ex = [bt.benchmark_return(curve, t["entry_date"], t["exit_date"]) for t in primary]
        ex = [x for x in ex if x is not None]
        if ex:
            print(f"  0050 同期持有平均報酬（只報告）：{sum(ex) / len(ex):.2%}（{len(ex)} 筆）")

    print("\n== §3 敏感度（只報告，不改判定）==")
    sens = {}
    for label, params, excl in (
        ("stop_atr_mult=0（只用支撐區停損）", {"stop_atr_mult": 0}, excluded),
        ("stop_atr_mult=1.0", {"stop_atr_mult": 1.0}, excluded),
        ("stop_atr_mult=0.5，含被排除的代碼", None, ()),
    ):
        tr, _, _ = run("pullback", params, excl)
        ctrl, _, _ = run("control", params, excl)
        c = bt.evaluate_criteria(tr, ctrl, n=args.bootstrap_n)
        sens[label] = c
        print(f"-- {label}")
        print_criteria(c)

    print("\n== §4.1 附錄 A：停損距離分組（描述性，不參與判定）==")
    groups = bt.stop_distance_groups(primary, n=args.bootstrap_n)
    for g, parts in groups.items():
        a = parts["all"]
        if a.get("note"):
            print(f"  {g}: {a['n']} 筆，平均 net_r {fmt(a['mean_net_r'])}（{a['note']}）")
            continue
        ci = a["ci"]
        print(f"  {g}: {a['n']} 筆，平均 net_r {fmt(a['mean_net_r'])} CI [{fmt(ci[0])}, {fmt(ci[1])}]，勝率 {a['win_rate']:.1%}，"
              f"平均毛 R {fmt(a['mean_gross_r'])}，zone/atr = {a['basis_share']['zone']:.0%}/{a['basis_share']['atr']:.0%}，"
              f"平均 ATR/成交價 {fmt(a['mean_atr_pct'], 2)}%")
        for basis in ("zone", "atr"):
            b = parts[basis]
            print(f"      {basis}: {b['n']} 筆，平均 net_r {fmt(b.get('mean_net_r'))}" + (f"（{b['note']}）" if b.get("note") else ""))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"commit": head, "locked_strategy_commit": commit, "excluded": excluded, "criteria": crit,
                   "sensitivity": sens, "stop_groups": groups, "trades": primary, "control": control}, f,
                  ensure_ascii=False, indent=1, default=str)
    print(f"\n完整結果：{os.path.relpath(args.out, BASE_DIR)}")


if __name__ == "__main__":
    main()
