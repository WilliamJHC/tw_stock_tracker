# -*- coding: utf-8 -*-
"""Build stock-split/dividend adjustment factors for the backtest DB
(docs/archive/backtest_plan.md Phase 1b). Offline batch job -- this is the
only place in the project that pulls yfinance/pandas for adjustment math;
src/adjust.py (the code the rest of the app touches) stays pure-stdlib and
only reads floats back out of the DB.

Two sources, combined per code:
  - TWSE 除權除息計算結果表 (TWT49U), one request per calendar year for the
    WHOLE market -- the primary, most-authoritative source for TWSE-listed
    (上市) codes. Gives official pre_close/ref_price directly.
  - Yahoo Finance Adj Close / this project's own FinMind raw close, the
    ratio of which IS the back-adjustment factor directly (Adj Close is
    fully split+dividend adjusted; FinMind's close is raw -- see
    src/data_sources.py's get_daily() docstring on why plain
    "Adj Close / Close" doesn't work: Yahoo's own Close is already
    split-adjusted, so that ratio only captures dividends). Used as the
    SOLE source for TPEX-listed (上櫃) codes (TWT49U doesn't cover them),
    and to fill day-level gaps in TWSE coverage for TWSE-listed codes.
    Any Yahoo-detected step that lands on neither a TWSE event date nor
    looks like a genuine multi-percent move is logged as a cross-source
    price-mismatch warning instead of being treated as a corporate action
    (the "free by-product" cross-validation from Phase 1b).

Usage:
    python scripts/build_adj_factors.py [--codes 2330,2317] [--verify]

Without --codes, runs the full watchlist. Always writes corporate_actions +
daily_bars.adj_factor for the codes it processes (INSERT OR REPLACE / UPDATE,
idempotent). --verify additionally runs src/adjust.py's invariant checks on
every code and prints event/warning counts.
"""
import argparse
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import adjust  # noqa: E402
import data_sources as ds  # noqa: E402
import db  # noqa: E402
import pipeline as pl  # noqa: E402

TWSE_URL = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
# >2pp day-over-day ratio jump: matches docs/archive/backtest_plan.md Phase
# 1b's cross-validation threshold ("任何非公司行動日的階梯...實測抓到 115
# 筆日報酬差異 >2pp"). Below this, treat it as float noise / rounding, not a
# real corporate action or a worthwhile warning.
STEP_THRESHOLD = 0.02
# A Yahoo-detected step within this many calendar days of a known TWSE
# event date is considered "the same event" (ex-dates can shift a day
# between vendors' calendars) rather than a separate/conflicting one.
DATE_MATCH_TOLERANCE_DAYS = 3


# --------------------------------------------------------------- TWSE ----

def _roc_to_iso(roc_date):
    """"114年03月18日" -> "2025-03-18"."""
    y = roc_date[:3]
    rest = roc_date[3:]
    m = rest.split("年")[1].split("月")[0]
    d = rest.split("月")[1].split("日")[0]
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def _num(s):
    s = str(s).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def fetch_twse_year(year):
    """One request for the whole TWSE market's corporate actions in
    `year`. Returns raw rows (list of lists) or [] on failure -- a single
    bad year shouldn't abort the whole run."""
    start = f"{year}0101"
    end = f"{year}1231"
    try:
        import requests
        resp = requests.get(
            TWSE_URL, params={"startDate": start, "endDate": end, "response": "json"},
            headers={"User-Agent": "Mozilla/5.0 (stock-tracker local app)"}, timeout=30,
        )
        body = resp.json()
    except Exception as e:
        print(f"  TWSE {year}: 失敗 ({e})")
        return []
    if body.get("stat") != "OK":
        return []
    return body.get("data") or []


