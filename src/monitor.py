# -*- coding: utf-8 -*-
"""Standing intraday monitor loop. Phase 3 of docs/dev_plan_intraday.md.

Wires realtime/ (Phase 1) -> intraday.py (Phase 2) -> alerts.py (Phase 3
sibling module) into one loop: fetch -> accumulate per-code history ->
compute indicators -> evaluate_alerts() -> dedupe_and_cooldown() ->
send_telegram() -> persist to cache/intraday_{code}_{date}.json.

State shape (the caller owns this dict and passes it into every call, so
tests can inspect/replay it without touching global state):
    state = {
        "history": {
            code: {
                "ticks": [{"price","vol","ts","is_pretrade"}, ...],   # from Quote (MIS/TW)
                "trade_ticks": [{"price","qty","ts"}, ...],           # from Trade (crypto)
                "sides": ["buy"/"sell", ...],                         # parallel to trade_ticks
                "depth": {"bid_prices","bid_vols","ask_prices","ask_vols"} or None,
                "prev_depth": same shape or None,       # previous snapshot, for wall/pulled-order detection
                "session_open": datetime or None,       # first non-pretrade tick's ts; ORB anchor
            }
        },
        "cooldown": {(code, event_type): last_sent_datetime},  # owned by alerts.dedupe_and_cooldown
    }

Fetch/record are deliberately decoupled from evaluation: record_quote() /
record_trade() / record_depth() are plain functions that append one event
into `state`. run_once() calls source.poll_once() (MIS-style synchronous
adapters only) itself, but for push adapters (BinanceSource, WS-based) the
intended wiring is for the caller to register record_trade/record_depth as
the on_trade/on_depth callbacks on a running source, so state gets fed
continuously, and call run_once(..., source=None) periodically to just
evaluate whatever has accumulated. This is also what makes the "replay"
tests possible: build the ticks/trades directly (or via a fake source with
poll_once()) and feed them into run_once() without any real network I/O.

Phase 5: main() drives both tracks concurrently under one asyncio.run().
The TW/MIS side stays the synchronous poll_once()-based path from Phase 3,
just pushed onto a worker thread each round (asyncio.to_thread) so its
blocking `requests` call can't stall the event loop the Binance task needs.
The crypto side runs a live BinanceSource.run() as a background task
(public aggTrade + depth WebSocket streams -- no API key needed); its
on_trade/on_depth callbacks are wired straight to record_trade/record_depth
so `state` fills continuously between evaluation rounds, exactly as
tests/test_push_wiring.py pins the contract. See main()/`_main_loop()`.
"""
import asyncio
import datetime as dt
import json
import os

import data_sources as ds
import pipeline
from alerts import dedupe_and_cooldown, evaluate_alerts, send_telegram
from intraday import depth_imbalance, opening_range, realtime_cvd, vwap_bands
from realtime.binance_source import BinanceSource
from realtime.mis_source import MISSource

TW_OPEN = dt.time(8, 30)
TW_CLOSE = dt.time(13, 30)


# ------------------------------------------------------------- schedule ----

def is_market_open(market, now=None):
    """market="tw": 08:30-13:30 inclusive (covers the 08:30-09:00 pre-open
    call auction as well as the continuous session). market="crypto": always
    open (24h). `now` is injectable for tests."""
    now = now or dt.datetime.now()
    if market == "crypto":
        return True
    if market == "tw":
        t = now.time()
        return TW_OPEN <= t <= TW_CLOSE
    raise ValueError(f"unknown market: {market!r}")


# ---------------------------------------------------------------- state ----

def new_state():
    return {"history": {}, "cooldown": {}}


def _ensure_history(state, code):
    return state["history"].setdefault(code, {
        "ticks": [], "trade_ticks": [], "sides": [],
        "depth": None, "prev_depth": None, "session_open": None,
    })


def record_quote(state, quote):
    """Feed one realtime.models.Quote (MIS-style) into `state`."""
    hist = _ensure_history(state, quote.code)
    if quote.price is not None:
        hist["ticks"].append({
            "price": quote.price, "vol": quote.vol_lots or 0,
            "ts": quote.ts, "is_pretrade": bool(quote.is_pretrade),
        })
        if not quote.is_pretrade and hist["session_open"] is None:
            hist["session_open"] = _ts_to_dt(quote.ts)
    if quote.bid_prices or quote.ask_prices:
        if hist["depth"] is not None:
            hist["prev_depth"] = hist["depth"]
        hist["depth"] = {
            "bid_prices": quote.bid_prices, "bid_vols": quote.bid_vols,
            "ask_prices": quote.ask_prices, "ask_vols": quote.ask_vols,
        }


def record_trade(state, trade):
    """Feed one realtime.models.Trade (crypto tick) into `state`."""
    hist = _ensure_history(state, trade.code)
    hist["trade_ticks"].append({"price": trade.price, "qty": trade.qty, "ts": trade.ts})
    hist["sides"].append(trade.side or "buy")
    if hist["session_open"] is None:
        hist["session_open"] = _ts_to_dt(trade.ts)


