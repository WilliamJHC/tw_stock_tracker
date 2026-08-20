# -*- coding: utf-8 -*-
"""CLI entry point for the backtest database (docs/archive/backtest_plan.md).

Implements Phase 2 (data import), Phase 3 (signal win-rate stats), and
Phase 4 (score replay + strategy simulation).
"""
import argparse
import datetime as dt
import json
import time

import adjust
import backtest as bt
import data_sources as ds
import db
import pipeline as pl

# 3650 calendar days ~= 10y of bars; analysis needs a 120-bar warmup
# (ma60 + volume_profile lookback) before it can score a day, so to get a
# full 10y of *scored* days the raw daily pull has to reach back further.
# See docs/archive/backtest_plan.md Phase 2 "抓多久".
DAILY_DAYS = 3900
CHIP_DAYS = 3650

_CHIP_COLUMNS = (
    "foreign_net", "trust_net", "margin_balance", "margin_delta",
    "short_balance", "short_delta", "foreign_ratio", "foreign_shares",
    "sbl_balance", "sbl_sales", "sbl_returns",
)


def _adjusted_bars(bars):
    """db.query_daily_bars()'s raw rows -> split/dividend-adjusted o/h/l/c
    (docs/archive/backtest_plan.md Phase 1c's downstream rule: momentum/
    win-rate/strategy simulation compare price *levels* across time, so
    they must run on the adjusted series, not the raw one that jumps at
    every split/stock-dividend ex-date). `v` is left untouched -- see
    adjust.py's module docstring.

    adj_factor is NULL for a bar scripts/build_adj_factors.py hasn't
    processed yet. If EVERY bar is NULL (the batch job has never run for
    this code), defaulting all of them to 1.0 is a consistent no-op
    (today's unadjusted behavior). But if only SOME bars are NULL -- e.g.
    --refresh-data just appended new dates after a fresh split, and the
    batch job hasn't rerun to push that split's ratio back through the
    older, already-computed bars -- treating the NULL tail as 1.0 while
    the stale non-NULL older bars still carry their pre-split factor would
    silently create a phantom price jump right at the seam (the older
    factors are wrong, not the NULL ones). Raise instead of guessing;
    callers should skip this code and prompt a build_adj_factors.py rerun."""
    raw_factors = [b.get("adj_factor") for b in bars]
    has_null = any(f is None for f in raw_factors)
    has_value = any(f is not None for f in raw_factors)
    if has_null and has_value:
        raise ValueError(
            f"{bars[0]['code']}: adj_factor 只有部分算過（可能是 --refresh-data 補了新日期，"
            "但 scripts/build_adj_factors.py 還沒重跑）——還原股價會在新舊資料接縫處出現假跳空，"
            "先跑 scripts/build_adj_factors.py 再重試"
        )
    factors = [f if f is not None else 1.0 for f in raw_factors]
    return [
        {**b, "o": b["o_adj"], "h": b["h_adj"], "l": b["l_adj"], "c": b["c_adj"]}
        for b in adjust.apply_adjustment(bars, factors)
    ]


def _daily_bar_rows(daily):
    """data_sources.get_daily()'s {t,o,h,l,c,v,source} -> db.upsert_daily_bars() rows."""
    rows = []
    for i, ts in enumerate(daily["t"]):
        rows.append({
            "date": dt.datetime.fromtimestamp(ts).date().isoformat(),
            "o": daily["o"][i], "h": daily["h"][i], "l": daily["l"][i],
            "c": daily["c"][i], "v": daily["v"][i], "source": daily["source"],
        })
    return rows


def _chip_rows(code):
    """Merge the four chip getters into one wide table keyed by date, per
    the chip_daily schema (docs/archive/backtest_plan.md Phase 1). A getter
    returning None (e.g. 0050 has no institutional/margin data as an ETF --
    see Phase 2) just leaves those columns blank for the dates it would
    have covered; chip_analysis() already handles missing components by
    renormalizing weights over what's available."""
    inst = ds.get_institutional(code, days=CHIP_DAYS, cache=False) or []
    margin = ds.get_margin(code, days=CHIP_DAYS, cache=False) or []
    shareholding = ds.get_shareholding(code, days=CHIP_DAYS, cache=False) or []
    sbl = ds.get_sbl(code, days=CHIP_DAYS, cache=False) or []

    by_date = {}
    for r in inst:
        by_date.setdefault(r["date"], {})["foreign_net"] = r["foreign"]
        by_date[r["date"]]["trust_net"] = r["trust"]
    for r in margin:
        slot = by_date.setdefault(r["date"], {})
        slot["margin_balance"] = r["margin_balance"]
        slot["margin_delta"] = r["margin_delta"]
        slot["short_balance"] = r["short_balance"]
        slot["short_delta"] = r["short_delta"]
    for r in shareholding:
        slot = by_date.setdefault(r["date"], {})
        slot["foreign_ratio"] = r["foreign_ratio"]
        slot["foreign_shares"] = r["foreign_shares"]
    for r in sbl:
        slot = by_date.setdefault(r["date"], {})
        slot["sbl_balance"] = r["sbl_balance"]
        slot["sbl_sales"] = r["sbl_sales"]
        slot["sbl_returns"] = r["sbl_returns"]

    return [
        {"date": date, **{col: by_date[date].get(col) for col in _CHIP_COLUMNS}}
        for date in sorted(by_date)
    ]


