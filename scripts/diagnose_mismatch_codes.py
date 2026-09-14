# -*- coding: utf-8 -*-
"""One-off diagnostic (read-only): for the 13 codes docs/dev_plan_data_
integrity.md's Priority-5 follow-up item #2 flagged as "verified against
the CURRENT adjustment logic but mismatching cache/factor_run_long/'s
stored c_adj" (cache/factor_run_long_verify.jsonl, n_mismatch > 0, error
is None), pin down WHERE the two diverge: recompute today's
resolve_adjustment_actions() for each code and compare its action dates
against the first date verify_factor_run_long_adjfactors.py recorded a
mismatch on.

Does not modify any cache. Prints a per-code report; nothing is persisted
except this script itself (findings go in docs/dev_plan_data_integrity.md
by hand after reading the output).

Usage:
    python scripts/diagnose_mismatch_codes.py
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import data_sources as ds  # noqa: E402

import build_adj_factors as baf  # noqa: E402
import run_factor_backtest as rfb  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFY_PATH = os.path.join(BASE_DIR, "cache", "factor_run_long_verify.jsonl")
LONG_DIR = os.path.join(BASE_DIR, "cache", "factor_run_long")


def load_mismatch_rows():
    rows = []
    with open(VERIFY_PATH, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row.get("error") is None and (row.get("n_mismatch") or 0) > 0:
                rows.append(row)
    return rows


def main():
    rows = load_mismatch_rows()
    codes = [r["code"] for r in rows]
    print(f"=== {len(codes)} 檔 mismatch 代碼：{codes} ===\n")

    universe = ds.get_universe(cache=True) or []
    market_by_code = {u["code"]: u["market"] for u in universe}
    first_bar_dates = []
    for c in codes:
        with open(os.path.join(LONG_DIR, f"{c}.json"), encoding="utf-8") as f:
            first_bar_dates.append(json.load(f)["bars"][0]["date"])
    start_year = int(min(first_bar_dates)[:4])
    years = range(start_year, dt.date.today().year + 1)
    print(f"抓取 TWSE 除權息表（{years[0]}-{years[-1]}）...")
    twse_by_code = rfb.fetch_twse_table(years, codes)

    for row in rows:
        code = row["code"]
        with open(os.path.join(LONG_DIR, f"{code}.json"), encoding="utf-8") as f:
            long_data = json.load(f)
        bars = long_data["bars"]
        market = market_by_code.get(code, "twse")
        twse_events = twse_by_code.get(code, [])

        actions, counters = baf.resolve_adjustment_actions(code, market, bars, twse_events)

        first_mismatch_date = row["mismatch_dates"][0]["date"] if row["mismatch_dates"] else None
        max_rel_diff = row["max_rel_diff"]

        print(f"--- {code}（market={market}, n_mismatch={row['n_mismatch']}, "
              f"max_rel_diff={max_rel_diff:.4f}, first_mismatch={first_mismatch_date}） ---")
        print(f"  重算後偵測到的事件數：{len(actions)}（twse={counters['n_twse']}, "
              f"yahoo_supplement={counters['n_yahoo_supplement']}, warnings={counters['n_warnings']}）")
        # Show any action within +-10 calendar days of the first mismatch date,
        # plus the earliest 3 actions overall (covers the "discrepant event is
        # before this window's first bar" case for codes whose mismatch starts
        # on bar #1).
        if first_mismatch_date:
            fmd = dt.datetime.strptime(first_mismatch_date, "%Y-%m-%d")
            near = [a for a in actions
                    if abs((dt.datetime.strptime(a["date"], "%Y-%m-%d") - fmd).days) <= 10]
            print(f"  first_mismatch 附近 (+-10天) 的事件：{near}")
        earliest = sorted(actions, key=lambda a: a["date"])[:3]
        print(f"  最早 3 個事件：{earliest}")
        print()


if __name__ == "__main__":
    main()
