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

KNOWN DATA GAP (read before trusting the output): monitor._persist_snapshot()
records price/vwap/orb/cvd/depth per poll, but its "volume" field is always
None -- monitor._compute_indicators()'s docstring says the real per-slot volume
baseline needed for that is Phase 4/5 territory and isn't computed yet. In other
words, the intraday cache has no reliable per-tick or per-minute traded-volume
figure to resample here. resample_records_to_minutes() below therefore uses the
COUNT of snapshots falling in each minute as an activity-level proxy for volume
-- this is an APPROXIMATION, not real traded volume, and is documented as such
on the return value. Any CVD estimate analysis.of_from_minutes() computes from
this source inherits that lower confidence versus the get_minute() (yfinance)
path, which carries real share volume.

TODO (future work, not done in this pass): extend monitor.record_quote() /
_persist_snapshot() to additionally capture per-tick volume (e.g. the delta of
quote.vol_lots between successive quotes, or cumulative session volume) so a
future version of resample_records_to_minutes() can compute real per-minute
volume instead of the snapshot-count proxy.
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
      v -> COUNT of records falling in that minute -- an activity-level
           PROXY, NOT real traded volume. See this module's docstring for
           why (monitor.py does not currently persist per-tick volume).

    Records missing a usable "price" or "ts", or with an unparsable "ts",
    are skipped rather than raising, since a corrupt record should not sink
    the whole conversion. Returns None if no record yielded a usable bucket
    (caller should treat that the same as "no cache available" and fall
    back). Output is sorted oldest-first, matching get_minute()'s convention.
    """
    buckets = {}  # minute-start unix ts -> {"c": last_price, "v": count}
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
        bucket = buckets.setdefault(minute_ts, {"c": price, "v": 0})
        bucket["c"] = price  # chronological input order -> last assignment wins
        bucket["v"] += 1
    if not buckets:
        return None
    keys = sorted(buckets)
    return {
        "t": keys,
        "c": [buckets[k]["c"] for k in keys],
        "v": [buckets[k]["v"] for k in keys],
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
