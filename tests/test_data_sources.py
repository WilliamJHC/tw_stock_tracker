# -*- coding: utf-8 -*-
"""Unit tests for data_sources.py FinMind helpers. No real network calls:
requests.get is patched, and a side_effect guards against unexpected use."""


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


def test_finmind_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(ds, "FINMIND_RETRY_DELAYS", (0, 0))  # no real sleep
    rows = [{"date": "2026-01-02", "close": 100}]
    responses = [
        _fake_response(status=503, body_status=503, msg="temporarily down"),
        _fake_response(data=rows),
    ]
    with patch("data_sources.requests.get", side_effect=responses) as mock_get:
        result = ds._finmind("TaiwanStockPrice", "2330", "2026-01-01")
    assert result == rows
    assert mock_get.call_count == 2


def test_finmind_no_retry_on_client_error(monkeypatch):
    monkeypatch.setattr(ds, "FINMIND_RETRY_DELAYS", (0, 0))
    resp = _fake_response(status=402, body_status=402, msg="quota exceeded")
    with patch("data_sources.requests.get", return_value=resp) as mock_get:
        with pytest.raises(ds.SourceError):
            ds._finmind("TaiwanStockPrice", "2330", "2026-01-01")
    assert mock_get.call_count == 1  # quota errors fail fast, no retry


def test_finmind_no_retry_on_rate_limit(monkeypatch):
    # 429 can only clear when the hourly window rolls over, so retrying would
    # spend three requests of an exhausted quota instead of one.
    monkeypatch.setattr(ds, "FINMIND_RETRY_DELAYS", (0, 0))
    resp = _fake_response(status=429, body_status=429, msg="rate limit exceeded")
    with patch("data_sources.requests.get", return_value=resp) as mock_get:
        with pytest.raises(ds.SourceError):
            ds._finmind("TaiwanStockPrice", "2330", "2026-01-01")
    assert mock_get.call_count == 1


def test_finmind_stops_retrying_once_budget_is_spent(monkeypatch):
    # A zero budget stands in for "the first request ate the whole window":
    # retries must not extend a call past FINMIND_CALL_BUDGET.
    monkeypatch.setattr(ds, "FINMIND_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(ds, "FINMIND_CALL_BUDGET", 0.0)
    resp = _fake_response(status=503, body_status=503, msg="temporarily down")
    with patch("data_sources.requests.get", return_value=resp) as mock_get:
        with pytest.raises(ds.SourceError):
            ds._finmind("TaiwanStockPrice", "2330", "2026-01-01")
    assert mock_get.call_count == 1
    # the one attempt still gets a usable, bounded timeout
    assert 0 < mock_get.call_args.kwargs["timeout"] <= ds.FINMIND_TIMEOUT


def test_data_tier_reflects_token_presence(monkeypatch):
    monkeypatch.setattr(ds, "FINMIND_TOKEN", "some-token")
    assert ds.data_tier() == "premium"
    monkeypatch.setattr(ds, "FINMIND_TOKEN", "")
    assert ds.data_tier() == "free"


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


def test_get_margin_error_returns_none(monkeypatch):
    monkeypatch.setattr(ds, "FINMIND_RETRY_DELAYS", ())  # skip back-off sleeps
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


# ----------------------------------------------------------- get_universe --

def test_get_universe_keeps_4digit_twse_tpex_dedupes_by_code():
    rows = [
        {"stock_id": "2330", "stock_name": "台積電", "type": "twse", "date": "2020-01-01"},
        {"stock_id": "2330", "stock_name": "台積電", "type": "twse", "date": "2021-01-01"},  # dup, later date
        {"stock_id": "5481", "stock_name": "先豐", "type": "tpex", "date": "2020-01-01"},
        {"stock_id": "00631L", "stock_name": "元大台灣50正2", "type": "twse", "date": "2020-01-01"},  # not 4-digit
        {"stock_id": "TXFF4", "stock_name": "期貨", "type": "future", "date": "2020-01-01"},  # not twse/tpex
    ]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)):
        result = ds.get_universe()
    assert result == [
        {"code": "2330", "name": "台積電", "market": "twse"},
        {"code": "5481", "name": "先豐", "market": "tpex"},
    ]


def test_get_universe_no_data_returns_none():
    with patch("data_sources.requests.get", return_value=_fake_response(data=[])):
        result = ds.get_universe()
    assert result is None


# --------------------------------------------------- get_monthly_revenue --

