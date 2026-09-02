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
    daily = {"t": ts, "o": [1, 2], "h": [1, 2], "l": [1, 2], "c": [100.0, 101.0], "v": [1000, 2000]}
    bars = rfb.daily_to_bars(daily)
    expected_dates = [dt.datetime.fromtimestamp(t).date().isoformat() for t in ts]
    assert [b["date"] for b in bars] == expected_dates
    assert [b["c"] for b in bars] == [100.0, 101.0]
    assert [b["v"] for b in bars] == [1000, 2000]


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