def record_depth(state, depth):
    """Feed one realtime.models.Depth into `state` (crypto WS depth stream;
    MIS depth arrives bundled in Quote and goes through record_quote)."""
    hist = _ensure_history(state, depth.code)
    if hist["depth"] is not None:
        hist["prev_depth"] = hist["depth"]
    hist["depth"] = {
        "bid_prices": depth.bid_prices, "bid_vols": depth.bid_vols,
        "ask_prices": depth.ask_prices, "ask_vols": depth.ask_vols,
    }


def _ts_to_dt(ts):
    return dt.datetime.fromtimestamp(ts) if ts else dt.datetime.now()


def _cumulative_to_incremental(cum_vols):
    """Converts a monotonic session-cumulative volume series (MIS's `v`
    field, a running total since session open -- see
    realtime/mis_source.py's docstring) into per-tick deltas, which is what
    vwap_bands()/realtime_cvd() actually expect as a weight per data point
    (each is a straight `sum(vols)`/`vols[i]` accumulator, not a diff). Using
    the raw cumulative readings there would double-count every earlier
    poll's total on each subsequent tick and skew both toward the
    session's later/larger cumulative readings.

    The first reading has no prior poll to diff against, so it is used
    as-is (the volume traded between session open and the first poll); a
    reading that does not increase (two consecutive polls with no new
    trade, or a glitch) clamps to 0 rather than going negative."""
    incremental = []
    prev = None
    for v in cum_vols:
        incremental.append(v if prev is None else max(0, v - prev))
        prev = v
    return incremental


# ------------------------------------------------------------ indicators ----

def _compute_indicators(hist):
    """Builds the indicators dict evaluate_alerts() expects, from one
    code's accumulated `hist`. Prefers MIS ticks when present (TW), falls
    back to crypto trade ticks. Pre-open call-auction ticks (is_pretrade)
    are excluded from every continuous-session accumulator, per
    intraday.py's module docstring."""
    # cum_vol: session-cumulative traded volume as of this poll, used by
    # intraday_feedback.resample_records_to_minutes() to derive real
    # per-minute volume (diff of consecutive cum_vol readings) instead of a
    # snapshot-count proxy. TW: MIS's `v` field is already a running session
    # total (see realtime/mis_source.py), so the last tick's vol IS that
    # total -- do not sum it, summing session_ticks' vol would double count.
    # Crypto: Binance aggTrade qty is per-trade (not cumulative), so it must
    # be summed here to produce a comparable running total.
    if hist["ticks"]:
        session_ticks = [t for t in hist["ticks"] if not t["is_pretrade"]]
        prices = [t["price"] for t in session_ticks]
        cum_vols = [t["vol"] for t in session_ticks]
        vols = _cumulative_to_incremental(cum_vols)  # vwap_bands/realtime_cvd need per-tick, not cumulative
        ts_list = [t["ts"] for t in session_ticks]
        cvd = realtime_cvd(prices, vols, method="tick_rule")
        cum_vol = cum_vols[-1] if cum_vols else None
    else:
        prices = [t["price"] for t in hist["trade_ticks"]]
        vols = [t["qty"] for t in hist["trade_ticks"]]
        ts_list = [t["ts"] for t in hist["trade_ticks"]]
        cvd = realtime_cvd(None, vols, method="true_side", sides=hist["sides"])
        cum_vol = sum(vols) if vols else None

    vwap = vwap_bands(prices, vols)

    orb = {"high": None, "low": None, "breakout": None, "window_minutes": 15}
    if hist["session_open"] and ts_list and all(t is not None for t in ts_list):
        ts_dt = [dt.datetime.fromtimestamp(t) for t in ts_list]
        orb = opening_range(prices, ts_dt, hist["session_open"])

    depth_result = None
    if hist["depth"]:
        prev = hist.get("prev_depth")
        depth_result = depth_imbalance(
            hist["depth"]["bid_prices"], hist["depth"]["bid_vols"],
            hist["depth"]["ask_prices"], hist["depth"]["ask_vols"],
            prev["bid_vols"] if prev else None, prev["ask_vols"] if prev else None,
        )

    price = prices[-1] if prices else None
    # volume_anomaly() needs a multi-day same-time-slot baseline (Phase 4/5
    # territory -- computing it requires historical minute-bucket data this
    # module doesn't fetch yet); left None here so evaluate_alerts() simply
    # skips the volume_anomaly rule until a caller supplies one.
    return {"price": price, "vwap": vwap, "orb": orb, "cvd": cvd, "depth": depth_result,
            "volume": None, "cum_vol": cum_vol}


# --------------------------------------------------------------- cache ----

def _cache_file(code, date_str):
    return ds.intraday_cache_file(code, date_str)