def test_get_monthly_revenue_builds_date_from_year_month_not_raw_date():
    rows = [
        {"date": "2024-01-01", "revenue_year": 2023, "revenue_month": 12, "revenue": 176299866000},
        {"date": "2023-12-01", "revenue_year": 2023, "revenue_month": 11, "revenue": 150000000000},
    ]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)):
        result = ds.get_monthly_revenue("2330")
    assert result == [
        {"date": "2023-11-01", "revenue": 150000000000.0, "announce_date": None},
        {"date": "2023-12-01", "revenue": 176299866000.0, "announce_date": None},
    ]


def test_get_monthly_revenue_picks_up_nonempty_create_time_as_announce_date():
    rows = [
        {"date": "2024-01-01", "revenue_year": 2023, "revenue_month": 12, "revenue": 176299866000,
         "create_time": "2024-01-10"},
        {"date": "2023-12-01", "revenue_year": 2023, "revenue_month": 11, "revenue": 150000000000,
         "create_time": ""},
    ]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)):
        result = ds.get_monthly_revenue("2330")
    assert result == [
        {"date": "2023-11-01", "revenue": 150000000000.0, "announce_date": None},
        {"date": "2023-12-01", "revenue": 176299866000.0, "announce_date": "2024-01-10"},
    ]


def test_get_monthly_revenue_no_data_returns_none():
    with patch("data_sources.requests.get", return_value=_fake_response(data=[])):
        result = ds.get_monthly_revenue("2330")
    assert result is None


# -------------------------------------------------------- get_financials --

def test_get_financials_joins_income_and_equity_by_date():
    income_rows = [
        {"date": "2023-03-31", "type": "IncomeAfterTaxes", "value": 100.0},
        {"date": "2023-06-30", "type": "IncomeAfterTaxes", "value": 120.0},
        {"date": "2023-06-30", "type": "GrossProfit", "value": 999.0},  # rides along, see below
    ]
    equity_rows = [
        {"date": "2023-03-31", "type": "Equity", "value": 1000.0},
        {"date": "2023-06-30", "type": "Equity", "value": 1100.0},
    ]
    with patch("data_sources.requests.get", side_effect=[
        _fake_response(data=income_rows), _fake_response(data=equity_rows),
    ]):
        result = ds.get_financials("2330")
    assert result == [
        {"date": "2023-03-31", "net_income": 100.0, "equity": 1000.0,
         "revenue": None, "gross_profit": None, "operating_income": None},
        {"date": "2023-06-30", "net_income": 120.0, "equity": 1100.0,
         "revenue": None, "gross_profit": 999.0, "operating_income": None},
    ]


def test_get_financials_margin_fields_ride_along_zero_extra_requests():
    income_rows = [
        {"date": "2023-03-31", "type": "IncomeAfterTaxes", "value": 100.0},
        {"date": "2023-03-31", "type": "Revenue", "value": 500.0},
        {"date": "2023-03-31", "type": "GrossProfit", "value": 200.0},
        {"date": "2023-03-31", "type": "OperatingIncome", "value": 150.0},
    ]
    equity_rows = [{"date": "2023-03-31", "type": "Equity", "value": 1000.0}]
    with patch("data_sources.requests.get", side_effect=[
        _fake_response(data=income_rows), _fake_response(data=equity_rows),
    ]) as mock_get:
        result = ds.get_financials("2330")
    assert mock_get.call_count == 2  # same 2 requests as before this extension
    assert result == [{
        "date": "2023-03-31", "net_income": 100.0, "equity": 1000.0,
        "revenue": 500.0, "gross_profit": 200.0, "operating_income": 150.0,
    }]


def test_get_financials_drops_dates_missing_either_side():
    income_rows = [{"date": "2023-03-31", "type": "IncomeAfterTaxes", "value": 100.0}]
    equity_rows = []  # no equity at all -> no usable date
    with patch("data_sources.requests.get", side_effect=[
        _fake_response(data=income_rows), _fake_response(data=equity_rows),
    ]):
        result = ds.get_financials("2330")
    assert result is None


# -------------------------------------------------------------- get_per_pbr --

def test_get_per_pbr_casts_string_numbers_to_float():
    # FinMind's TaiwanStockPER can return PER/PBR as numeric strings (same
    # reason get_pe() -- this module's other TaiwanStockPER caller -- casts
    # explicitly); finlab_factors.value_series() does `-row["pbr"]`, which
    # raises TypeError on an uncast str.
    rows = [{"date": "2024-01-02", "PER": "17.17", "PBR": "4.59", "dividend_yield": "1.85"}]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)):
        result = ds.get_per_pbr("2330")
    assert result == [{"date": "2024-01-02", "per": 17.17, "pbr": 4.59}]