def parse_twse_rows(rows, codes):
    """rows: fetch_twse_year()'s output, possibly from several years
    concatenated. codes: set of stock codes we care about (skip the rest --
    the response covers the whole market). Returns {code: [{date, pre_close,
    ref_price, ratio, kind}, ...]} ascending by date."""
    by_code = {}
    for row in rows:
        code = row[1]
        if code not in codes:
            continue
        pre_close, ref_price = _num(row[3]), _num(row[4])
        if not pre_close or not ref_price:
            continue  # can't compute a ratio without both
        date = _roc_to_iso(row[0])
        by_code.setdefault(code, []).append({
            "date": date, "pre_close": pre_close, "ref_price": ref_price,
            "ratio": ref_price / pre_close, "kind": row[6],
        })
    for code in by_code:
        by_code[code].sort(key=lambda r: r["date"])
    return by_code


# -------------------------------------------------------------- yahoo ----

def fetch_yahoo_adj_close(code, start_date):
    """{date_iso: adj_close} from Yahoo, or {} on failure. Uses
    data_sources' .TW/.TWO fallback logic via _yf_history_since so TPEX
    codes resolve the same way get_daily() would."""
    try:
        df = ds._yf_history_since(code, start_date, "1d")
    except Exception as e:
        print(f"  yfinance {code}: 失敗 ({e})")
        return {}
    out = {}
    for ts, row in df.iterrows():
        adj_close = row.get("Adj Close")
        if adj_close is None or adj_close != adj_close:  # NaN
            continue
        out[ts.strftime("%Y-%m-%d")] = float(adj_close)
    return out


def compute_yahoo_ratio_series(bars, yahoo_adj_close):
    """bars: ascending [{date, c, ...}]. Returns {date: adj_close/raw_close}
    for dates present in both and with a positive raw close."""
    out = {}
    for b in bars:
        raw = b["c"]
        adj = yahoo_adj_close.get(b["date"])
        if raw and adj is not None:
            out[b["date"]] = adj / raw
    return out


def detect_steps(dates_sorted, ratio_by_date, threshold=STEP_THRESHOLD):
    """dates_sorted: ascending dates present in ratio_by_date, where
    ratio_by_date[d] == Yahoo Adj Close / FinMind raw close on d -- this IS
    the target adj_factor(d) directly (see module docstring). Returns
    [{"date": d, "ratio": step}] for every consecutive-day change whose
    relative size exceeds `threshold`, one entry per detected corporate
    action's ex-date d.

    `ratio` is deliberately PREV/CURRENT (not current/prev): d's ratio_yahoo
    is itself adj_factor(d) (the bar ON the ex-date is already post-event
    scale, same convention as TWSE's ref_price), while
    adjust.build_adj_factor() wants an event's `ratio` to be the
    multiplicative factor that bars strictly BEFORE d get scaled by --
    i.e. adj_factor(day before d) / adj_factor(d), the same ref/pre<1
    shape as a TWSE row. Getting this backwards produces a factor series
    that grows going into the past instead of shrinking (caught by
    adjust.validate_adj_factor()'s monotonicity check if it slips through)."""
    steps = []
    for i in range(1, len(dates_sorted)):
        prev_ratio = ratio_by_date[dates_sorted[i - 1]]
        r = ratio_by_date[dates_sorted[i]]
        if not prev_ratio or not r:
            continue
        step = prev_ratio / r
        if abs(step - 1.0) > threshold:
            # Persistence check: a real corporate action's new price level
            # holds (the very next observed ratio should still be close to
            # `r`, not bounce back toward prev_ratio) -- a single day where
            # FinMind and Yahoo's raw closes simply disagree (a data-quality
            # blip, not an event) instead reverts on the next date. Without
            # this, a transient one-day mismatch gets misread as a
            # corporate action and corrupts every earlier bar's factor.
            # Look ahead up to 2 observed points (a same-day revert or a
            # 1-day-late correction both happen in practice): if either one
            # is back close to prev_ratio, the "new level" never actually
            # held -- it's a blip, not a step.
            persists = True
            for lookahead in (1, 2):
                if i + lookahead >= len(dates_sorted):
                    break
                later_r = ratio_by_date[dates_sorted[i + lookahead]]
                if later_r and abs(later_r / prev_ratio - 1.0) < threshold / 2:
                    persists = False
                    break
            steps.append({"date": dates_sorted[i], "ratio": step, "persists": persists})
    return steps


