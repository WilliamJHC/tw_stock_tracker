# -*- coding: utf-8 -*-
"""Phase 5.4 of docs/dev_plan_intraday.md: feed monitor.py's persisted intraday
snapshots (cache/intraday_{code}_{date}.json, written by monitor._persist_snapshot())
back into analysis.of_from_minutes()'s minute-bar input shape, so pipeline.py's
build_stock_payload() can prefer same-day, higher-frequency local data over
data_sources.get_minute()'s delayed yfinance 5m bars when a monitor run has
already produced a cache file for today.

Kept as its own module rather than added to monitor.py to avoid a circular
import: monitor.py already does `import pipeline` (for load_watchlist()), so if
pipeline.py needed to `import monitor` to reach this conversion function, the
two modules would import each other. This module has no dependency on monitor.py
(it only reads the JSON cache file monitor.py happens to write), so both
monitor.py and pipeline.py can depend on it without a cycle.

VOLUME (real, with a graceful-degrade fallback): monitor._persist_snapshot()
records a "cum_vol" field -- session-cumulative traded volume as of that poll
(TW: MIS's own running total; crypto: summed Binance aggTrade qty; see
monitor._compute_indicators()). resample_records_to_minutes() below diffs
consecutive cum_vol readings across minute buckets to get REAL per-minute
volume. Older cache records (written before this field existed) or any minute
with no usable cum_vol reading fall back to the COUNT of snapshots in that
minute as an activity-level proxy -- an approximation, not real volume -- so a
day file mixing old/new records still degrades gracefully instead of raising.
The "volume" field (distinct from "cum_vol") is unrelated: that one is always
None, reserved for volume_anomaly()'s multi-day baseline, not this module.
"""
import datetime as dt
import json

import data_sources as ds


def _cache_file(code, date_str):
    return ds.intraday_cache_file(code, date_str)


def load_intraday_cache(code, date_str):
    """Read cache/intraday_{code}_{date}.json written by monitor.py.

    Returns the parsed {"code","market","date","records":[...]} dict, or
    None if the file is missing, unreadable, not JSON, not the expected
    shape, or has an empty records list -- any of which should be treated
    as "no intraday data available today", not an error.
    """
    path = _cache_file(code, date_str)
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or not blob.get("records"):
        return None
    return blob


def resample_records_to_minutes(records):
    """Downsample monitor.py's high-frequency snapshot records (one per poll
    round, e.g. every ~30s per run_once()'s sleep_seconds) into minute
    buckets shaped like data_sources.get_minute()'s {"t","c","v"} dict --
    i.e. what analysis.of_from_minutes() expects as its `mhist` argument.

    Field mapping per minute bucket:
      t -> unix timestamp of the minute's start (from each record's "ts",
           an ISO datetime string written by monitor._persist_snapshot()
           via now.isoformat())
      c -> "price" of the LAST record whose ts falls in that minute (records
           are appended in chronological order by _persist_snapshot(), so a
           simple last-write-wins per bucket gives the minute's close)
      v -> REAL per-minute traded volume: the bucket's last "cum_vol" reading
           minus the previous bucket's last "cum_vol" reading (both are
           session-cumulative running totals, see this module's docstring).
           A bucket with no usable cum_vol reading (older cache format, or a
           gap) falls back to the COUNT of snapshots in that bucket instead
           -- an activity-level PROXY, not real volume -- and does not reset
           the running cum_vol baseline used to diff the next bucket.

    Records missing a usable "price" or "ts", or with an unparsable "ts",
    are skipped rather than raising, since a corrupt record should not sink
    the whole conversion. Returns None if no record yielded a usable bucket
    (caller should treat that the same as "no cache available" and fall
    back). Output is sorted oldest-first, matching get_minute()'s convention.
    """
    buckets = {}  # minute-start unix ts -> {"c": last_price, "count": n, "cum_vol": last reading or None}
    for rec in records:
        price = rec.get("price")
        ts_str = rec.get("ts")
        if price is None or not ts_str:
            continue
        try:
            ts = dt.datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        minute_start = ts.replace(second=0, microsecond=0)
        minute_ts = int(minute_start.timestamp())
        bucket = buckets.setdefault(minute_ts, {"c": price, "count": 0, "cum_vol": None})
        bucket["c"] = price  # chronological input order -> last assignment wins
        bucket["count"] += 1
        cum_vol = rec.get("cum_vol")
        if cum_vol is not None:
            bucket["cum_vol"] = cum_vol  # last-write-wins, same as price
    if not buckets:
        return None
    keys = sorted(buckets)

    volumes = []
    prev_cum_vol = None
    for k in keys:
        cum_vol = buckets[k]["cum_vol"]
        if cum_vol is not None and prev_cum_vol is not None:
            volumes.append(max(0.0, cum_vol - prev_cum_vol))  # clamp: a glitchy/reset reading must not go negative
        elif cum_vol is not None:
            volumes.append(cum_vol)  # first reading of the day: nothing to diff against yet
        else:
            volumes.append(buckets[k]["count"])  # no cum_vol this minute -- fall back to the count proxy
        if cum_vol is not None:
            prev_cum_vol = cum_vol

    return {
        "t": keys,
        "c": [buckets[k]["c"] for k in keys],
        "v": volumes,
    }


def get_minute_from_intraday_cache(code, date_str=None):
    """Top-level helper for pipeline.build_stock_payload(): load + resample
    today's intraday cache for `code`, or return None on any failure so the
    caller can fall back to data_sources.get_minute() unconditionally.

    date_str defaults to today (local date) -- pass explicitly in tests to
    avoid depending on wall-clock date.
    """
    date_str = date_str or dt.date.today().isoformat()
    try:
        blob = load_intraday_cache(code, date_str)
        if blob is None:
            return None
        return resample_records_to_minutes(blob["records"])
    except Exception:  # noqa: BLE001 -- any cache/format problem must fall back, never crash the pipeline
        return None
