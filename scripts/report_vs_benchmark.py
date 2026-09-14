# -*- coding: utf-8 -*-
"""Does a fixed, event-driven "hold the multi-factor strategy, switch to
the benchmark on a pre-determined market-regime signal" rule actually beat
holding the benchmark outright -- see docs/regime_switch_vs_0050.md and
plans/0050-all-time-optimized-wolf.md (/grill-me, 2026-09-07) for the full
design (that design used 0050 specifically; --benchmark-symbol below lets
this same report run against any other yfinance-listed TW ticker, e.g. 0051,
to separate stock-picking alpha from a concentrated benchmark's composition
effect -- see fetch_benchmark_curves()'s docstring). No network for the
strategy side (off cache/factor_run/*.json, same source
scripts/report_factor_ic.py/run_strategy_search.py already share via
report_factor_ic.load_cached_universe()); one cached yfinance pull for the
benchmark itself (see fetch_benchmark_curves()).

This is intentionally NOT src/backtest.py's "does timing beat buy&hold on
ONE stock" question (already answered: no) -- it's a portfolio-level,
regime-conditional ALLOCATION question: given you're already running the
multi-factor strategy, is there an honest, no-lookahead rule for when to
step out of it into the benchmark instead. Two rules are tested, fixed in
advance (no search over rule variants):
  Rule A: hold the strategy in bull/consolidation, the benchmark in bear.
  Rule B: hold the strategy only in bull, the benchmark in bear/consolidation.

Usage:
    python scripts/report_vs_benchmark.py
    python scripts/report_vs_benchmark.py --lot-size 1000 --json-out out.json
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import benchmark as bm  # noqa: E402
import data_sources as ds  # noqa: E402
import market_regime as mr  # noqa: E402
import metrics as mt  # noqa: E402
import optimize as opt  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

# report_factor_ic.py lives in this same scripts/ directory, not src/ --
# same cross-script import convention scripts/run_strategy_search.py,
# scripts/run_ranking_optimize.py and scripts/propose_regime_weights.py
# already use for load_cached_universe()/RUN_CACHE_DIR, so this report
# reads the exact same on-disk cache instead of growing its own loader.
import report_factor_ic as rfi  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The 4 candidates this report compares -- top_n in {10, 20} x
# {上線組權重 (revenue .4/roe .3/momentum .3), 純動能對照}. Deliberately NOT
# low-volatility or dollar-volume proxy candidates (/grill-me ruled those
# out: no existing factor implementation to reuse, would need new code this
# report isn't meant to introduce). extra_factors is omitted (defaults to
# None) for all four, which is what lets them share one ranks_cache below --
# finlab_factors.factor_ranks() only varies with (universe_data, date,
# extra_factors), never with ranking_weights/top_n.
CANDIDATES = [
    {"id": "baseline_top10", "ranking_weights": None, "top_n": 10},
    {"id": "baseline_top20", "ranking_weights": None, "top_n": 20},
    {"id": "momentum_top10", "ranking_weights": {"momentum": 1.0}, "top_n": 10},
    {"id": "momentum_top20", "ranking_weights": {"momentum": 1.0}, "top_n": 20},
]

# hold_strategy_in sets for the two pre-registered switch rules -- see
# module docstring. Fixed here, not swept, on purpose.
SWITCH_RULES = {
    "rule_a": {"bull", "consolidation"},
    "rule_b": {"bull"},
}


# TW stock/ETF codes are 4-6 alphanumerics (e.g. "0050", "00891B") -- this
# is a filesystem-safety allowlist for --benchmark-symbol, not a real code
# validator: it exists only to keep the value out of cache_path's f-string
# (no "/", "\", "." -- blocks path traversal like "../../foo") before it
# ever reaches a yfinance-suffix guess, not to confirm the code trades.
_SYMBOL_RE = re.compile(r"^[0-9A-Za-z]{1,10}$")


def fetch_benchmark_curves(start_date="2021-07-01", cache_path=None, refresh=False, symbol="0050"):
    """Returns (total_return_curve, price_curve, resolved_ticker), where the
    first two are benchmark.py-shaped [{"date", "value"}] curves for `symbol`
    (default 0050) and resolved_ticker is the actual "<symbol>.TW"/".TWO"
    yfinance resolved to (see data_sources._yf_history_since()'s .attrs
    stamp -- without this, a report would have to guess the suffix, and
    guess wrong for any TPEX/OTC code that only resolves via .TWO).
    total_return_curve is yfinance's Adj Close (splits + dividends
    back-adjusted -- the honest "held it and reinvested everything"
    total-return line, and this report's MAIN benchmark); price_curve is
    Close (splits back-adjusted only, no dividends) kept as a stricter
    side-reference, since the strategy side has no dividend reinvestment of
    its own (cache/factor_run/*.json's c_adj coverage is 0/2148 as of this
    report's design -- see the plan's known limitations).

    Calls data_sources._yf_history_since() DIRECTLY -- same cross-module
    precedent as scripts/build_adj_factors.py's fetch_yahoo_adj_close().
    Deliberately NEVER data_sources.get_daily(symbol, ...): that shares its
    cache file (cache/daily_0050.json) with the running app's own 730-day
    frontend cache, and 0050's 2025 1:4 split produces a false cliff in
    FinMind's unadjusted price there (158.75 -> 106.2) -- see the plan's
    verification step. This function keeps its own cache file instead, and
    never touches cache/daily_0050.json.

    refresh=False (default) reuses cache_path as-is whenever it exists,
    matches this `symbol`, AND was fetched with the same start_date -- it
    does NOT check whether the cache is stale relative to today; pass
    --refresh-benchmark to force a re-fetch when you want today's latest
    bar. Raises ValueError for a `symbol` that fails _SYMBOL_RE (see its
    comment) and lets data_sources.SourceError propagate for a
    well-formed but unresolvable one -- callers should catch both."""
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"invalid --benchmark-symbol {symbol!r}: expected 1-10 alphanumeric characters")

    cache_path = cache_path or os.path.join(BASE_DIR, "cache", f"benchmark_{symbol}.json")
    if not refresh and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            # .get("symbol", "0050") -- a cache file written before --benchmark-symbol
            # existed never stored a "symbol" key at all, and always held 0050 data;
            # treating a missing key as implicit "0050" keeps such a cache valid
            # instead of forcing an unnecessary network re-fetch on first run after
            # upgrading (self-heals: the next write always stamps "symbol").
            if cached.get("symbol", "0050") == symbol and cached.get("start_date") == start_date:
                return cached["total_return_curve"], cached["price_curve"], cached.get("resolved_ticker", f"{symbol}.TW")
        except (OSError, ValueError, KeyError):
            pass

    df = ds._yf_history_since(symbol, start_date, "1d")
    resolved_ticker = df.attrs.get("resolved_ticker", f"{symbol}.TW")
    total_return_curve, price_curve = [], []
    for ts, row in df.iterrows():
        close, adj_close = row["Close"], row["Adj Close"]
        if close != close or adj_close != adj_close:  # NaN, same convention as data_sources._df_to_hist
            continue
        date = ts.date().isoformat()
        price_curve.append({"date": date, "value": float(close)})
        total_return_curve.append({"date": date, "value": float(adj_close)})

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({
            "start_date": start_date, "symbol": symbol, "resolved_ticker": resolved_ticker,
            "total_return_curve": total_return_curve, "price_curve": price_curve,
        }, f)
    return total_return_curve, price_curve, resolved_ticker


def _common_start(raw_regime_by_date, trade_dates):
    """max(first day TAIEX's regime is non-None, strategy's first trade
    date) -- see the plan's rationale: before this, either the regime
    signal or the strategy itself isn't defined yet, so a comparison
    including those days would be comparing against an undefined side."""
    first_regime_date = next((d for d, r in sorted(raw_regime_by_date.items()) if r is not None), None)
    first_trade_date = min(trade_dates) if trade_dates else None
    candidates = [d for d in (first_regime_date, first_trade_date) if d]
    return max(candidates) if candidates else None


def evaluate_candidate(candidate, sim, total_return_curve, raw_regime_by_date, eff_regime_by_date,
                        switch_cost_pct, window_months, train_range, test_range, min_win_rate_pct):
    """Runs both switch rules against one candidate's simulate() result and
    computes every comparison the report needs off the resulting three
    lines (pure/rule_a/rule_b) vs. the benchmark's total-return curve.
    Returns None if this candidate never traded or its trades start after
    the whole comparison window is exhausted (nothing to evaluate)."""
    equity_curve = bm.value_curve(sim["equity_curve"])
    trade_dates = [t["date"] for t in sim["trades"]]
    common_start = _common_start(raw_regime_by_date, trade_dates)
    if common_start is None or not equity_curve:
        return None

    lines = {"pure": {"curve": equity_curve, "n_switches": 0}}
    for rule_id, hold_in in SWITCH_RULES.items():
        result = bm.apply_switch_rule(
            equity_curve, total_return_curve, eff_regime_by_date, hold_strategy_in=hold_in,
            switch_cost_pct=switch_cost_pct, start_date=common_start,
        )
        lines[rule_id] = {"curve": bm.value_curve(result["equity_curve"], key="equity"), "n_switches": result["n_switches"]}

    train_start, train_end = train_range
    test_start, test_end = test_range
    comparisons, regime_buckets = {}, {}
    for line_id, line in lines.items():
        curve = line["curve"]
        comparisons[line_id] = {
            "full": bm.full_period_comparison(curve, total_return_curve, start=common_start),
            "rolling": bm.rolling_window_comparisons(curve, total_return_curve, window_months=window_months, start=common_start),
            "train": bm.full_period_comparison(curve, total_return_curve, start=max(common_start, train_start), end=train_end),
            "test": bm.full_period_comparison(curve, total_return_curve, start=max(common_start, test_start), end=test_end),
            "n_switches": line["n_switches"],
        }
        regime_buckets[line_id] = bm.regime_bucket_comparison(curve, total_return_curve, raw_regime_by_date)

    verdict = {
        rule_id: bm.adoption_verdict(
            comparisons[rule_id]["full"], comparisons[rule_id]["rolling"].get(12, {}), comparisons[rule_id]["test"],
            min_win_rate_pct=min_win_rate_pct,
        )
        for rule_id in SWITCH_RULES
    }

    return {
        "id": candidate["id"], "top_n": candidate["top_n"], "ranking_weights": candidate["ranking_weights"],
        "common_start": common_start, "portfolio_summary": sim["summary"],
        "comparisons": comparisons, "regime_buckets": regime_buckets, "verdict": verdict,
    }


# ------------------------------------------------------------- printing ----

def print_overview(results, benchmark_symbol="0050"):
    print("=== 總覽（全期間，較起點: 見各候選 common_start） ===")
    print(f"{'candidate':<18}{'line':<10}{'total':>10}{benchmark_symbol:>10}{'excess':>10}{'MDD':>9}{benchmark_symbol + ' MDD':>10}{'switch':>8}  beat")
    for r in results:
        for line_id in ("pure", "rule_a", "rule_b"):
            full = r["comparisons"][line_id]["full"]
            if not full:
                print(f"{r['id']:<18}{line_id:<10}{'(no data)':>10}")
                continue
            mark = "*" if full["beat"] else ""
            print(
                f"{r['id']:<18}{line_id:<10}{mt.fmt_pct(full['a_ret_pct'], 10)}{mt.fmt_pct(full['b_ret_pct'], 10)}"
                f"{mt.fmt_pct(full['excess_pct'], 10)}{mt.fmt_pct(full['a_mdd_pct'], 9)}{mt.fmt_pct(full['b_mdd_pct'], 10)}"
                f"{r['comparisons'][line_id]['n_switches']:>8}  {mark}"
            )
    print()


def print_rolling(results, window_months):
    for n in window_months:
        print(f"=== 滾動窗（{n} 個月） ===")
        print(f"{'candidate':<18}{'line':<10}{'windows':>8}{'beat':>6}{'win%':>8}{'median':>10}{'worst':>10}{'best':>10}")
        for r in results:
            for line_id in ("pure", "rule_a", "rule_b"):
                summary = r["comparisons"][line_id]["rolling"].get(n, {}).get("summary", {})
                if not summary:
                    print(f"{r['id']:<18}{line_id:<10}{'(no window)':>8}")
                    continue
                print(
                    f"{r['id']:<18}{line_id:<10}{summary['n_windows']:>8}{summary['n_beat']:>6}"
                    f"{summary['win_rate_pct']:>7.1f}%{summary['excess_median_pct']:>9.1f}%"
                    f"{summary['excess_min']['excess_pct']:>9.1f}%{summary['excess_max']['excess_pct']:>9.1f}%"
                )
        print()


def print_train_test(results, train_range, test_range, benchmark_symbol="0050"):
    print(f"=== Train/Test 分段（train {train_range[0]}..{train_range[1]}, test {test_range[0]}..{test_range[1]}） ===")
    print(f"{'candidate':<18}{'line':<10}{'seg':<6}{'total':>10}{benchmark_symbol:>10}{'excess':>10}  beat")
    for r in results:
        for line_id in ("pure", "rule_a", "rule_b"):
            for seg in ("train", "test"):
                cmp_result = r["comparisons"][line_id][seg]
                if not cmp_result:
                    print(f"{r['id']:<18}{line_id:<10}{seg:<6}{'(no data)':>10}")
                    continue
                mark = "*" if cmp_result["beat"] else ""
                print(
                    f"{r['id']:<18}{line_id:<10}{seg:<6}{mt.fmt_pct(cmp_result['a_ret_pct'], 10)}"
                    f"{mt.fmt_pct(cmp_result['b_ret_pct'], 10)}{mt.fmt_pct(cmp_result['excess_pct'], 10)}  {mark}"
                )
    print()


def print_regime_buckets(results, benchmark_symbol="0050"):
    print("=== 盤勢分桶（背景統計，原始未防抖分類；年化） ===")
    print(f"{'candidate':<18}{'line':<10}{'regime':<14}{'n_days':>7}{'ann':>10}{benchmark_symbol + ' ann':>10}{'excess ann':>11}")
    for r in results:
        for line_id in ("pure", "rule_a", "rule_b"):
            for regime in ("bull", "bear", "consolidation"):
                b = r["regime_buckets"][line_id][regime]
                if not b["n_days"]:
                    print(f"{r['id']:<18}{line_id:<10}{regime:<14}{0:>7}")
                    continue
                print(
                    f"{r['id']:<18}{line_id:<10}{regime:<14}{b['n_days']:>7}"
                    f"{mt.fmt_pct(b['a_annualized_pct'], 10)}{mt.fmt_pct(b['b_annualized_pct'], 10)}"
                    f"{mt.fmt_pct(b['excess_annualized_pct'], 11)}"
                )
    print()


def print_verdict(results, min_win_rate_pct, benchmark_symbol="0050"):
    print(f"=== 採用門檻判定（全期>{benchmark_symbol} AND 12個月滾動勝率>={min_win_rate_pct:.0f}% AND 留出測試期>{benchmark_symbol}） ===")
    print(f"{'candidate':<18}{'rule':<8}{'full':>6}{'rolling':>9}{'test':>6}  結論")
    any_pass = False
    for r in results:
        for rule_id in ("rule_a", "rule_b"):
            v = r["verdict"][rule_id]
            checks = v["checks"]
            conclusion = "支持切換" if v["pass"] else f"繼續抱 {benchmark_symbol}"
            any_pass = any_pass or v["pass"]
            print(
                f"{r['id']:<18}{rule_id:<8}"
                f"{'V' if checks['full_period_beats'] else 'x':>6}"
                f"{'V' if checks['rolling_12m_win_rate'] else 'x':>9}"
                f"{'V' if checks['test_period_beats'] else 'x':>6}  {conclusion}"
            )
    print()
    print("整體結論：" + ("至少一組候選×規則通過三項門檻，證據支持切換。" if any_pass else f"沒有任何候選×規則三項門檻同時通過 -- 繼續抱 {benchmark_symbol}。"))
    print()


def build_json_report(results, args, universe_data, total_return_curve, raw_regime_by_date, today_regime,
                       train_range, test_range, benchmark_symbol="0050", resolved_ticker=None):
    n_by_regime = {"bull": 0, "bear": 0, "consolidation": 0, "none": 0}
    for r in raw_regime_by_date.values():
        n_by_regime[r if r in n_by_regime else "none"] += 1

    return {
        "generated_at": dt.datetime.now().isoformat(),
        "params": {
            "lot_size": args.lot_size, "switch_cost_pct": args.switch_cost_pct,
            "window_months": args.window_months, "test_months": args.test_months,
            "min_win_rate_pct": args.min_win_rate,
        },
        "universe": {"n_codes": len(universe_data)},
        "benchmark": {
            "source": "yfinance", "ticker": resolved_ticker or f"{benchmark_symbol}.TW",
            "n_bars": len(total_return_curve),
            "start": total_return_curve[0]["date"] if total_return_curve else None,
            "end": total_return_curve[-1]["date"] if total_return_curve else None,
            "note": "total_return_curve uses Adj Close (含息); price_curve (Close, 不含息) is a side reference not repeated here",
        },
        "regime": {"n_days_by_regime_raw": n_by_regime, "today": today_regime},
        "train_range": list(train_range), "test_range": list(test_range),
        "candidates": [
            {
                "id": r["id"], "weights": r["ranking_weights"], "top_n": r["top_n"],
                "common_start": r["common_start"], "portfolio_summary": r["portfolio_summary"],
                "lines": {
                    line_id: {
                        "full": r["comparisons"][line_id]["full"],
                        "rolling": {str(n): r["comparisons"][line_id]["rolling"].get(n, {}) for n in args.window_months},
                        "train": r["comparisons"][line_id]["train"],
                        "test": r["comparisons"][line_id]["test"],
                        "n_switches": r["comparisons"][line_id]["n_switches"],
                    }
                    for line_id in ("pure", "rule_a", "rule_b")
                },
                "regime_buckets": r["regime_buckets"],
                "verdict": r["verdict"],
            }
            for r in results
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lot-size", type=int, default=0, help="0=零股（預設，見計畫的整張限制驗證）；1000=正式整張限制")
    ap.add_argument("--switch-cost-pct", type=float, default=0.7, help="每次切換扣的全額成本%%（估算值，見文件）")
    ap.add_argument("--window-months", type=str, default="12,36")
    ap.add_argument("--test-months", type=int, default=12)
    ap.add_argument("--min-win-rate", type=float, default=60.0)
    ap.add_argument("--refresh-benchmark", action="store_true")
    ap.add_argument("--benchmark-symbol", type=str, default="0050", help="基準標的代碼（預設 0050）；非 0050 時快取寫到 cache/benchmark_{symbol}.json，不覆蓋 cache/benchmark_0050.json")
    ap.add_argument("--json-out", type=str, default=None)
    args = ap.parse_args()
    args.window_months = tuple(int(x) for x in args.window_months.split(","))

    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")
    if not universe_data:
        print("nothing cached -- run scripts/run_factor_backtest.py --universe all first")
        sys.exit(1)

    print("抓取 TAIEX 日線並分類盤勢 ...")
    taiex = ds.get_index_daily("TAIEX")
    raw_series = mr.regime_series(taiex)
    eff_series = mr.effective_regime_series(raw_series)
    raw_regime_by_date = dict(raw_series)
    eff_regime_by_date = dict(eff_series)
    n_by_regime = {"bull": 0, "bear": 0, "consolidation": 0}
    for r in raw_regime_by_date.values():
        if r in n_by_regime:
            n_by_regime[r] += 1
    total_classified = sum(n_by_regime.values())
    print(f"  原始分類天數: {n_by_regime}"
          + (f"（空頭 {n_by_regime['bear'] / total_classified * 100:.1f}%）" if total_classified else ""))

    print(f"抓取 {args.benchmark_symbol} 基準線（yfinance） ...")
    try:
        total_return_curve, price_curve, resolved_ticker = fetch_benchmark_curves(
            refresh=args.refresh_benchmark, symbol=args.benchmark_symbol,
        )
    except (ValueError, ds.SourceError) as e:
        print(f"卡點：{args.benchmark_symbol} 基準線抓取失敗 -- {e}")
        sys.exit(1)
    if not total_return_curve:
        print(f"卡點：{args.benchmark_symbol} 基準線抓取成功但沒有可用資料點（可能整批 Close/Adj Close 都是 NaN）")
        sys.exit(1)
    print(f"  ({resolved_ticker}) {len(total_return_curve)} bars, {total_return_curve[0]['date']}..{total_return_curve[-1]['date']}")
    price_only_cmp = bm.full_period_comparison(price_curve, total_return_curve)
    if price_only_cmp:
        # Side reference only (not used anywhere else below): how much of
        # the benchmark's total return over this window came from dividends
        # alone -- the strategy side has no reinvested-dividend equivalent,
        # so this quantifies how much the main comparison already favors the
        # benchmark (see the plan's known-limitation #2).
        print(f"  {args.benchmark_symbol} 含息 vs 不含息（同期）：{price_only_cmp['b_ret_pct']:.1f}% vs {price_only_cmp['a_ret_pct']:.1f}%"
              f"（股息貢獻約 {price_only_cmp['b_ret_pct'] - price_only_cmp['a_ret_pct']:.1f} 個百分點）")

    calendar = pb._trading_calendar(universe_data)
    split = opt.train_test_split_by_calendar(calendar, test_months=args.test_months, purge_days=0)
    if split is None:
        print("資料量不足以切出 train/test 區段，中止")
        sys.exit(1)
    train_range, test_range = split["train"], split["test"]
    print(f"  train={train_range[0]}..{train_range[1]}  test={test_range[0]}..{test_range[1]}")

    shared_ranks_cache = {}
    results = []
    for candidate in CANDIDATES:
        params = {"top_n": candidate["top_n"], "ranking_weights": candidate["ranking_weights"], "lot_size": args.lot_size}
        t0 = time.time()
        sim = pb.simulate(universe_data, params=params, ranks_cache=shared_ranks_cache)
        elapsed = time.time() - t0
        summary = sim["summary"]
        print(f"[{candidate['id']}] {elapsed:.1f}s  total_return={summary.get('total_return_pct', float('nan')):.1f}%"
              f"  n_trades={len(sim['trades'])}")
        evaluated = evaluate_candidate(
            candidate, sim, total_return_curve, raw_regime_by_date, eff_regime_by_date,
            switch_cost_pct=args.switch_cost_pct, window_months=args.window_months,
            train_range=train_range, test_range=test_range, min_win_rate_pct=args.min_win_rate,
        )
        if evaluated is None:
            print(f"  [{candidate['id']}] 無足夠交易/盤勢資料可評估，略過")
            continue
        results.append(evaluated)

    print()
    print_overview(results, benchmark_symbol=args.benchmark_symbol)
    print_rolling(results, args.window_months)
    print_train_test(results, train_range, test_range, benchmark_symbol=args.benchmark_symbol)
    print_regime_buckets(results, benchmark_symbol=args.benchmark_symbol)
    print_verdict(results, args.min_win_rate, benchmark_symbol=args.benchmark_symbol)

    today_regime = mr.current_regime(taiex)
    if today_regime is None:
        print("今日盤勢：尚無足夠歷史可判斷")
    else:
        rule_a_holds = "策略" if today_regime in SWITCH_RULES["rule_a"] else args.benchmark_symbol
        rule_b_holds = "策略" if today_regime in SWITCH_RULES["rule_b"] else args.benchmark_symbol
        print(f"今日盤勢（防抖）{today_regime} → 規則 A 持有 {rule_a_holds}／規則 B 持有 {rule_b_holds}")

    if args.json_out:
        report = build_json_report(results, args, universe_data, total_return_curve, raw_regime_by_date,
                                    today_regime, train_range, test_range, benchmark_symbol=args.benchmark_symbol,
                                    resolved_ticker=resolved_ticker)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
