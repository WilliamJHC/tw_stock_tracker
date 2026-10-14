# -*- coding: utf-8 -*-
"""scripts/run_pullback_scan.py: back-adjustment of fresh OHLC with the
factor cache's c_adj/c, and the screen -> fetch -> setup pipeline with a
fake fetch_fn (no network, no DB)."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import pytest  # noqa: E402

import run_pullback_scan as rps  # noqa: E402
from test_pullback_strategy import _setup_bars  # noqa: E402


def _ts(date_str):
    return dt.datetime.fromisoformat(date_str + "T00:00:00").timestamp()


def _daily(dates, closes, adjusted=False):
    return {"t": [_ts(d) for d in dates], "o": closes, "h": [c + 1 for c in closes],
            "l": [c - 1 for c in closes], "c": closes, "v": [1000] * len(closes),
            "adjusted": adjusted, "source": "finmind"}


def test_adjustment_factors_from_cache():
    cached = [{"date": "2026-01-02", "c": 100, "c_adj": 90}, {"date": "2026-01-03", "c": 50, "c_adj": 50},
              {"date": "2026-01-04", "c": 51}]
    dates, factors = rps.adjustment_factors(cached)
    assert dates == ["2026-01-02", "2026-01-03"]
    assert factors == pytest.approx([0.9, 1.0])


def test_daily_to_ohlc_bars_applies_next_cached_factor_and_keeps_volume_raw():
    daily = _daily(["2026-01-01", "2026-01-02", "2026-01-05"], [100.0, 100.0, 50.0])
    bars = rps.daily_to_ohlc_bars(daily, ["2026-01-02", "2026-01-03"], [0.9, 1.0])
    # 01-01 (not cached) takes 01-02's factor; 01-05 is newer than the cache -> 1.0
    assert [b["c"] for b in bars] == pytest.approx([90.0, 90.0, 50.0])
    assert bars[0]["h"] == pytest.approx(101 * 0.9)
    assert all(b["v"] == 1000 for b in bars)


def test_daily_to_ohlc_bars_skips_factors_for_yfinance_adjusted_series():
    daily = _daily(["2026-01-01"], [100.0], adjusted=True)
    assert rps.daily_to_ohlc_bars(daily, ["2026-01-02"], [0.5])[0]["c"] == 100.0


def _bars_to_daily(bars):
    return {"t": [_ts(b["date"]) for b in bars], "o": [b["o"] for b in bars], "h": [b["h"] for b in bars],
            "l": [b["l"] for b in bars], "c": [b["c"] for b in bars], "v": [b["v"] for b in bars],
            "adjusted": False, "source": "finmind"}


def test_build_scan_codes_mode_evaluates_and_sorts():
    good = _bars_to_daily(_setup_bars())
    fetched = []

    def fake_fetch(code):
        fetched.append(code)
        if code == "9999":
            raise RuntimeError("boom")
        return good

    payload = rps.build_scan({}, top_n=5, max_fetch=10, codes=["2330", "9999"], fetch_fn=fake_fetch,
                             industries={"2330": ["半導體業"]}, names={"2330": "台積電"}, sleep=0)
    assert fetched == ["2330", "9999"]
    assert payload["mode"] == "codes"
    assert payload["counts"]["setups"] == 1 and payload["counts"]["failed"] == 1
    s = payload["setups"][0]
    assert s["code"] == "2330" and s["name"] == "台積電" and s["theme"] == "半導體業"
    assert s["status"] == "near_support" and s["status_label"]
    # slimmed: zone levels carry only kind/label/price
    assert set(s["zone"]["levels"][0]) <= {"kind", "label", "price"}
    assert "all_levels" not in s


def test_build_scan_screen_mode_respects_max_fetch():
    n = 130
    universe = {}
    for k in range(10):
        bars = [{"date": f"d{i:03d}", "c": 100 + i * (1 + k) * 0.8, "v": 1_000_000} for i in range(n)]
        universe[f"{1000 + k}"] = {"bars": bars}
    calls = []

    def fake_fetch(code):
        calls.append(code)
        return None

    payload = rps.build_scan(universe, top_n=5, max_fetch=2, fetch_fn=fake_fetch,
                             industries={}, names={}, sleep=0)
    assert payload["mode"] == "screen"
    assert payload["counts"]["strong"] >= 2
    assert len(calls) == 2
    # strongest first
    assert calls[0] == "1009"


def test_build_scan_empty_universe_in_screen_mode():
    assert rps.build_scan({}, 5, 5, fetch_fn=lambda c: None, industries={}, names={}, sleep=0) is None


def test_summary_text_lists_near_support():
    payload = rps.build_scan({}, 5, 5, codes=["2330"], fetch_fn=lambda c: _bars_to_daily(_setup_bars()),
                             industries={}, names={"2330": "台積電"}, sleep=0)
    payload["as_of"] = payload["as_of"] or "x"
    text = rps.summary_text(payload)
    assert "接近支撐 1 檔" in text and "2330 台積電" in text


def test_unadjusted_jump_flags_split_after_cache_only():
    bars = [{"date": f"2026-09-{d:02d}", "c": c} for d, c in
            [(1, 100), (2, 30), (3, 31), (4, 31.5)]]   # 1:3 split on 09-02
    assert rps.unadjusted_jump(bars) == "2026-09-02"
    assert rps.unadjusted_jump(bars, after_date="2026-09-02") is None  # factor cache already covers it
    calm = [{"date": f"2026-09-{d:02d}", "c": c} for d, c in [(1, 100), (2, 109.5), (3, 99)]]
    assert rps.unadjusted_jump(calm) is None


def test_build_scan_skips_code_with_unadjusted_split():
    bars = _setup_bars()
    for b in bars[-10:]:
        for k in ("o", "h", "l", "c"):
            b[k] /= 3
    payload = rps.build_scan({}, 5, 5, codes=["6669"], fetch_fn=lambda c: _bars_to_daily(bars),
                             industries={}, names={}, sleep=0)
    assert payload["counts"]["unadjusted"] == 1 and payload["setups"] == []


def test_fetch_failure_too_high_flags_outage_not_quiet_market():
    assert rps.fetch_failure_too_high({"candidates": 10, "failed": 6}, max_fetch=120)
    assert not rps.fetch_failure_too_high({"candidates": 10, "failed": 5}, max_fetch=120)
    # max_fetch caps the denominator: 60 of the first 100 attempted failed
    assert rps.fetch_failure_too_high({"candidates": 500, "failed": 60}, max_fetch=100)
    # nothing attempted / no failures is never an outage
    assert not rps.fetch_failure_too_high({"candidates": 0, "failed": 0}, max_fetch=120)
    assert not rps.fetch_failure_too_high({"candidates": 10, "failed": 0}, max_fetch=120)