def _persist_snapshot(code, market, now, indicators):
    """Appends one indicator snapshot to cache/intraday_{code}_{date}.json
    (read-modify-write of a JSON array under a top-level "records" key), so
    Phase 4/5 replay/backtest code can read a full day's indicator history
    per code."""
    os.makedirs(ds.CACHE_DIR, exist_ok=True)
    date_str = now.date().isoformat()
    path = _cache_file(code, date_str)
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError):
        blob = {"code": code, "market": market, "date": date_str, "records": []}
    blob["records"].append({
        "ts": now.isoformat(),
        "price": indicators.get("price"),
        "vwap": indicators.get("vwap"),
        "orb": indicators.get("orb"),
        "cvd": indicators.get("cvd"),
        "depth": indicators.get("depth"),
        "cum_vol": indicators.get("cum_vol"),
        "volume": indicators.get("volume"),
    })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(blob, f, ensure_ascii=False)


# ---------------------------------------------------------- main loop ----

def run_once(codes, market, state, source=None, send_fn=send_telegram, now=None, cooldown_minutes=5):
    """One evaluation round for `codes` in `market`.

    If `source` exposes poll_once() (MISSource-style), it is called once
    and every returned Quote for a code in `codes` is recorded first. If
    `source` is None (crypto/push-adapter case, or a replay test that
    already populated `state` via record_quote/record_trade/record_depth
    directly), this step is skipped and whatever is already in `state` is
    evaluated as-is.

    Resilient by design: a failing source.poll_once() or a failure while
    processing one code is caught, logged, and does not stop the loop from
    processing the remaining codes.

    Returns the list of {"alert": ..., "send_result": ...} that were
    actually sent this round (post dedupe/cooldown).
    """
    now = now or dt.datetime.now()

    if source is not None and hasattr(source, "poll_once"):
        try:
            quotes = source.poll_once()
        except Exception as exc:  # noqa: BLE001 -- must not kill the loop
            print(f"[monitor] source.poll_once() failed for market={market}: {exc}")
            quotes = []
        for q in quotes:
            if q.code in codes:
                record_quote(state, q)

    sent = []
    for code in codes:
        hist = state["history"].get(code)
        if not hist or (not hist["ticks"] and not hist["trade_ticks"]):
            continue  # no data yet for this code -- skip, don't error
        try:
            indicators = _compute_indicators(hist)
            alerts = evaluate_alerts(code, indicators)
            alerts = dedupe_and_cooldown(alerts, state["cooldown"], cooldown_minutes=cooldown_minutes, now=now)
            for alert in alerts:
                result = send_fn(alert["message"])
                sent.append({"alert": alert, "send_result": result})
            _persist_snapshot(code, market, now, indicators)
        except Exception as exc:  # noqa: BLE001 -- one bad code must not kill the round
            print(f"[monitor] run_once failed for code={code}: {exc}")
            continue
    return sent


def main(codes=None, crypto_codes=None, max_iterations=None, sleep_seconds=30,
         mis_source=None, binance_source=None, send_fn=send_telegram, state=None):
    """Standing loop: watchlist.json drives the TW/MIS track (via
    pipeline.load_watchlist(), same convention as app.py/snapshot.py);
    crypto_codes is an explicit opt-in list (empty by default) that, when
    non-empty, starts a live BinanceSource WebSocket task alongside the TW
    polling round -- public streams, no API key required. `binance_source`
    lets callers/tests inject a fake adapter instead of a real BinanceSource
    (mirrors the existing `mis_source` injection point). `max_iterations`
    bounds the loop for tests/manual runs; None (default) means run forever.
    """
    if codes is None:
        codes = [w["code"] for w in pipeline.load_watchlist()]
    if crypto_codes is None:
        crypto_codes = []
    state = state if state is not None else new_state()
    return asyncio.run(_main_loop(
        codes, crypto_codes, max_iterations, sleep_seconds,
        mis_source, binance_source, send_fn, state,
    ))


async def _main_loop(codes, crypto_codes, max_iterations, sleep_seconds,
                      mis_source, binance_source, send_fn, state):
    source = mis_source or MISSource()
    if codes:
        source.subscribe(codes)

    binance_task = None
    if crypto_codes:
        binance = binance_source or BinanceSource()
        binance.subscribe(crypto_codes)
        binance.on_trade(lambda t: record_trade(state, t))
        binance.on_depth(lambda d: record_depth(state, d))
        binance_task = asyncio.create_task(binance.run())

    try:
        iteration = 0
        while True:
            now = dt.datetime.now()
            if codes and is_market_open("tw", now):
                # Worker thread: source.poll_once() is a blocking `requests`
                # call (up to 15s timeout) and must not stall the event loop
                # the Binance task depends on to process incoming frames.
                await asyncio.to_thread(
                    run_once, codes, "tw", state, source=source, send_fn=send_fn, now=now,
                )
            if crypto_codes and is_market_open("crypto", now):
                # No polling network I/O here (source=None): just evaluates
                # whatever the Binance callbacks have already accumulated.
                run_once(crypto_codes, "crypto", state, source=None, send_fn=send_fn, now=now)
            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break
            await asyncio.sleep(sleep_seconds)
    finally:
        if binance_task is not None:
            binance_task.cancel()
            try:
                await binance_task
            except asyncio.CancelledError:
                pass
    return state


if __name__ == "__main__":
    main()