def _matches_any(date, other_dates, tolerance_days=DATE_MATCH_TOLERANCE_DAYS):
    d = dt.datetime.strptime(date, "%Y-%m-%d")
    for od in other_dates:
        if abs((d - dt.datetime.strptime(od, "%Y-%m-%d")).days) <= tolerance_days:
            return True
    return False


# ------------------------------------------------------------- per-code --

def resolve_adjustment_actions(code, market, bars, twse_events):
    """DB-free core of build_code(): combine `twse_events` (this code's rows
    from parse_twse_rows(), [] if none/not TWSE-listed) with a Yahoo-ratio
    cross-check, same source-selection rules build_code() has always used
    (TWSE authoritative for 上市 codes when available, Yahoo-ratio-only for
    TPEX/no-TWSE-coverage). Used both by build_code() (DB-backed, watchlist
    scale) and scripts/run_factor_backtest.py's market-wide c_adj pipeline
    (no DB) so the two paths can't drift apart.

    Returns (actions, counters) where counters = {n_events, n_twse,
    n_yahoo_supplement, n_warnings, yahoo_ok} -- the first four are the
    fields build_code() used to return directly (minus `violations`, which
    the caller derives itself once it has bar_dates+factors). yahoo_ok is
    False when fetch_yahoo_adj_close() came back empty (network failure OR
    genuinely no Yahoo data for this code -- indistinguishable, same as
    before this function existed) -- a caller relying solely on Yahoo (no
    TWSE coverage for this code) needs this to tell "confirmed no
    adjustment" apart from "couldn't check," see
    scripts/run_factor_backtest.py's _augment_bars_with_adj_factor()."""
    bar_dates = [b["date"] for b in bars]
    yahoo_adj_close = fetch_yahoo_adj_close(code, min(bar_dates))
    yahoo_ratio_series = compute_yahoo_ratio_series(bars, yahoo_adj_close)
    yahoo_steps = detect_steps(sorted(yahoo_ratio_series), yahoo_ratio_series) if yahoo_ratio_series else []

    if market == "tpex" or not twse_events:
        actions = [{"date": s["date"], "ratio": s["ratio"], "pre_close": None,
                    "ref_price": None, "kind": None, "source": "yahoo_ratio"}
                   for s in yahoo_steps if s["persists"]]
        n_twse, n_warnings = 0, sum(1 for s in yahoo_steps if not s["persists"])
    else:
        twse_dates = [e["date"] for e in twse_events]
        actions = [{**e, "source": "twse"} for e in twse_events]
        n_twse = len(actions)
        n_warnings = 0
        for s in yahoo_steps:
            if _matches_any(s["date"], twse_dates):
                continue  # confirms an existing TWSE event, nothing to add
            n_warnings += 1
            # A non-persistent step is a single-day FinMind/Yahoo close
            # mismatch (see detect_steps' docstring), not a real corporate
            # action -- log it as a warning only, don't corrupt the factor
            # series with it.
            if s["persists"]:
                actions.append({"date": s["date"], "ratio": s["ratio"], "pre_close": None,
                                 "ref_price": None, "kind": None, "source": "yahoo_ratio"})

    n_yahoo_supplement = len(actions) - n_twse
    return actions, {
        "n_events": len(actions), "n_twse": n_twse,
        "n_yahoo_supplement": n_yahoo_supplement, "n_warnings": n_warnings,
        "yahoo_ok": bool(yahoo_adj_close),
    }


