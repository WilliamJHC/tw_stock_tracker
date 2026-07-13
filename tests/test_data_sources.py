# -*- coding: utf-8 -*-
"""Unit tests for data_sources.py FinMind helpers. No real network calls:
requests.get is patched, and a side_effect guards against unexpected use."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import pytest

import data_sources as ds


def _fake_response(status=200, body_status=200, data=None, msg=None):
    class FakeResponse:
        status_code = status

        def json(self):
            return {"status": body_status, "data": data or [], "msg": msg}

    return FakeResponse()


def _no_network(*args, **kwargs):
    raise AssertionError("unexpected real network call via requests.get")


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "CACHE_DIR", str(tmp_path))


# ------------------------------------------------------------- _finmind --

def test_finmind_raises_on_error_status():
    with patch("data_sources.requests.get", return_value=_fake_response(status=402, body_status=402, msg="quota exceeded")):
        with pytest.raises(ds.SourceError):
            ds._finmind("TaiwanStockPrice", "2330", "2026-01-01")


def test_finmind_ok_returns_data():
    rows = [{"date": "2026-01-02", "close": 100}]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)):
        result = ds._finmind("TaiwanStockPrice", "2330", "2026-01-01")
    assert result == rows


# -------------------------------------------------------------- get_margin --

def test_get_margin_parses_and_converts_lots_to_shares():
    rows = [
        {
            "date": "2026-01-05",
            "MarginPurchaseTodayBalance": "100",
            "MarginPurchaseYesterdayBalance": "90",
            "ShortSaleTodayBalance": "10",
            "ShortSaleYesterdayBalance": "8",
        }
    ]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)):
        result = ds.get_margin("2330")
    assert result == [{
        "date": "2026-01-05",
        "margin_balance": 100_000,
        "margin_delta": 10_000,
        "short_balance": 10_000,
        "short_delta": 2_000,
    }]


def test_get_margin_defensive_conversion_handles_null_and_string():
    rows = [
        {
            "date": "2026-01-05",
            "MarginPurchaseTodayBalance": None,
            "MarginPurchaseYesterdayBalance": "0",
            "ShortSaleTodayBalance": "5.0",
            "ShortSaleYesterdayBalance": None,
        }
    ]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)):
        result = ds.get_margin("2330")
    assert result[0]["margin_balance"] == 0
    assert result[0]["short_balance"] == 5000


def test_get_margin_no_data_returns_none():
    with patch("data_sources.requests.get", return_value=_fake_response(data=[])):
        result = ds.get_margin("2330")
    assert result is None


def test_get_margin_error_returns_none():
    with patch("data_sources.requests.get", return_value=_fake_response(status=500, body_status=500)):
        result = ds.get_margin("2330")
    assert result is None


# --------------------------------------------------------- get_shareholding --

def test_get_shareholding_parses_ratio_and_shares():
    rows = [{
        "date": "2026-01-05",
        "ForeignInvestmentSharesRatio": "72.5",
        "ForeignInvestmentShares": "1000000",
        "NumberOfSharesIssued": "1379735317",
    }]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)):
        result = ds.get_shareholding("2330")
    assert result == [{
        "date": "2026-01-05",
        "foreign_ratio": 72.5,
        "foreign_shares": 1_000_000,
    }]


def test_get_shareholding_no_data_returns_none():
    with patch("data_sources.requests.get", return_value=_fake_response(data=[])):
        result = ds.get_shareholding("2330")
    assert result is None


# ------------------------------------------------------------------ get_sbl --

def test_get_sbl_does_not_multiply_by_1000():
    rows = [{
        "date": "2026-01-05",
        "SBLShortSalesCurrentDayBalance": "11358514",
        "SBLShortSalesPreviousDayBalance": "11000000",
        "SBLShortSalesShortSales": "400000",
        "SBLShortSalesReturns": "41486",
    }]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)):
        result = ds.get_sbl("2330")
    assert result == [{
        "date": "2026-01-05",
        "sbl_balance": 11_358_514,
        "sbl_sales": 400_000,
        "sbl_returns": 41_486,
    }]


def test_get_sbl_no_data_returns_none():
    with patch("data_sources.requests.get", return_value=_fake_response(data=[])):
        result = ds.get_sbl("2330")
    assert result is None


# ------------------------------------------------------------ caching path --

def test_get_margin_uses_same_day_cache_without_second_call():
    rows = [{
        "date": "2026-01-05",
        "MarginPurchaseTodayBalance": "50",
        "MarginPurchaseYesterdayBalance": "50",
        "ShortSaleTodayBalance": "0",
        "ShortSaleYesterdayBalance": "0",
    }]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)) as mock_get:
        first = ds.get_margin("2330")
        assert mock_get.call_count == 1
    # second call must not hit the network at all
    with patch("data_sources.requests.get", side_effect=_no_network):
        second = ds.get_margin("2330")
    assert first == second


# --------------------------------------------------------------- _load_token --

def test_load_token_prefers_env_var(monkeypatch):
    monkeypatch.setenv("FINMIND_TOKEN", "env-token-value")
    assert ds._load_token() == "env-token-value"


def test_load_token_strips_quotes_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    (tmp_path / ".env").write_text('FINMIND_TOKEN="quoted-token"\n', encoding="utf-8")
    monkeypatch.setattr(ds, "BASE_DIR", str(tmp_path))
    assert ds._load_token() == "quoted-token"


def test_load_token_skips_comment_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    (tmp_path / ".env").write_text(
        "# FINMIND_TOKEN=should-be-ignored\n"
        "\n"
        "FINMIND_TOKEN=real-token\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ds, "BASE_DIR", str(tmp_path))
    assert ds._load_token() == "real-token"


def test_load_token_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    monkeypatch.setattr(ds, "BASE_DIR", str(tmp_path))
    assert ds._load_token() == ""
