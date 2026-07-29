# -*- coding: utf-8 -*-
"""Unit tests for pipeline.py's build_watchlist_payloads() -- the new batch
assembly layer that computes cross-stock 5-day RS ranking (analysis.rs_ranking())
and feeds it back into each stock's build_stock_payload() call. No real
network calls: data_sources.get_daily and pipeline.build_stock_payload are
monkeypatched.
"""


import data_sources as ds
import pipeline as pl


def test_build_watchlist_payloads_computes_rs_rank(monkeypatch):
    daily_data = {
        "A": {"c": [100, 101, 102, 103, 104, 110]},  # 5d return +10%
        "B": {"c": [50, 50, 50, 50, 50, 49]},         # 5d return -2%
    }
    monkeypatch.setattr(ds, "get_daily", lambda code, *a, **kw: daily_data[code])
    captured = {}

    def fake_build(code, rs_info=None):
        captured[code] = rs_info
        return {"code": code, "analysis": {}}

    monkeypatch.setattr(pl, "build_stock_payload", fake_build)
    out = pl.build_watchlist_payloads(["A", "B"])

    assert out["A"]["code"] == "A" and out["B"]["code"] == "B"
    assert captured["A"]["rank"] == 1 and captured["A"]["percentile"] == 100.0
    assert captured["B"]["rank"] == 2 and captured["B"]["percentile"] == 0.0


def test_build_watchlist_payloads_single_code_no_rs(monkeypatch):
    # rs_ranking() only means something with >=2 codes; a lone code gets no rs_info
    monkeypatch.setattr(ds, "get_daily", lambda code, *a, **kw: {"c": [100] * 6})
    captured = {}

    def fake_build(code, rs_info=None):
        captured[code] = rs_info
        return {"code": code}

    monkeypatch.setattr(pl, "build_stock_payload", fake_build)
    pl.build_watchlist_payloads(["A"])
    assert captured["A"] is None


def test_build_watchlist_payloads_records_error_per_code(monkeypatch):
    monkeypatch.setattr(ds, "get_daily", lambda code, *a, **kw: {"c": [100] * 6})

    def fake_build(code, rs_info=None):
        if code == "BAD":
            raise ds.SourceError("boom")
        return {"code": code}

    monkeypatch.setattr(pl, "build_stock_payload", fake_build)
    out = pl.build_watchlist_payloads(["A", "BAD"])
    assert out["A"] == {"code": "A"}
    assert "error" in out["BAD"]


def test_build_watchlist_payloads_get_daily_failure_skips_rs_for_that_code(monkeypatch):
    def flaky_get_daily(code, *a, **kw):
        if code == "NODATA":
            raise ds.SourceError("no data")
        return {"c": [100, 100, 100, 100, 100, 105]}

    monkeypatch.setattr(ds, "get_daily", flaky_get_daily)
    captured = {}

    def fake_build(code, rs_info=None):
        captured[code] = rs_info
        return {"code": code}

    monkeypatch.setattr(pl, "build_stock_payload", fake_build)
    pl.build_watchlist_payloads(["NODATA", "A"])
    # only one code has a usable 5d return -> rs_ranking() needs >=2, so neither gets ranked
    assert captured["NODATA"] is None
    assert captured["A"] is None
