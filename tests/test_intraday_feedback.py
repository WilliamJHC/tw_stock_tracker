# -*- coding: utf-8 -*-
"""Unit tests for intraday_feedback.py: converting monitor.py's persisted
cache/intraday_{code}_{date}.json snapshots into analysis.of_from_minutes()'s
{"t","c","v"} minute-bar input shape, and pipeline.py's use-cache-else-fallback
wiring in build_stock_payload(). No real network calls or real filesystem
outside tmp_path (data_sources.CACHE_DIR is monkeypatched).
"""
import json


import pytest

import data_sources as ds
import intraday_feedback as ifb
import pipeline as pl


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "CACHE_DIR", str(tmp_path))


def _write_cache(code, date_str, records):
    path = ifb._cache_file(code, date_str)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"code": code, "market": "tw", "date": date_str, "records": records}, f)
    return path


# ------------------------------------------------------- resample_records --

def test_resample_records_to_minutes_groups_by_minute_and_takes_last_price():
    records = [
        {"ts": "2026-07-20T09:00:05", "price": 100.0},
        {"ts": "2026-07-20T09:00:35", "price": 100.5},  # same minute -> becomes the close
        {"ts": "2026-07-20T09:01:10", "price": 101.0},
    ]
    out = ifb.resample_records_to_minutes(records)
    assert out["c"] == [100.5, 101.0]
    assert out["v"] == [2, 1]  # activity-count proxy, not real volume
    assert out["t"] == sorted(out["t"])  # oldest-first


def test_resample_records_to_minutes_skips_missing_price_or_ts():
    records = [
        {"ts": "2026-07-20T09:00:05", "price": None},
        {"ts": None, "price": 100.0},
        {"ts": "2026-07-20T09:00:05", "price": 100.0},
    ]
    out = ifb.resample_records_to_minutes(records)
    assert out["c"] == [100.0]
    assert out["v"] == [1]


def test_resample_records_to_minutes_skips_unparsable_ts():
    records = [{"ts": "not-a-timestamp", "price": 100.0}]
    assert ifb.resample_records_to_minutes(records) is None


def test_resample_records_to_minutes_empty_input_returns_none():
    assert ifb.resample_records_to_minutes([]) is None


def test_resample_records_to_minutes_uses_cum_vol_diff_as_real_volume():
    """cum_vol present on every record -> per-minute volume is the diff of
    consecutive cumulative readings, not the count-of-snapshots proxy."""
    records = [
        {"ts": "2026-07-20T09:00:05", "price": 100.0, "cum_vol": 500},
        {"ts": "2026-07-20T09:00:35", "price": 100.5, "cum_vol": 620},  # same minute
        {"ts": "2026-07-20T09:01:10", "price": 101.0, "cum_vol": 900},
    ]
    out = ifb.resample_records_to_minutes(records)
    # first minute has nothing to diff against yet -> raw cumulative reading
    # second minute -> 900 - 620 (the minute's LAST cum_vol reading, i.e. 620, not 500)
    assert out["v"] == [620, 280]


def test_resample_records_to_minutes_clamps_negative_diff_to_zero():
    """A glitchy/reset cum_vol reading (e.g. source restarted mid-session)
    must not produce a negative volume."""
    records = [
        {"ts": "2026-07-20T09:00:05", "price": 100.0, "cum_vol": 900},
        {"ts": "2026-07-20T09:01:10", "price": 101.0, "cum_vol": 100},
    ]
    out = ifb.resample_records_to_minutes(records)
    assert out["v"] == [900, 0]


def test_resample_records_to_minutes_falls_back_to_count_per_bucket_missing_cum_vol():
    """A day file mixing old-format records (no cum_vol) with new ones: the
    minute lacking cum_vol falls back to its snapshot count, and does not
    corrupt the running baseline used to diff the next minute that has one."""
    records = [
        {"ts": "2026-07-20T09:00:05", "price": 100.0, "cum_vol": 500},
        {"ts": "2026-07-20T09:01:05", "price": 100.2},   # old format: no cum_vol
        {"ts": "2026-07-20T09:01:35", "price": 100.4},   # same minute, still no cum_vol
        {"ts": "2026-07-20T09:02:05", "price": 101.0, "cum_vol": 650},
    ]
    out = ifb.resample_records_to_minutes(records)
    assert out["v"] == [500, 2, 150]  # 500 (first reading), 2 (count fallback), 650-500 (baseline carried forward)


# ------------------------------------------------------- load_intraday_cache --

def test_load_intraday_cache_missing_file_returns_none():
    assert ifb.load_intraday_cache("2330", "2026-07-20") is None


def test_load_intraday_cache_empty_records_returns_none():
    _write_cache("2330", "2026-07-20", [])
    assert ifb.load_intraday_cache("2330", "2026-07-20") is None


def test_load_intraday_cache_corrupt_json_returns_none(tmp_path):
    path = ifb._cache_file("2330", "2026-07-20")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    assert ifb.load_intraday_cache("2330", "2026-07-20") is None


def test_get_minute_from_intraday_cache_end_to_end():
    _write_cache("2330", "2026-07-20", [
        {"ts": "2026-07-20T09:00:05", "price": 100.0},
        {"ts": "2026-07-20T09:01:10", "price": 101.0},
    ])
    out = ifb.get_minute_from_intraday_cache("2330", date_str="2026-07-20")
    assert out["c"] == [100.0, 101.0]


def test_get_minute_from_intraday_cache_missing_returns_none():
    assert ifb.get_minute_from_intraday_cache("9999", date_str="2026-07-20") is None


# ------------------------------------------------- pipeline.py fallback wiring --

