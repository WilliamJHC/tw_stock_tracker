# -*- coding: utf-8 -*-
"""Shared analysis pipeline: watchlist I/O and per-stock payload assembly.

Used by both app.py (local FastAPI) and snapshot.py (cloud cron). Must not
import fastapi -- snapshot.py runs in a minimal environment without it.
"""
import datetime as dt
import json
import os

import analysis as an
import backtest as bt
import data_sources as ds
import db
import intraday_feedback as ifb

# Data files (cache/, static/, watchlist.json, .env) live at the repo root,
# one level up from src/.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
DEFAULT_WATCHLIST = [
    {"code": "2330", "name": "台積電"},
    {"code": "2317", "name": "鴻海"},
    {"code": "0050", "name": "元大台灣50"},
    {"code": "2454", "name": "聯發科"},
    {"code": "6869", "name": "雲豹能源"},
    {"code": "3443", "name": "創意"},
    {"code": "2881", "name": "富邦金"},
    {"code": "2891", "name": "中信金"},
    {"code": "6446", "name": "藥華藥"},
    {"code": "8046", "name": "南電"},
    {"code": "3653", "name": "健策"},
    {"code": "2313", "name": "華通"},
    {"code": "2327", "name": "國巨"},
]
CHART_BARS = 120      # daily bars sent to the chart
TICK_OF_DAYS = 20     # tick days fetched when paid tier is available


def load_watchlist():
    try:
        with open(WATCHLIST_PATH, encoding="utf-8") as f:
            wl = json.load(f)
        if isinstance(wl, list):
            return wl
    except (OSError, ValueError):
        pass
    save_watchlist(DEFAULT_WATCHLIST)
    return list(DEFAULT_WATCHLIST)


def save_watchlist(wl):
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(wl, f, ensure_ascii=False, indent=1)


def _fetch_tick_days(code, times):
    """Last TICK_OF_DAYS trading days of tick data (paid tier only)."""
    out = {}
    for ts in times[-TICK_OF_DAYS:]:
        date = dt.datetime.fromtimestamp(ts).date().isoformat()
        try:
            rows = ds.get_ticks(code, date)
            if rows:
                out[date] = rows
        except ds.SourceError:
            return None  # tier revoked mid-flight -> fall back to estimate
    return out or None


def build_stock_payload(code, rs_info=None):
    """Fetch, analyze, and assemble the full per-stock response dict.

    rs_info: optional {rank, percentile, return_pct} for this code (from
        an.rs_ranking() across the watchlist) -- see build_watchlist_payloads().
        A single-stock caller (e.g. app.py's /api/analysis/{code}) has no
        batch context, so it's left as None there; the RS signal/score
        contribution is simply skipped for that request.

    Raises ds.SourceError if daily data can't be fetched, or ValueError if
    history is insufficient for analysis.
    """
    code = code.strip().upper()
    daily = ds.get_daily(code)  # raises ds.SourceError

    # Prefer today's local intraday-monitor cache (higher frequency, no
    # network call) if monitor.py has been running and produced one; fall
    # back to yfinance's delayed 5m bars on any miss/failure (no cache file
    # yet today, corrupt cache, code never monitored, etc.) -- see
    # intraday_feedback.py's module docstring for the data-quality caveat
    # (activity-count volume proxy, not real traded volume).
    minute = None
    try:
        minute = ifb.get_minute_from_intraday_cache(code)
    except Exception:
        minute = None
    if not minute:
        try:
            minute = ds.get_minute(code)
        except Exception:
            minute = None

    tick_days = None
    if ds.tick_tier_available():
        tick_days = _fetch_tick_days(code, daily["t"])

    inst = ds.get_institutional(code)
    margin = ds.get_margin(code)
    shareholding = ds.get_shareholding(code)
    sbl = ds.get_sbl(code)
    result = an.analyze(daily, minute, tick_days, inst,
                        margin=margin, shareholding=shareholding, sbl=sbl, rs_info=rs_info)
    if result is None:
        raise ValueError(f"{code} 歷史資料不足")

    attention = an.compute_attention(result["score"], result.get("chip"), result)

    quote = ds.get_quote(code)
    pe = ds.get_pe(code)

    # chart payload: trim series to the last CHART_BARS bars
    n = len(daily["c"])
    s = max(0, n - CHART_BARS)
    chart = {
        "t": daily["t"][s:], "o": daily["o"][s:], "h": daily["h"][s:],
        "l": daily["l"][s:], "c": daily["c"][s:], "v": daily["v"][s:],
        "ma5": result["ma5"][s:], "ma20": result["ma20"][s:],
        "ma60": result["ma60"][s:], "vol20": result["vol20"][s:],
    }
    analysis_out = {k: v for k, v in result.items()
                    if k not in ("ma5", "ma20", "ma60", "vol20")}
    lastidx = n - 1
    return {
        "code": code,
        "name": (quote or {}).get("name") or code,
        "quote": quote,
        "pe": pe,
        "daily_source": daily["source"],
        "last_close": daily["c"][lastidx],
        "last_date": dt.datetime.fromtimestamp(daily["t"][lastidx]).date().isoformat(),
        "ma60_last": result["ma60"][lastidx],
        "chart": chart,
        "analysis": analysis_out,
        "attention": attention,
    }


