# -*- coding: utf-8 -*-
"""Unit tests for pipeline.py's build_watchlist_payloads() -- the new batch
assembly layer that computes cross-stock 5-day RS ranking (analysis.rs_ranking())
and feeds it back into each stock's build_stock_payload() call. No real
network calls: data_sources.get_daily and pipeline.build_stock_payload are
monkeypatched.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_sources as ds
import db
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


# --------------------------------------------------- build_suggestions (Phase 5) --

def _fake_payload(code, name, action, priority, signals=None):
    return {
        "code": code, "name": name,
        "attention": {"action": action, "priority": priority},
        "analysis": {"signals": signals or []},
    }


def test_build_suggestions_graceful_when_db_empty(monkeypatch, tmp_path):
    # db.connect()/init_db() just creates a fresh, empty DB -- no exception
    # path to test here, only that empty query results degrade correctly.
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "backtest.db"))
    monkeypatch.setattr(pl, "build_watchlist_payloads",
                        lambda codes: {"2330": _fake_payload("2330", "台積電", "觀察", 30)})

    out = pl.build_suggestions(codes=["2330"])
    assert len(out) == 1
    item = out[0]
    assert "error" not in item
    assert item["confidence"] is None
    assert item["expected_ret"] is None
    assert item["sample_n"] == 0
    assert item["strategy"] is None
    assert item["evidence"] == []
    assert item["data_as_of"] is None


def test_build_suggestions_passes_through_per_code_error(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "backtest.db"))
    monkeypatch.setattr(pl, "build_watchlist_payloads", lambda codes: {"BAD": {"error": "抓不到資料"}})

    out = pl.build_suggestions(codes=["BAD"])
    assert out[0]["code"] == "BAD"
    assert out[0]["error"] == "抓不到資料"


def test_build_suggestions_wires_real_db_signal_stats_and_strategy(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "backtest.db"))
    conn = db.connect()
    db.init_db(conn)
    db.upsert_signal_stats(conn, "2330", [
        {"signal": "均線多頭排列", "horizon": 20, "n": 40, "wins": 28, "win_rate": 70.0,
         "avg_ret": 5.0, "computed_at": "2024-12-31T00:00:00"},
    ])
    db.upsert_strategy_run(conn, "run1", "2330", {
        "params_json": "{}", "n_trades": 5, "win_rate": 60.0, "avg_ret": 2.0,
        "total_return": 20.0, "buy_hold_return": 15.0, "max_drawdown": -5.0,
        "start_date": "2024-01-01", "end_date": "2024-12-31", "computed_at": "2024-12-31T00:00:00",
    })
    db.set_meta(conn, "last_run_at", "2024-12-31T00:00:00")
    conn.close()

    monkeypatch.setattr(pl, "build_watchlist_payloads", lambda codes: {
        "2330": _fake_payload("2330", "台積電", "準備進場", 60,
                              signals=[{"text": "均線多頭排列", "bias": 1, "bt": "均線多頭排列"}]),
    })

    out = pl.build_suggestions(codes=["2330"])
    item = out[0]
    assert item["action"] == "準備進場" and item["priority"] == 60
    assert item["confidence"] == 70.0
    assert item["sample_n"] == 40
    assert item["strategy"]["total_return"] == 20.0
    assert item["strategy"]["n_trades"] == 5
    assert item["evidence"] == [{"signal": "均線多頭排列", "horizon": 20, "n": 40,
                                 "win_rate": 70.0, "avg_ret": 5.0}]
    assert item["data_as_of"] == "2024-12-31T00:00:00"