def _split_chip_rows(chip_rows):
    """Un-merge chip_daily's wide rows back into the four getter-shaped lists
    analyze()/chip_analysis() expect (inverse of _chip_rows() above). A row
    missing a given source's fields (e.g. an ETF with no margin data, or a
    date margin/shareholding/SBL hasn't published for yet -- see Phase 4's
    "籌碼延遲一個交易日" note) is simply excluded from that source's list."""
    inst = [{"date": r["date"], "foreign": r["foreign_net"], "trust": r["trust_net"]}
            for r in chip_rows if r["foreign_net"] is not None and r["trust_net"] is not None]
    margin = [{"date": r["date"], "margin_balance": r["margin_balance"],
               "margin_delta": r["margin_delta"], "short_balance": r["short_balance"],
               "short_delta": r["short_delta"]}
              for r in chip_rows if r["margin_balance"] is not None]
    shareholding = [{"date": r["date"], "foreign_ratio": r["foreign_ratio"],
                     "foreign_shares": r["foreign_shares"]}
                    for r in chip_rows if r["foreign_ratio"] is not None]
    sbl = [{"date": r["date"], "sbl_balance": r["sbl_balance"], "sbl_sales": r["sbl_sales"],
            "sbl_returns": r["sbl_returns"]}
           for r in chip_rows if r["sbl_balance"] is not None]
    return inst, margin, shareholding, sbl


def import_data(conn, codes):
    """Pull daily bars + chip data from FinMind (cache=False -- see
    data_sources.py's cache param docstring) and upsert into the DB.
    Per-code failures are logged and skipped so one bad code doesn't abort
    the whole watchlist (matches pipeline.build_watchlist_payloads()'s
    per-code try/except convention). Returns the list of failed codes."""
    failed = []
    for i, code in enumerate(codes, 1):
        print(f"[{i}/{len(codes)}] {code} ...", end=" ", flush=True)
        try:
            daily = ds.get_daily(code, days=DAILY_DAYS, cache=False)
            db.upsert_daily_bars(conn, code, _daily_bar_rows(daily))
            chip_rows = _chip_rows(code)
            if chip_rows:
                db.upsert_chip_daily(conn, code, chip_rows)
            print(f"ok（{len(daily['c'])} 根日線, {len(chip_rows)} 天籌碼）")
        except Exception as e:
            print(f"失敗: {e}")
            failed.append(code)
    return failed


def compute_signal_stats(conn, codes):
    """Compute + store signal_stats (Phase 3) for every code that already
    has daily bars in the DB. Codes never imported (no bars yet) are
    skipped with a message -- run with --refresh-data first."""
    computed_at = dt.datetime.now().isoformat()
    for code in codes:
        bars = db.query_daily_bars(conn, code)
        if not bars:
            print(f"  {code}: 尚無日線資料，略過（先跑 --refresh-data）")
            continue
        try:
            bars = _adjusted_bars(bars)
        except ValueError as e:
            print(f"  {e}，略過")
            continue
        daily = {"c": [r["c"] for r in bars], "v": [r["v"] for r in bars],
                  "o": [r["o"] for r in bars]}
        rows = bt.signal_win_rates(daily)
        for row in rows:
            row["computed_at"] = computed_at
        db.upsert_signal_stats(conn, code, rows)
        n_with_samples = sum(1 for r in rows if r["n"] > 0)
        print(f"  {code}: {len(rows)} 組訊號×天期（{n_with_samples} 組有樣本）")