def test_build_stock_payload_prefers_intraday_cache_over_get_minute(monkeypatch):
    daily = {"t": [1, 2, 3, 4, 5, 6], "o": [1] * 6, "h": [1] * 6, "l": [1] * 6,
             "c": [1, 2, 3, 4, 5, 6], "v": [1] * 6, "source": "fake"}
    monkeypatch.setattr(ds, "get_daily", lambda code: daily)
    monkeypatch.setattr(ds, "get_minute", lambda code: (_ for _ in ()).throw(AssertionError("should not be called")))
    monkeypatch.setattr(ds, "tick_tier_available", lambda: False)
    monkeypatch.setattr(ds, "get_institutional", lambda code: None)
    monkeypatch.setattr(ds, "get_margin", lambda code: None)
    monkeypatch.setattr(ds, "get_shareholding", lambda code: None)
    monkeypatch.setattr(ds, "get_sbl", lambda code: None)
    monkeypatch.setattr(ds, "get_quote", lambda code: None)
    monkeypatch.setattr(ds, "get_pe", lambda code: None)

    captured = {}

    def fake_ifb_get_minute(code):
        captured["called_with"] = code
        return {"t": [1000], "c": [1.0], "v": [1]}

    monkeypatch.setattr(ifb, "get_minute_from_intraday_cache", fake_ifb_get_minute)

    def fake_analyze(daily_arg, minute, tick_days, inst, **kw):
        captured["minute_passed_to_analyze"] = minute
        return {"score": 0, "chip": None, "ma5": [0] * 6, "ma20": [0] * 6,
                "ma60": [0] * 6, "vol20": [0] * 6}

    monkeypatch.setattr("analysis.analyze", fake_analyze)
    monkeypatch.setattr("analysis.compute_attention", lambda *a, **kw: {})

    pl.build_stock_payload("2330")
    assert captured["called_with"] == "2330"
    assert captured["minute_passed_to_analyze"] == {"t": [1000], "c": [1.0], "v": [1]}


def test_build_stock_payload_falls_back_to_get_minute_when_no_intraday_cache(monkeypatch):
    daily = {"t": [1, 2, 3, 4, 5, 6], "o": [1] * 6, "h": [1] * 6, "l": [1] * 6,
             "c": [1, 2, 3, 4, 5, 6], "v": [1] * 6, "source": "fake"}
    monkeypatch.setattr(ds, "get_daily", lambda code: daily)
    monkeypatch.setattr(ifb, "get_minute_from_intraday_cache", lambda code: None)

    captured = {}

    def fake_get_minute(code):
        captured["fallback_called"] = True
        return {"t": [2000], "c": [2.0], "v": [5]}

    monkeypatch.setattr(ds, "get_minute", fake_get_minute)
    monkeypatch.setattr(ds, "tick_tier_available", lambda: False)
    monkeypatch.setattr(ds, "get_institutional", lambda code: None)
    monkeypatch.setattr(ds, "get_margin", lambda code: None)
    monkeypatch.setattr(ds, "get_shareholding", lambda code: None)
    monkeypatch.setattr(ds, "get_sbl", lambda code: None)
    monkeypatch.setattr(ds, "get_quote", lambda code: None)
    monkeypatch.setattr(ds, "get_pe", lambda code: None)

    def fake_analyze(daily_arg, minute, tick_days, inst, **kw):
        captured["minute_passed_to_analyze"] = minute
        return {"score": 0, "chip": None, "ma5": [0] * 6, "ma20": [0] * 6,
                "ma60": [0] * 6, "vol20": [0] * 6}

    monkeypatch.setattr("analysis.analyze", fake_analyze)
    monkeypatch.setattr("analysis.compute_attention", lambda *a, **kw: {})

    pl.build_stock_payload("2330")
    assert captured["fallback_called"] is True
    assert captured["minute_passed_to_analyze"] == {"t": [2000], "c": [2.0], "v": [5]}


def test_build_stock_payload_intraday_cache_exception_falls_back(monkeypatch):
    daily = {"t": [1, 2, 3, 4, 5, 6], "o": [1] * 6, "h": [1] * 6, "l": [1] * 6,
             "c": [1, 2, 3, 4, 5, 6], "v": [1] * 6, "source": "fake"}
    monkeypatch.setattr(ds, "get_daily", lambda code: daily)

    def boom(code):
        raise RuntimeError("cache read exploded")

    monkeypatch.setattr(ifb, "get_minute_from_intraday_cache", boom)

    captured = {}

    def fake_get_minute(code):
        captured["fallback_called"] = True
        return {"t": [2000], "c": [2.0], "v": [5]}

    monkeypatch.setattr(ds, "get_minute", fake_get_minute)
    monkeypatch.setattr(ds, "tick_tier_available", lambda: False)
    monkeypatch.setattr(ds, "get_institutional", lambda code: None)
    monkeypatch.setattr(ds, "get_margin", lambda code: None)
    monkeypatch.setattr(ds, "get_shareholding", lambda code: None)
    monkeypatch.setattr(ds, "get_sbl", lambda code: None)
    monkeypatch.setattr(ds, "get_quote", lambda code: None)
    monkeypatch.setattr(ds, "get_pe", lambda code: None)
    monkeypatch.setattr("analysis.analyze", lambda *a, **kw: {
        "score": 0, "chip": None, "ma5": [0] * 6, "ma20": [0] * 6, "ma60": [0] * 6, "vol20": [0] * 6})
    monkeypatch.setattr("analysis.compute_attention", lambda *a, **kw: {})

    pl.build_stock_payload("2330")
    assert captured["fallback_called"] is True
