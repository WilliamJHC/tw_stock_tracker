# -*- coding: utf-8 -*-
"""Unit tests for pipeline.py's build_watchlist_payloads() -- the new batch
assembly layer that computes cross-stock 5-day RS ranking (analysis.rs_ranking())
and feeds it back into each stock's build_stock_payload() call. No real
network calls: data_sources.get_daily and pipeline.build_stock_payload are
monkeypatched.
"""


import json

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


# ------------------------------------- build_recommendations (Phase 2, ADR 0001) --

def test_build_recommendations_no_meta_returns_empty_gracefully(monkeypatch, tmp_path):
    """Before scripts/run_daily_recommendations.py has ever run,
    meta.daily_recommendations doesn't exist -- must degrade to an empty
    list + as_of=None, not raise."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "backtest.db"))
    assert pl.build_recommendations() == {"recommendations": [], "as_of": None}


def test_build_recommendations_enriches_with_quote_and_target_price(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "backtest.db"))
    conn = db.connect()
    db.init_db(conn)
    db.set_meta(conn, "daily_recommendations", json.dumps({
        "as_of": "2026-08-20",
        "generated_at": "2026-08-24T07:00:00",
        "codes": [
            {"code": "2330", "name": "台積電", "composite_score": 0.9,
             "factor_ranks": {"revenue": 0.9, "roe": 0.8, "momentum": 0.7}},
        ],
    }))
    conn.close()

    def fake_build_watchlist_payloads(codes):
        assert codes == ["2330"]
        return {"2330": {
            "quote": {"price": 950.0, "name": "台積電"},
            "analysis": {"target_price": {"level": 960.0, "basis": "VAH", "as_of": "2026-08-20"}},
        }}
    monkeypatch.setattr(pl, "build_watchlist_payloads", fake_build_watchlist_payloads)
    monkeypatch.setattr(pl, "load_watchlist", lambda: [{"code": "2330", "name": "台積電"}])

    out = pl.build_recommendations()
    assert out["as_of"] == "2026-08-20"
    assert len(out["recommendations"]) == 1
    r = out["recommendations"][0]
    assert r["code"] == "2330"
    assert r["name"] == "台積電"
    assert r["composite_score"] == 0.9
    assert r["factor_ranks"] == {"revenue": 0.9, "roe": 0.8, "momentum": 0.7}
    assert r["quote"]["price"] == 950.0
    assert r["target_price"] == {"level": 960.0, "basis": "VAH", "as_of": "2026-08-20"}
    assert r["tracked"] is True  # 2330 is in the (mocked) watchlist


def test_build_recommendations_marks_untracked_code_and_degrades_on_per_code_failure(monkeypatch, tmp_path):
    """A code that failed inside build_watchlist_payloads() comes back as
    {"error": ...} (build_watchlist_payloads()'s own per-code try/except
    convention) -- must not crash, just leave quote/target_price as None. A
    code not in the watchlist must report tracked=False (docs/dev_plan_
    recommendations.md scope decision 6: recommendations aren't excluded
    from overlapping the watchlist, but an untracked one must say so)."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "backtest.db"))
    conn = db.connect()
    db.init_db(conn)
    db.set_meta(conn, "daily_recommendations", json.dumps({
        "as_of": "2026-08-20",
        "generated_at": "2026-08-24T07:00:00",
        "codes": [{"code": "9999", "name": "測試股", "composite_score": 0.5,
                   "factor_ranks": {"revenue": 0.5, "roe": 0.5, "momentum": 0.5}}],
    }))
    conn.close()

    monkeypatch.setattr(pl, "build_watchlist_payloads",
                        lambda codes: {"9999": {"error": "抓不到日線資料"}})
    monkeypatch.setattr(pl, "load_watchlist", lambda: [{"code": "2330", "name": "台積電"}])

    out = pl.build_recommendations()
    r = out["recommendations"][0]
    assert r["quote"] is None
    assert r["target_price"] is None
    assert r["tracked"] is False