def build_code(conn, code, market, bars=None):
    """Resolves one code's corporate_actions + adj_factor and writes them.
    Returns a dict of counters for the summary report: n_events (final,
    written), n_twse, n_yahoo_supplement, n_warnings, violations (from
    adjust.validate_adj_factor(), should be [])."""
    if bars is None:
        bars = db.query_daily_bars(conn, code)
    if not bars:
        return {"n_events": 0, "n_twse": 0, "n_yahoo_supplement": 0, "n_warnings": 0, "violations": ["no bars"]}
    bar_dates = [b["date"] for b in bars]

    twse_events = TWSE_BY_CODE.get(code, [])
    actions, counters = resolve_adjustment_actions(code, market, bars, twse_events)
    db.upsert_corporate_actions(conn, code, actions)

    factors = adjust.build_adj_factor(bar_dates, actions)
    db.update_adj_factors(conn, code, dict(zip(bar_dates, factors)))

    violations = adjust.validate_adj_factor(bar_dates, factors, [a["date"] for a in actions])
    return {**counters, "violations": violations}


TWSE_BY_CODE = {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=str, default=None, help="逗號分隔的股票代碼，預設為完整 watchlist")
    ap.add_argument("--verify", action="store_true", help="額外印出事件/告警統計與不變量檢查結果")
    args = ap.parse_args()

    codes = args.codes.split(",") if args.codes else [s["code"] for s in pl.load_watchlist()]

    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)

    universe = ds.get_universe() or []
    market_by_code = {r["code"]: r["market"] for r in universe}

    bars_by_code = {}
    min_dates = []
    for code in codes:
        bars = db.query_daily_bars(conn, code)
        bars_by_code[code] = bars
        if bars:
            min_dates.append(bars[0]["date"])
    if not min_dates:
        print("DB 內沒有任何指定股票的日線資料，先跑 run_backtest.py --refresh-data")
        sys.exit(1)
    years = range(int(min(min_dates)[:4]), dt.date.today().year + 1)

    print(f"抓取 TWSE 除權息計算結果表（{years[0]}-{years[-1]}，共 {len(years)} 個 request）...")
    all_rows = []
    for year in years:
        rows = fetch_twse_year(year)
        all_rows.extend(rows)
        print(f"  {year}: {len(rows)} 筆")
        time.sleep(0.3)
    global TWSE_BY_CODE
    TWSE_BY_CODE = parse_twse_rows(all_rows, set(codes))

    print(f"\n計算 {len(codes)} 檔股票的還原因子 ...")
    totals = {"n_events": 0, "n_twse": 0, "n_yahoo_supplement": 0, "n_warnings": 0}
    all_violations = {}
    for i, code in enumerate(codes, 1):
        market = market_by_code.get(code, "twse")
        result = build_code(conn, code, market, bars=bars_by_code.get(code))
        for key in totals:
            totals[key] += result[key]
        if result["violations"]:
            all_violations[code] = result["violations"]
        print(f"[{i}/{len(codes)}] {code} ({market}): {result['n_events']} 個公司行動 "
              f"(twse={result['n_twse']}, yahoo補={result['n_yahoo_supplement']}), "
              f"{result['n_warnings']} 筆跨來源告警"
              + (f" ⚠ {len(result['violations'])} 個不變量違反！" if result["violations"] else ""))

    print(f"\n合計：{totals['n_events']} 個公司行動（twse={totals['n_twse']}, "
          f"yahoo補={totals['n_yahoo_supplement']}），{totals['n_warnings']} 筆跨來源告警")

    if args.verify:
        if all_violations:
            print(f"\n⚠ {len(all_violations)} 檔股票的不變量檢查失敗：")
            for code, violations in all_violations.items():
                for v in violations:
                    print(f"  {code}: {v}")
            sys.exit(1)
        else:
            print("\n不變量檢查全部通過（最後一段 factor==1.0、僅在公司行動日變動）")

    conn.close()


if __name__ == "__main__":
    main()
