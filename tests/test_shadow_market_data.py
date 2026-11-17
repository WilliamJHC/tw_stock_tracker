# -*- coding: utf-8 -*-
"""Tests for shadow_market_data. No network: requests.get is replaced by a fake that serves fixtures from
tests/fixtures/shadow/ (shrunk copies of the real 2026-07-16 responses; see the module docstring)."""
import copy
import datetime
import json
import os

import pytest

import shadow_market_data as smd

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "shadow")
D = datetime.date(2026, 7, 16)
DEFAULT_INTERVAL = smd.REQUEST_INTERVAL  # captured before the autouse fixture zeroes it
DEFAULT_TIMEOUT = smd.TIMEOUT


def load(name):
    with open(os.path.join(FIX, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


class FakeResp:
    def __init__(self, payload=None, status=200, text=None):
        self.status_code = status
        self.encoding = None
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(smd, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(smd, "_last_request_at", None)
    # The fixtures are shrunk (38 four-digit codes), so lower the size gates; the gates themselves are
    # tested separately with the real thresholds.
    monkeypatch.setattr(smd, "TWSE_MIN_CODES", 30)
    monkeypatch.setattr(smd, "TPEX_MIN_CODES", 30)


@pytest.fixture
def serve(monkeypatch):
    """serve(payload | FakeResp | Exception) -> list collecting requested URLs."""
    calls = []

    def _serve(resp):
        def fake_get(url, **kw):
            calls.append((url, kw))
            if isinstance(resp, Exception):
                raise resp
            return resp if isinstance(resp, FakeResp) else FakeResp(resp)
        monkeypatch.setattr(smd.requests, "get", fake_get)
        return calls
    return _serve


# ------------------------------------------------------------------ TWSE close

class TestTwseClose:
    def test_normal_parse(self, serve):
        calls = serve(load("twse_close_20260716"))
        res = smd.fetch_twse_close(D)
        assert res is not None
        # only 4-digit numeric codes (0050 qualifies, 006208 / 00400A do not)
        assert "0050" in res and "006208" not in res and "00400A" not in res
        assert all(len(c) == 4 and c.isdigit() for c in res)
        row = res["1101"]
        assert set(row) == {"o", "h", "l", "c", "v", "status"}
        assert isinstance(row["c"], float) and isinstance(row["v"], int)
        assert row["status"] == "ok"
        url, kw = calls[0]
        assert "date=20260716" in url and "type=ALLBUT0999" in url
        assert kw["timeout"] == 40 and kw["headers"]["User-Agent"] == "Mozilla/5.0"

    def test_thousand_separators(self, serve):
        serve(load("twse_close_20260716"))
        tsmc = smd.fetch_twse_close(D)["2330"]
        assert tsmc["o"] == 2430.0 and tsmc["h"] == 2470.0 and tsmc["l"] == 2420.0 and tsmc["c"] == 2470.0
        assert tsmc["v"] == 30538604

    def test_exright_flag_x(self, serve):
        serve(load("twse_close_20260716"))
        res = smd.fetch_twse_close(D)
        assert res["1102"]["status"] == "exright"
        assert res["1102"]["c"] == 33.0
        assert res["2382"]["status"] == "exright"

    def test_no_close(self, serve):
        serve(load("twse_close_20260716"))
        row = smd.fetch_twse_close(D)["1213"]
        assert row["c"] is None and row["status"] == "no_close"
        assert row["o"] is None and row["h"] is None and row["l"] is None
        assert row["v"] == 1  # real quirk: 1 share traded but no price

    def test_whitespace_in_cells(self, serve):
        p = load("twse_close_20260716")
        for r in p["tables"][1]["data"]:
            if r[0] == "2330":
                r[8] = "  2,470.00 "
                r[2] = " 30,538,604 "
        serve(p)
        assert smd.fetch_twse_close(D)["2330"]["c"] == 2470.0

    def test_date_mismatch_returns_none(self, serve):
        p = load("twse_close_20260716")
        p["date"] = "20260715"
        serve(p)
        assert smd.fetch_twse_close(D) is None

    def test_non_trading_day_returns_none(self, serve):
        serve(load("twse_close_nodata"))
        assert smd.fetch_twse_close(datetime.date(2026, 7, 18)) is None

    def test_too_few_codes_with_real_threshold(self, serve, monkeypatch):
        monkeypatch.setattr(smd, "TWSE_MIN_CODES", 900)
        serve(load("twse_close_20260716"))  # only 38 four-digit codes
        assert smd.fetch_twse_close(D) is None

    def test_threshold_boundary(self, serve, monkeypatch):
        serve(load("twse_close_20260716"))
        monkeypatch.setattr(smd, "TWSE_MIN_CODES", 38)
        assert smd.fetch_twse_close(D) is not None
        monkeypatch.setattr(smd, "TWSE_MIN_CODES", 39)
        assert smd.fetch_twse_close(D) is None

    def test_short_row_returns_none(self, serve):
        p = load("twse_close_20260716")
        p["tables"][1]["data"][5] = p["tables"][1]["data"][5][:9]  # drops the change column
        serve(p)
        assert smd.fetch_twse_close(D) is None

    def test_too_few_columns_in_header_returns_none(self, serve):
        p = load("twse_close_20260716")
        p["tables"][1]["fields"] = p["tables"][1]["fields"][:9]
        serve(p)
        assert smd.fetch_twse_close(D) is None

    def test_reordered_columns_returns_none(self, serve):
        p = load("twse_close_20260716")
        f = p["tables"][1]["fields"]
        f[8], f[5] = f[5], f[8]
        serve(p)
        assert smd.fetch_twse_close(D) is None

    def test_garbage_price_returns_none(self, serve):
        p = load("twse_close_20260716")
        p["tables"][1]["data"][5][8] = "N/A"
        serve(p)
        assert smd.fetch_twse_close(D) is None

    def test_duplicate_code_returns_none(self, serve):
        p = load("twse_close_20260716")
        p["tables"][1]["data"].append(copy.deepcopy(p["tables"][1]["data"][4]))
        serve(p)
        assert smd.fetch_twse_close(D) is None

    def test_stock_table_missing_returns_none(self, serve):
        p = load("twse_close_20260716")
        p["tables"] = p["tables"][:1]  # index decoy table only
        serve(p)
        assert smd.fetch_twse_close(D) is None

    def test_http_error_returns_none(self, serve):
        serve(FakeResp({}, status=503))
        assert smd.fetch_twse_close(D) is None

    def test_json_error_returns_none(self, serve):
        serve(FakeResp(text="<html>blocked</html>"))
        assert smd.fetch_twse_close(D) is None

    def test_network_exception_returns_none(self, serve):
        import requests
        serve(requests.ConnectionError("boom"))
        assert smd.fetch_twse_close(D) is None

    def test_failure_is_logged(self, serve, caplog):
        serve(FakeResp({}, status=500))
        with caplog.at_level("WARNING", logger="shadow_market_data"):
            smd.fetch_twse_close(D)
        assert any("HTTP 500" in r.message for r in caplog.records)

    def test_utf8_encoding_set(self, monkeypatch):
        seen = {}

        def fake_get(url, **kw):
            r = FakeResp(load("twse_close_20260716"))
            seen["resp"] = r
            return r
        monkeypatch.setattr(smd.requests, "get", fake_get)
        smd.fetch_twse_close(D)
        assert seen["resp"].encoding == "utf-8"


# ------------------------------------------------------------------ TPEx close

class TestTpexClose:
    def test_normal_parse(self, serve):
        calls = serve(load("tpex_close_20260716"))
        res = smd.fetch_tpex_close(D)
        assert res is not None
        assert "00679B" not in res
        assert all(len(c) == 4 and c.isdigit() for c in res)
        row = res["1240"]
        assert row == {"o": 55.4, "h": 55.5, "l": 55.3, "c": 55.5, "v": 10000, "status": "ok"}
        url, kw = calls[0]
        assert "date=2026%2F07%2F16" in url and "type=EW" in url
        assert "stk_quote_result" not in url
        assert kw["timeout"] == 40 and kw["headers"]["User-Agent"] == "Mozilla/5.0"

    def test_thousand_separators(self, serve):
        serve(load("tpex_close_20260716"))
        row = smd.fetch_tpex_close(D)["6488"]
        assert row["c"] == 1370.0 and row["o"] == 1420.0 and row["h"] == 1460.0 and row["l"] == 1355.0
        assert row["v"] == 6639000

    def test_exright_flags(self, serve):
        serve(load("tpex_close_20260716"))
        res = smd.fetch_tpex_close(D)
        assert res["1777"]["status"] == "exright" and res["1777"]["c"] == 72.0  # 除息
        assert res["2755"]["status"] == "exright"  # 除權息
        assert res["6488"]["status"] == "exright"

    def test_no_close(self, serve):
        serve(load("tpex_close_20260716"))
        row = smd.fetch_tpex_close(D)["2752"]
        assert row["c"] is None and row["status"] == "no_close" and row["v"] == 0
        assert row["o"] is None and row["h"] is None and row["l"] is None

    def test_date_mismatch_returns_none(self, serve):
        p = load("tpex_close_20260716")
        p["date"] = "20260715"
        serve(p)
        assert smd.fetch_tpex_close(D) is None

    def test_table_date_mismatch_returns_none(self, serve):
        p = load("tpex_close_20260716")
        p["tables"][0]["date"] = "115/07/15"
        serve(p)
        assert smd.fetch_tpex_close(D) is None

    def test_empty_day_returns_none(self, serve):
        serve(load("tpex_close_nodata"))
        assert smd.fetch_tpex_close(datetime.date(2026, 7, 18)) is None

    def test_too_few_codes_with_real_threshold(self, serve, monkeypatch):
        monkeypatch.setattr(smd, "TPEX_MIN_CODES", 700)
        serve(load("tpex_close_20260716"))
        assert smd.fetch_tpex_close(D) is None

    def test_short_row_returns_none(self, serve):
        p = load("tpex_close_20260716")
        p["tables"][0]["data"][3] = p["tables"][0]["data"][3][:6]
        serve(p)
        assert smd.fetch_tpex_close(D) is None

    def test_header_with_too_few_columns_returns_none(self, serve):
        p = load("tpex_close_20260716")
        p["tables"][0]["fields"] = p["tables"][0]["fields"][:5]
        serve(p)
        assert smd.fetch_tpex_close(D) is None

    def test_http_error_returns_none(self, serve):
        serve(FakeResp({}, status=404))
        assert smd.fetch_tpex_close(D) is None

    def test_json_error_returns_none(self, serve):
        serve(FakeResp(text="not json"))
        assert smd.fetch_tpex_close(D) is None

    def test_garbage_volume_returns_none(self, serve):
        p = load("tpex_close_20260716")
        p["tables"][0]["data"][3][7] = "abc"
        serve(p)
        assert smd.fetch_tpex_close(D) is None


# ------------------------------------------------------------------ ex-rights tables

class TestExright:
    def test_twse_parse(self, serve):
        calls = serve(load("twse_exright_20260716"))
        res = smd.fetch_twse_exright(D, D)
        assert res[(D, "1102")] == {"pre_close": 34.8, "ref_price": 32.5}
        assert res[(D, "2404")] == {"pre_close": 1285.0, "ref_price": 1245.0}  # thousand separators
        assert (D, "006208") not in res and all(len(c) == 4 for _, c in res)
        assert "startDate=20260716" in calls[0][0] and "endDate=20260716" in calls[0][0]

    def test_tpex_parse(self, serve):
        calls = serve(load("tpex_exright_20260716"))
        res = smd.fetch_tpex_exright(D, D)
        assert res[(D, "1777")] == {"pre_close": 75.3, "ref_price": 71.3}
        assert res[(D, "6488")] == {"pre_close": 1480.0, "ref_price": 1474.3}
        assert (D, "00694B") not in res
        assert "startDate=2026%2F07%2F16" in calls[0][0] and "endDate=2026%2F07%2F16" in calls[0][0]

    def test_twse_date_outside_range_returns_none(self, serve):
        serve(load("twse_exright_20260716"))
        assert smd.fetch_twse_exright(datetime.date(2026, 7, 17), datetime.date(2026, 7, 20)) is None

    def test_tpex_date_outside_range_returns_none(self, serve):
        # the endpoint silently answers "latest day" when it ignores the range
        serve(load("tpex_exright_20260716"))
        assert smd.fetch_tpex_exright(datetime.date(2026, 7, 17), datetime.date(2026, 7, 20)) is None

    def test_tpex_echoed_range_mismatch_returns_none(self, serve):
        p = load("tpex_exright_20260716")
        p["date"] = "20260101~20260101"
        serve(p)
        assert smd.fetch_tpex_exright(D, D) is None

    def test_range_covering_row_is_ok(self, serve):
        p = load("tpex_exright_20260716")
        p["date"] = "20260713~20260717"
        serve(p)
        res = smd.fetch_tpex_exright(datetime.date(2026, 7, 13), datetime.date(2026, 7, 17))
        assert (D, "1777") in res

    def test_empty_range_is_empty_dict(self, serve):
        s = datetime.date(2026, 7, 18)
        serve(load("twse_exright_nodata"))
        assert smd.fetch_twse_exright(s, s) == {}
        serve(load("tpex_exright_nodata"))
        assert smd.fetch_tpex_exright(s, s) == {}

    def test_http_error_returns_none(self, serve):
        serve(FakeResp({}, status=500))
        assert smd.fetch_twse_exright(D, D) is None
        assert smd.fetch_tpex_exright(D, D) is None

    def test_unknown_stat_returns_none(self, serve):
        serve({"stat": "something else"})
        assert smd.fetch_twse_exright(D, D) is None

    def test_short_row_returns_none(self, serve):
        p = load("twse_exright_20260716")
        p["data"][0] = p["data"][0][:4]
        serve(p)
        assert smd.fetch_twse_exright(D, D) is None

    def test_bad_price_row_is_skipped_not_fatal(self, serve):
        p = load("tpex_exright_20260716")
        for r in p["tables"][0]["data"]:
            if r[1] == "1777":
                r[4] = "--"
        serve(p)
        res = smd.fetch_tpex_exright(D, D)
        assert (D, "1777") not in res and (D, "1785") in res

    def test_start_after_end_returns_none(self, serve):
        serve(load("twse_exright_20260716"))
        assert smd.fetch_twse_exright(D, D - datetime.timedelta(days=1)) is None


# ------------------------------------------------------------------ adj_factor

def make_tables(twse_ex=None, tpex_ex=None, twse_close=None, tpex_close=None):
    return {"twse": twse_ex, "tpex": tpex_ex, "twse_close": twse_close, "tpex_close": tpex_close}


def real_tables(serve):
    serve(load("twse_close_20260716"))
    twse_close = smd.fetch_twse_close(D)
    serve(load("tpex_close_20260716"))
    tpex_close = smd.fetch_tpex_close(D)
    serve(load("twse_exright_20260716"))
    twse_ex = smd.fetch_twse_exright(D, D)
    serve(load("tpex_exright_20260716"))
    tpex_ex = smd.fetch_tpex_exright(D, D)
    return make_tables(twse_ex, tpex_ex, twse_close, tpex_close)


class TestAdjFactor:
    def test_source_twse_from_table(self, serve):
        t = real_tables(serve)
        f, src = smd.adj_factor(D, "1102", t)
        assert src == "twse" and f == pytest.approx(32.5 / 34.8)

    def test_source_tpex_from_table(self, serve):
        t = real_tables(serve)
        f, src = smd.adj_factor(D, "1777", t)
        assert src == "tpex" and f == pytest.approx(71.3 / 75.3)

    def test_normal_stock_is_one_with_market_source(self, serve):
        t = real_tables(serve)
        assert smd.adj_factor(D, "2330", t) == (1.0, "twse")
        assert smd.adj_factor(D, "1240", t) == (1.0, "tpex")

    def test_fallback_used_when_table_lacks_exright_code(self, serve):
        t = real_tables(serve)
        del t["tpex"][(D, "1777")]  # table lost the row, but the quote still says 除息
        got = []

        def fb(d, c):
            got.append((d, c))
            return 0.947
        assert smd.adj_factor(D, "1777", t, fallback=fb) == (0.947, "yahoo")
        assert got == [(D, "1777")]

    def test_fallback_used_when_whole_table_failed(self, serve):
        t = real_tables(serve)
        t["tpex"] = None  # fetch_tpex_exright failed
        assert smd.adj_factor(D, "1777", t, fallback=lambda d, c: 0.95) == (0.95, "yahoo")
        # ... but a normal stock does not trigger the fallback
        assert smd.adj_factor(D, "1240", t, fallback=lambda d, c: pytest.fail("must not be called")) == (1.0, "tpex")

    def test_fallback_not_called_when_table_has_code(self, serve):
        t = real_tables(serve)
        f, src = smd.adj_factor(D, "1102", t, fallback=lambda d, c: pytest.fail("must not be called"))
        assert src == "twse"

    def test_adj_missing_when_fallback_returns_none(self, serve):
        t = real_tables(serve)
        t["tpex"] = None
        assert smd.adj_factor(D, "1777", t, fallback=lambda d, c: None) == (None, "adj_missing")

    def test_adj_missing_when_fallback_raises(self, serve):
        t = real_tables(serve)
        t["tpex"] = None

        def boom(d, c):
            raise RuntimeError("yahoo down")
        assert smd.adj_factor(D, "1777", t, fallback=boom) == (None, "adj_missing")

    def test_adj_missing_when_fallback_returns_garbage(self, serve):
        t = real_tables(serve)
        t["tpex"] = None
        assert smd.adj_factor(D, "1777", t, fallback=lambda d, c: float("nan")) == (None, "adj_missing")
        assert smd.adj_factor(D, "1777", t, fallback=lambda d, c: -1.0) == (None, "adj_missing")

    def test_adj_missing_when_code_unknown(self, serve):
        t = real_tables(serve)
        assert smd.adj_factor(D, "9999", t) == (None, "adj_missing")

    def test_adj_missing_when_quotes_failed_and_no_table_entry(self):
        assert smd.adj_factor(D, "2330", make_tables()) == (None, "adj_missing")

    def test_table_entry_works_without_quotes(self):
        tab = make_tables(twse_ex={(D, "1102"): {"pre_close": 40.0, "ref_price": 38.0}})
        assert smd.adj_factor(D, "1102", tab) == (pytest.approx(0.95), "twse")

    def test_default_fallback_is_yahoo(self, serve, monkeypatch):
        t = real_tables(serve)
        t["tpex"] = None
        seen = []
        monkeypatch.setattr(smd, "yahoo_adj_ratio", lambda d, c, m: seen.append((d, c, m)) or 0.9469)
        assert smd.adj_factor(D, "1777", t) == (0.9469, "yahoo")
        assert seen == [(D, "1777", "tpex")]
        seen.clear()
        t["twse"] = None
        smd.adj_factor(D, "1102", t)
        assert seen == [(D, "1102", "twse")]

    def test_no_close_exright_not_applicable(self):
        tab = make_tables(twse_close={"1213": {"c": None, "status": "no_close"}}, twse_ex={})
        assert smd.adj_factor(D, "1213", tab) == (1.0, "twse")


# ------------------------------------------------------------------ yahoo_adj_ratio

class TestYahooAdjRatio:
    def test_ratio_from_real_chart(self, serve):
        calls = serve(load("yahoo_chart_1777_two"))
        r = smd.yahoo_adj_ratio(D, "1777", "tpex")
        assert r == pytest.approx(71.3 / 75.3, rel=1e-5)
        assert "1777.TWO" in calls[0][0] and "period1=" in calls[0][0]

    def test_twse_suffix(self, serve):
        calls = serve(load("yahoo_chart_1777_two"))
        smd.yahoo_adj_ratio(D, "1102", "twse")
        assert "1102.TW?" in calls[0][0]

    def test_non_ex_day_is_one(self, serve):
        serve(load("yahoo_chart_1777_two"))
        assert smd.yahoo_adj_ratio(datetime.date(2026, 7, 17), "1777", "tpex") == pytest.approx(1.0, abs=1e-6)

    def test_missing_bar_returns_none(self, serve):
        serve(load("yahoo_chart_1777_two"))
        assert smd.yahoo_adj_ratio(datetime.date(2026, 7, 18), "1777", "tpex") is None

    def test_http_failure_returns_none(self, serve):
        serve(FakeResp({}, status=404))
        assert smd.yahoo_adj_ratio(D, "1777", "tpex") is None

    def test_unknown_market_returns_none(self):
        assert smd.yahoo_adj_ratio(D, "1777", "nyse") is None

    def test_implausible_factor_returns_none(self, serve):
        p = load("yahoo_chart_1777_two")
        p["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"][6] = 150.0  # previous bar: factor would be ~2
        serve(p)
        assert smd.yahoo_adj_ratio(D, "1777", "tpex") is None


# ------------------------------------------------------------------ trading calendar

def d(y, m, day):
    return datetime.date(y, m, day)


class TestCalendar:
    def test_2026_known_holidays(self, serve):
        serve(load("holidays_2026"))
        cal = smd.fetch_trading_calendar(2026)
        assert isinstance(cal, set) and all(isinstance(x, datetime.date) for x in cal)
        # Lunar New Year block 2026-02-12 .. 02-20 is closed, 02-11 and 02-23 are open
        for day in (12, 13, 16, 17, 18, 19, 20):
            assert d(2026, 2, day) not in cal
        assert d(2026, 2, 11) in cal and d(2026, 2, 23) in cal
        # make-up holidays
        assert d(2026, 10, 9) not in cal      # 國慶日補假
        assert d(2026, 10, 26) not in cal     # 光復節補假
        assert d(2026, 2, 27) not in cal      # 和平紀念日補假
        assert d(2026, 4, 3) not in cal and d(2026, 4, 6) not in cal
        assert d(2026, 1, 1) not in cal and d(2026, 12, 25) not in cal

    def test_opening_day_rows_are_trading_days(self, serve):
        serve(load("holidays_2026"))
        cal = smd.fetch_trading_calendar(2026)
        assert d(2026, 1, 2) in cal  # listed as 國曆新年開始交易日
        assert d(2026, 2, 23) in cal  # listed as 農曆春節後開始交易日

    def test_weekends_never_trading(self, serve):
        serve(load("holidays_2026"))
        cal = smd.fetch_trading_calendar(2026)
        assert all(x.weekday() < 5 and x.year == 2026 for x in cal)

    def test_2026_day_count(self, serve):
        serve(load("holidays_2026"))
        # 261 weekdays minus 18 weekday closures (hand-counted from the table)
        assert len(smd.fetch_trading_calendar(2026)) == 243

    def test_ordinary_day_is_trading(self, serve):
        serve(load("holidays_2026"))
        cal = smd.fetch_trading_calendar(2026)
        assert d(2026, 7, 16) in cal and d(2026, 11, 30) in cal and d(2026, 12, 1) in cal

    def test_2027_year_end_and_new_year(self, serve):
        serve(load("holidays_2027"))
        cal = smd.fetch_trading_calendar(2027)
        assert d(2027, 1, 4) in cal and d(2027, 1, 1) not in cal
        assert d(2027, 12, 31) not in cal  # 117年開國紀念日補假 falls on 2027-12-31
        assert d(2027, 2, 1) in cal and d(2027, 2, 2) not in cal and d(2027, 2, 11) in cal

    def test_unpublished_year_returns_none(self, serve):
        serve(load("holidays_2028_unpublished"))
        assert smd.fetch_trading_calendar(2028) is None

    def test_query_year_mismatch_returns_none(self, serve):
        serve(load("holidays_2026"))
        assert smd.fetch_trading_calendar(2027) is None

    def test_http_failure_returns_none(self, serve):
        serve(FakeResp({}, status=500))
        assert smd.fetch_trading_calendar(2026) is None

    def test_json_failure_returns_none(self, serve):
        serve(FakeResp(text="oops"))
        assert smd.fetch_trading_calendar(2026) is None

    def test_stat_not_ok_returns_none(self, serve):
        p = load("holidays_2026")
        p["stat"] = "error"
        serve(p)
        assert smd.fetch_trading_calendar(2026) is None

    def test_implausibly_short_table_returns_none(self, serve):
        p = load("holidays_2026")
        p["data"] = p["data"][:2]
        serve(p)
        assert smd.fetch_trading_calendar(2026) is None

    def test_bad_date_returns_none(self, serve):
        p = load("holidays_2026")
        p["data"][3][0] = "02/12/2026"
        serve(p)
        assert smd.fetch_trading_calendar(2026) is None

    def test_url_uses_year(self, serve):
        calls = serve(load("holidays_2026"))
        smd.fetch_trading_calendar(2026)
        assert "holidaySchedule" in calls[0][0] and "date=2026" in calls[0][0]


# ------------------------------------------------------------------ throttle

class TestThrottle:
    def test_sleeps_between_requests(self, serve, monkeypatch):
        slept = []
        clock = [100.0]
        monkeypatch.setattr(smd, "REQUEST_INTERVAL", 3.0)
        monkeypatch.setattr(smd.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(smd.time, "sleep", lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s)))
        serve(load("twse_close_nodata"))
        smd.fetch_twse_close(D)           # first request: no wait
        assert slept == []
        smd.fetch_twse_close(D)           # immediately again: waits the full interval
        assert slept == [pytest.approx(3.0)]
        clock[0] += 5.0                   # already 5 s later: no extra wait
        smd.fetch_twse_close(D)
        assert len(slept) == 1

    def test_interval_zero_never_sleeps(self, serve, monkeypatch):
        monkeypatch.setattr(smd.time, "sleep", lambda s: pytest.fail("must not sleep"))
        serve(load("twse_close_nodata"))
        smd.fetch_twse_close(D)
        smd.fetch_twse_close(D)

    def test_default_constants(self):
        assert DEFAULT_INTERVAL == 3.0 and DEFAULT_TIMEOUT == 40