def build_watchlist_payloads(codes=None):
    """Batch-assemble payloads for a set of codes (default: the full
    watchlist), including cross-stock 5-day relative-strength ranking
    (an.rs_ranking()) -- something build_stock_payload() alone can't do,
    since a single an.analyze() call only ever sees one stock.

    Used by snapshot.py's run_full() (the only existing "run the whole
    watchlist" loop); app.py's per-code /api/analysis/{code} endpoint stays
    single-stock and doesn't get an rs_rank (see build_stock_payload docstring).

    Returns {code: payload} on success or {code: {"error": str}} on failure,
    matching snapshot.py's existing per-code try/except convention.
    """
    if codes is None:
        codes = [s["code"] for s in load_watchlist()]

    returns_by_code = {}
    for code in codes:
        try:
            closes = ds.get_daily(code)["c"]  # cached -- build_stock_payload() below re-fetches for free
        except Exception:
            continue
        if len(closes) >= 6 and closes[-6]:
            returns_by_code[code] = (closes[-1] - closes[-6]) / closes[-6] * 100

    # rank() needs >= 2 codes to mean anything; skip (no rs_info) otherwise
    rs_ranks = an.rs_ranking(returns_by_code) if len(returns_by_code) >= 2 else {}

    out = {}
    for code in codes:
        try:
            out[code] = build_stock_payload(code, rs_info=rs_ranks.get(code))
        except Exception as e:
            # broad by design: matches snapshot.py's prior per-code try/except
            # (one bad stock must not abort the whole watchlist batch)
            out[code] = {"error": str(e)}
    return out


def build_suggestions(codes=None):
    """Rank-list of today's live decisions with backtest-backed confidence
    (docs/backtest_plan.md Phase 5): for each code, today's action/priority
    (from build_watchlist_payloads(), so RS ranking is included) plus this
    code's signal_stats/strategy_runs from the backtest DB, blended into a
    confidence score by backtest.compute_confidence().

    Degrades gracefully when the backtest DB hasn't been built yet (fresh
    db.connect()+init_db() just creates empty tables, no exception) --
    every code's confidence/expected_ret come back None with sample_n=0
    rather than raising, and data_as_of is None. A per-code live-fetch
    failure is surfaced as an "error" entry, matching
    build_watchlist_payloads()'s convention.

    Returns a list of {code, name, action, priority, confidence,
    expected_ret, sample_n, strategy, evidence, data_as_of} dicts (or
    {code, name, error} for codes whose live fetch failed).
    """
    if codes is None:
        codes = [s["code"] for s in load_watchlist()]

    conn = db.connect()
    db.init_db(conn)
    last_run_at = db.get_meta(conn, "last_run_at")

    payloads = build_watchlist_payloads(codes)

    out = []
    for code in codes:
        payload = payloads.get(code, {})
        if "error" in payload:
            out.append({"code": code, "name": code, "error": payload["error"]})
            continue

        attention = payload["attention"]
        signals = payload["analysis"].get("signals") or []
        signal_stats = db.query_signal_stats(conn, code)
        conf = bt.compute_confidence(signals, signal_stats)

        strategy_run = db.query_strategy_run(conn, code)
        strategy = None
        if strategy_run:
            strategy = {
                "total_return": strategy_run["total_return"],
                "buy_hold_return": strategy_run["buy_hold_return"],
                "win_rate": strategy_run["win_rate"],
                "max_drawdown": strategy_run["max_drawdown"],
                "n_trades": strategy_run["n_trades"],
            }

        out.append({
            "code": code,
            "name": payload["name"],
            "action": attention["action"],
            "priority": attention["priority"],
            "confidence": conf["confidence"],
            "expected_ret": conf["expected_ret"],
            "sample_n": conf["sample_n"],
            "strategy": strategy,
            "evidence": conf["evidence"],
            "data_as_of": last_run_at,
        })

    conn.close()
    return out
