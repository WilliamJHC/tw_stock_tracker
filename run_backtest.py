# -*- coding: utf-8 -*-
"""CLI entry point for the 2-year backtest database (docs/backtest_plan.md).

Currently implements Phase 2 (data import) and Phase 3 (signal win-rate
stats). Later phases (score replay, strategy simulation) extend this same
CLI without changing these steps.
"""
import argparse
import datetime as dt

import backtest as bt
import data_sources as ds
import db
import pipeline as pl

# 730 trading-day-ish calendar days ~= 1.5y of bars; analysis needs a 120-bar
# warmup (ma60 + volume_profile lookback) before it can score a day, so to
# get a full 2y of *scored* days the raw daily pull has to reach back further.
# See docs/backtest_plan.md Phase 2 "抓多久".
DAILY_DAYS = 1000
CHIP_DAYS = 730

_CHIP_COLUMNS = (
    "foreign_net", "trust_net", "margin_balance", "margin_delta",
    "short_balance", "short_delta", "foreign_ratio", "foreign_shares",
    "sbl_balance", "sbl_sales", "sbl_returns",
)


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
    the chip_daily schema (docs/backtest_plan.md Phase 1). A getter
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
        daily = {"c": [r["c"] for r in bars], "v": [r["v"] for r in bars],
                  "o": [r["o"] for r in bars]}
        rows = bt.signal_win_rates(daily)
        for row in rows:
            row["computed_at"] = computed_at
        db.upsert_signal_stats(conn, code, rows)
        n_with_samples = sum(1 for r in rows if r["n"] > 0)
        print(f"  {code}: {len(rows)} 組訊號×天期（{n_with_samples} 組有樣本）")


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

    if args.refresh_data:
        print(f"匯入 {len(codes)} 檔股票的日線（{DAILY_DAYS} 天）與籌碼（{CHIP_DAYS} 天）資料 ...")
        failed = import_data(conn, codes)
        if failed:
            print(f"\n{len(failed)}/{len(codes)} 檔匯入失敗: {', '.join(failed)}")
        else:
            print(f"\n{len(codes)} 檔全部匯入成功")
    else:
        print("未加 --refresh-data，不會呼叫 FinMind。目前 DB 內容：")
        for code in codes:
            bars = db.query_daily_bars(conn, code)
            print(f"  {code}: {len(bars)} 筆日線")

    print("\n計算訊號勝率統計 ...")
    compute_signal_stats(conn, codes)

    conn.close()


if __name__ == "__main__":
    main()
