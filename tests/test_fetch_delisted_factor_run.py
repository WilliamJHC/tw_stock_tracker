# -*- coding: utf-8 -*-
"""Tests for scripts/fetch_delisted_factor_run.py's pure logic. No network:
the four fetchers are injected."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import fetch_delisted_factor_run as fd  # noqa: E402


def _price_rows(n=40, start="2018-01-01", volume=5_000_000, close=100.0):
    import datetime as dt
    d0 = dt.date.fromisoformat(start)
    rows = []
    for i in range(n):
        d = d0 + dt.timedelta(days=i)
        rows.append({"date": d.isoformat(), "open": close, "max": close, "min": close,
                     "close": close, "Trading_Volume": volume})
    return rows


def test_candidates_filters_code_shape_and_date():
    rows = [
        {"stock_id": "2325", "stock_name": "矽品", "date": "2018-04-30"},
        {"stock_id": "00732", "stock_name": "ETF", "date": "2023-04-21"},   # 5-digit
        {"stock_id": "9157", "stock_name": "X-DR", "date": "2019-11-12"},
        {"stock_id": "1234", "stock_name": "old", "date": "2012-01-01"},    # before cutoff
        {"stock_id": "2361", "stock_name": "\t鴻友科技", "date": "2015-12-28"},
    ]
    got = fd.candidates(rows)
    assert [c["code"] for c in got] == ["2325", "2361", "9157"]
    assert got[1]["name"] == "鴻友科技"


def test_dividend_actions_ratio_and_filters():
    rows = [
        {"date": "2017-08-02", "before_price": 50.0, "reference_price": 48.25},
        {"date": "2016-01-01", "before_price": None, "reference_price": 10.0},
        {"date": "2015-01-01", "before_price": 10.0, "reference_price": 1.0},  # ratio 0.1: implausible
    ]
    got = fd.dividend_actions(rows)
    assert got == [{"date": "2017-08-02", "ratio": 48.25 / 50.0}]


def test_add_c_adj_scales_bars_before_ex_date():
    bars = [{"date": "2018-01-01", "c": 50.0}, {"date": "2018-01-02", "c": 50.0},
            {"date": "2018-01-03", "c": 45.0}]
    out, ok = fd.add_c_adj(bars, [{"date": "2018-01-03", "ratio": 0.9}])
    assert ok
    assert [round(b["c_adj"], 6) for b in out] == [45.0, 45.0, 45.0]


def _build(rows, delisted="2018-02-15", revenue=None, financials=None, dividends=None):
    cand = {"code": "9999", "name": "T", "delisted_date": delisted}
    return fd.build_entry(
        cand,
        lambda c: rows,
        lambda c: dividends or [],
        lambda c: revenue,
        lambda c: financials,
    )


def test_no_price():
    assert _build([])["status"] == "no_price"


def test_code_reused_when_bars_continue_past_delisting():
    assert _build(_price_rows(n=120), delisted="2018-02-01")["status"] == "code_reused"


def test_illiquid():
    assert _build(_price_rows(volume=10))["status"] == "illiquid"


def test_dark_when_fundamentals_missing():
    e = _build(_price_rows())
    assert e["status"] == "dark"
    assert e["bars"] and "c_adj" in e["bars"][0]
    assert e["revenue_series"] == [] and e["roe_series"] == []


def test_error_trap_records_and_reraises_source_errors():
    import pytest
    import data_sources as ds

    def boom(*a, **k):
        raise ds.SourceError("HTTP 402 / Requests reach the upper limit")

    trap = fd.ErrorTrap(boom)
    with pytest.raises(ds.SourceError):
        trap("TaiwanStockMonthRevenue", "1234", "2010-01-01")
    assert trap.errors == ["HTTP 402 / Requests reach the upper limit"]
    assert fd.ErrorTrap(lambda *a, **k: [1])("x") == [1]