def run_replay_and_simulate(conn, codes, run_id=None):
    """Phase 4: recompute daily scores bar-by-bar and run the strategy
    simulation for every code, writing score_history/strategy_runs/trades.
    Sets meta.last_run_at/last_run_id/params_json/of_excluded on success --
    the frontend's sole source for "回測資料更新於 X" (docs/archive/backtest_plan.md
    Phase 1), so this only happens once the full pipeline has actually run,
    not after the raw data import in Phase 2."""
    run_id = run_id or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n重算歷史分數與策略回測（run_id={run_id}）...")

    bars_by_code = {}
    for code in codes:
        bars = db.query_daily_bars(conn, code)
        if not bars:
            continue
        try:
            bars_by_code[code] = _adjusted_bars(bars)
        except ValueError as e:
            print(f"  {e}，略過")
    rs_by_code_date = bt.compute_rs_by_date(bars_by_code) if len(bars_by_code) >= 2 else {}
    if len(bars_by_code) < 2:
        print("  可用日線資料的股票不足 2 檔，RS 排名略過（其餘照常執行）")

    t0 = time.monotonic()
    for i, code in enumerate(codes, 1):
        bars = bars_by_code.get(code)
        if not bars:
            print(f"[{i}/{len(codes)}] {code}: 尚無日線資料，略過（先跑 --refresh-data）")
            continue

        chip_rows = db.query_chip_daily(conn, code)
        inst, margin, shareholding, sbl = _split_chip_rows(chip_rows)
        rs_by_date = rs_by_code_date.get(code, {})

        t1 = time.monotonic()
        score_rows = bt.replay_scores(bars, inst, margin, shareholding, sbl, rs_by_date=rs_by_date)
        elapsed = time.monotonic() - t1
        if not score_rows:
            print(f"[{i}/{len(codes)}] {code}: 日線不足 warmup 天數，無法重算分數，略過")
            continue
        db.upsert_score_history(conn, code, score_rows)

        sim = bt.simulate(score_rows, bars)
        db.upsert_strategy_run(conn, run_id, code, {
            "params_json": json.dumps(bt.STRATEGY_PARAMS, ensure_ascii=False),
            **sim["summary"],
            "computed_at": dt.datetime.now().isoformat(),
        })
        db.insert_trades(conn, run_id, code, sim["trades"])

        s = sim["summary"]
        tr = f"{s['total_return']:.1f}%" if s["total_return"] is not None else "無交易"
        print(f"[{i}/{len(codes)}] {code}: {len(score_rows)} 天分數（{elapsed:.1f}s）、"
              f"{s['n_trades']} 筆交易、總報酬 {tr}")

    total_elapsed = time.monotonic() - t0
    print(f"\n重算完成，共耗時 {total_elapsed:.1f} 秒（{total_elapsed / 60:.1f} 分鐘）")

    db.set_meta(conn, "last_run_at", dt.datetime.now().isoformat())
    db.set_meta(conn, "last_run_id", run_id)
    db.set_meta(conn, "params_json", json.dumps(bt.STRATEGY_PARAMS, ensure_ascii=False))
    # OF (orderFlow, 15/100 of SCORE_WEIGHTS) can't be reconstructed 2 years
    # back -- see docs/archive/backtest_plan.md Phase 4's "硬限制" section. Every
    # replayed score is scored as if OF contributed 0; build_suggestions()
    # (Phase 5) must check this flag to compare like with like.
    db.set_meta(conn, "of_excluded", "1")


def main():
    parser = argparse.ArgumentParser(description="建立/更新回測資料庫")
    parser.add_argument("--codes", type=str, default=None,
                         help="逗號分隔的股票代碼，預設為完整 watchlist")
    parser.add_argument("--refresh-data", action="store_true",
                         help="向 FinMind 重新抓取日線與籌碼資料；不加則直接讀現有 DB")
    args = parser.parse_args()

    codes = args.codes.split(",") if args.codes else [s["code"] for s in pl.load_watchlist()]

    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)

    if args.refresh_data:
        print(f"匯入 {len(codes)} 檔股票的日線（{DAILY_DAYS} 天）與籌碼（{CHIP_DAYS} 天）資料 ...")
        failed = import_data(conn, codes)
        if failed:
            print(f"\n{len(failed)}/{len(codes)} 檔匯入失敗: {', '.join(failed)}")
        else:
            print(f"\n{len(codes)} 檔全部匯入成功")
        print("提醒：--refresh-data 只會補上新日期的日線（adj_factor 預設 NULL），"
              "已存在日期的 adj_factor 會被保留（見 db.upsert_daily_bars docstring）。"
              "若這段期間有新的除權息，記得跑一次 scripts/build_adj_factors.py 補上新日期的還原因子。")
    else:
        print("未加 --refresh-data，不會呼叫 FinMind。目前 DB 內容：")
        for code in codes:
            bars = db.query_daily_bars(conn, code)
            print(f"  {code}: {len(bars)} 筆日線")

    print("\n計算訊號勝率統計 ...")
    compute_signal_stats(conn, codes)

    run_replay_and_simulate(conn, codes)

    conn.close()


if __name__ == "__main__":
    main()