def test_get_per_pbr_missing_values_become_none():
    rows = [{"date": "2024-01-02", "PER": None, "PBR": None}]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)):
        result = ds.get_per_pbr("2330")
    assert result == [{"date": "2024-01-02", "per": None, "pbr": None}]


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


# ------------------------------------------------------------------ fugle --
# _fugle_daily()'s endpoint/header/response-shape/raw-scale assumptions were
# confirmed 2026-08-18 against a live key (see its docstring). These tests
# still run mocked (no network) -- they lock down this module's OWN contract
# (inert without a key, raises/parses as expected when it does call out).

def test_fugle_daily_inert_without_api_key(monkeypatch):
    monkeypatch.setattr(ds, "FUGLE_API_KEY", "")
    with patch("data_sources.requests.get", side_effect=_no_network):
        with pytest.raises(ds.SourceError):
            ds._fugle_daily("2330", "2024-01-01")


def test_fugle_daily_parses_candles(monkeypatch):
    monkeypatch.setattr(ds, "FUGLE_API_KEY", "fake-key")
    rows = [
        {"date": "2024-01-02", "open": 590.0, "high": 593.0, "low": 589.0, "close": 593.0, "volume": 27997826},
        {"date": "2024-01-03", "open": 584.0, "high": 585.0, "low": 576.0, "close": 578.0, "volume": 40134497},
    ]
    with patch("data_sources.requests.get", return_value=_fake_response(data=rows)) as mock_get:
        hist = ds._fugle_daily("2330", "2024-01-01")
    assert hist["c"] == [593.0, 578.0]
    assert hist["v"] == [27997826, 40134497]
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["headers"]["X-API-KEY"] == "fake-key"
    assert "2330" in mock_get.call_args.args[0]


def test_fugle_daily_empty_response_raises(monkeypatch):
    monkeypatch.setattr(ds, "FUGLE_API_KEY", "fake-key")
    with patch("data_sources.requests.get", return_value=_fake_response(data=[])):
        with pytest.raises(ds.SourceError):
            ds._fugle_daily("2330", "2024-01-01")


def test_load_fugle_key_prefers_env_var(monkeypatch):
    monkeypatch.setenv("FUGLE_API_KEY", "env-fugle-key")
    assert ds._load_fugle_key() == "env-fugle-key"


def test_load_fugle_key_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("FUGLE_API_KEY", raising=False)
    monkeypatch.setattr(ds, "BASE_DIR", str(tmp_path))
    assert ds._load_fugle_key() == ""


# ------------------------------------------------------------- get_quote --
# MIS's own JSON shape (top-level msgArray), not FinMind's {status, data}.

def _fake_mis_response(msg_array):
    class FakeResponse:
        def json(self):
            return {"msgArray": msg_array}

    return FakeResponse()


def test_get_quote_uses_z_when_available():
    item = {"c": "2330", "n": "台積電", "z": "600.0000", "pz": "-", "y": "590.0000", "v": "12345"}
    with patch("data_sources.requests.get", return_value=_fake_mis_response([item])):
        q = ds.get_quote("2330")
    assert q["price"] == 600.0
    assert q["prev_close"] == 590.0
    assert q["source"] == "twse-mis"


def test_get_quote_falls_back_to_bid_ask_midpoint_when_z_and_pz_missing():
    # Reproduces the live MIS quirk documented in realtime/mis_source.py's
    # KNOWN GAP: z and pz both "-" mid-session while the order book is still
    # populated. Before this fix get_quote() fell straight through to `y`
    # (prev close), silently showing an intraday quote as unchanged.
    item = {
        "c": "2330", "n": "台積電", "z": "-", "pz": "-", "y": "2380.0000", "v": "6532",
        "b": "2345.0000_2340.0000_", "g": "713_1442_",
        "a": "2350.0000_2355.0000_", "f": "333_353_",
    }
    with patch("data_sources.requests.get", return_value=_fake_mis_response([item])):
        q = ds.get_quote("2330")
    assert q["price"] == 2347.5  # midpoint of best bid 2345 / best ask 2350
    assert q["prev_close"] == 2380.0
    assert q["source"] == "twse-mis-midpoint"


def test_get_quote_falls_back_to_prev_close_when_no_book_either():
    item = {"c": "2330", "n": "台積電", "z": "-", "pz": "-", "y": "2380.0000", "v": "0"}
    with patch("data_sources.requests.get", return_value=_fake_mis_response([item])):
        q = ds.get_quote("2330")
    assert q["price"] == 2380.0
    assert q["source"] == "twse-mis"
