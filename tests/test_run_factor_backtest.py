# -*- coding: utf-8 -*-
"""Unit/contract tests for run_factor_backtest.py's public cross-script
interface (code_cache_path()/daily_to_bars()/augment_bars_with_adj_factor())
and a regression test for the exact call shape scripts/update_universe_bars.py
depends on. Commit 8a504f0 crashed the weekly refresh because that call site
drifted out of sync with a signature change here and nothing caught it --
test_update_code_matches_augment_bars_signature() below reruns that same
call path so a future drift fails a test instead of a scheduled job.

No real network calls: build_adj_factors.resolve_adjustment_actions() (the
one place augment_bars_with_adj_factor() reaches Yahoo/TWSE) and
data_sources.get_daily() are both patched.
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import pytest  # noqa: E402

import run_factor_backtest as rfb  # noqa: E402
import update_universe_bars as uub  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(rfb, "RUN_CACHE_DIR", str(tmp_path))


def _bar(date, c, v=1000):
    return {"date": date, "c": c, "v": v}


def test_code_cache_path_joins_run_cache_dir():
    assert rfb.code_cache_path("2330") == os.path.join(rfb.RUN_CACHE_DIR, "2330.json")


def test_daily_to_bars_converts_ascending():
    ts = [1755561600, 1755648000]
    daily = {"t": ts, "o": [99.0, 100.5], "h": [102.0, 103.0], "l": [98.0, 100.0], "c": [100.0, 101.0], "v": [1000, 2000]}
    bars = rfb.daily_to_bars(daily)
    expected_dates = [dt.datetime.fromtimestamp(t).date().isoformat() for t in ts]
    assert [b["date"] for b in bars] == expected_dates
    assert [b["c"] for b in bars] == [100.0, 101.0]
    assert [b["v"] for b in bars] == [1000, 2000]
    # Raw o/h/l ride along with c so the pullback scan / future event study
    # can see amplitude, gaps and AVWAP anchors from the cache alone.
    assert [b["o"] for b in bars] == [99.0, 100.5]
    assert [b["h"] for b in bars] == [102.0, 103.0]
    assert [b["l"] for b in bars] == [98.0, 100.0]


def test_merge_bars_new_fetch_brings_ohl_and_old_bars_stay_untouched():
    old = [_bar("2026-08-01", 90.0), {**_bar("2026-08-02", 91.0), "c_adj": 91.0}]
    new = [{"date": "2026-08-02", "o": 90.5, "h": 92.0, "l": 90.0, "c": 91.5, "v": 1100},
           {"date": "2026-08-03", "o": 91.5, "h": 93.0, "l": 91.0, "c": 92.0, "v": 900}]
    merged = uub._merge_bars(old, new)
    assert [b["date"] for b in merged] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert "o" not in merged[0]                                      # outside the fetch window: still legacy
    assert merged[1] == new[0]                                       # overlap: new wins, stale c_adj dropped
    assert (merged[2]["o"], merged[2]["h"], merged[2]["l"]) == (91.5, 93.0, 91.0)
    assert uub.ohl_coverage(merged) == (2, 3)


def test_merge_bars_tolerates_new_bars_without_ohl():
    merged = uub._merge_bars([], [{"date": "2026-08-03", "c": 92.0, "v": 900}])
    assert merged == [{"date": "2026-08-03", "c": 92.0, "v": 900}]


def test_update_code_lookback_days_is_passed_through_and_backfills_ohl(monkeypatch):
    """--lookback-days 1800 is the one-off backfill: legacy bars inside the
    fetched window gain o/h/l, and the detail line reports the coverage."""
    rfb.save_code_cache("2330", {"bars": [_bar("2026-08-17", 99.0), _bar("2026-08-18", 100.0)], "days": 1800})
    seen = {}

    def fake_get_daily(code, days, cache):
        seen["days"] = days
        return {"t": [int(dt.datetime(2026, 8, 17).timestamp()), int(dt.datetime(2026, 8, 18).timestamp())],  # local tz, like daily_to_bars()
                "o": [98.0, 99.5], "h": [100.0, 101.0], "l": [97.5, 99.0], "c": [99.0, 100.0], "v": [1000, 1000]}

    monkeypatch.setattr(uub.ds, "get_daily", fake_get_daily)
    monkeypatch.setattr(rfb.baf, "resolve_adjustment_actions",
                        lambda code, market, bars, twse_events: ([], {"yahoo_ok": True}))
    result = uub.update_code("2330", rfb.RateLimiter(), dry_run=False, market="twse", twse_events=[], lookback_days=1800)
    assert seen["days"] == 1800
    assert result["status"] == "updated"
    with open(rfb.code_cache_path("2330"), encoding="utf-8") as f:
        saved = json.load(f)["bars"]
    assert all("o" in b and "h" in b and "l" in b and "c_adj" in b for b in saved)
    assert "ohl=2/2" in result["detail"]


def test_augment_bars_with_adj_factor_attaches_c_adj_when_no_actions(monkeypatch):
    """No corporate actions in the window -> factor is uniformly 1.0 ->
    c_adj == raw close for every bar (adjust.py's "today's adjusted price ==
    today's raw price" invariant)."""
    monkeypatch.setattr(rfb.baf, "resolve_adjustment_actions",
                         lambda code, market, bars, twse_events: ([], {"yahoo_ok": True}))

    bars = [_bar("2026-08-17", 100.0), _bar("2026-08-18", 101.0)]
    out = rfb.augment_bars_with_adj_factor("2330", bars, rfb.RateLimiter(), "twse", [])
    assert [b["c_adj"] for b in out] == [100.0, 101.0]


def test_update_code_matches_augment_bars_signature(monkeypatch):
    """Regression test for commit 8a504f0: update_universe_bars.update_code()
    must call rfb.augment_bars_with_adj_factor() with the exact positional
    shape that function currently requires (code, bars, limiter, market,
    twse_events). If a future signature change here isn't mirrored in
    update_code(), this raises TypeError and fails the test suite instead of
    silently degrading every code to status=failed in production."""
    code = "2330"
    rfb.save_code_cache(code, {
        "bars": [_bar("2026-08-01", 90.0)],
        "revenue_series": [], "roe_series": [], "per_pbr_series": [], "days": 1800,
    })

    monkeypatch.setattr(uub.ds, "get_daily", lambda code, days, cache: {
        "t": [1755561600], "o": [100.0], "h": [100.0], "l": [100.0], "c": [100.0], "v": [1000],
    })
    monkeypatch.setattr(rfb.baf, "resolve_adjustment_actions",
                         lambda code, market, bars, twse_events: ([], {"yahoo_ok": True}))

    result = uub.update_code(code, rfb.RateLimiter(), dry_run=False, market="twse", twse_events=[])

    assert result["status"] == "updated"
    assert "c_adj=yes" in result["detail"]
