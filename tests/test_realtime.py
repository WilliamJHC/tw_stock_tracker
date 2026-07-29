# -*- coding: utf-8 -*-
"""Unit tests for realtime/. No real network calls: requests.get and
_yf_history are patched for MISSource; BinanceSource is tested at the
message-parsing / backoff level (no live socket, no pytest-asyncio dep)."""
import datetime as dt


from unittest.mock import patch

import pytest

import data_sources as ds
from realtime import KGISource
from realtime.binance_source import BinanceSource
from realtime.mis_source import MISSource


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "CACHE_DIR", str(tmp_path))


# ---------------------------------------------------------- MISSource --

def _fake_json_response(payload):
    class FakeResponse:
        def json(self):
            return payload
    return FakeResponse()


def _mis_item(**overrides):
    item = {
        "c": "2330",
        "n": "台積電",
        "z": "1000.0",
        "y": "990.0",
        "v": "12345",
        "b": "999.0_998.0_997.0_996.0_995.0_",
        "g": "10_20_30_40_50_",
        "a": "1000.0_1001.0_1002.0_1003.0_1004.0_",
        "f": "5_15_25_35_45_",
    }
    item.update(overrides)
    return item


def test_mis_poll_once_parses_quote_and_depth():
    body = {"msgArray": [_mis_item()]}
    quotes_seen, depths_seen = [], []
    src = MISSource(now_fn=lambda: dt.datetime(2026, 7, 20, 10, 0, 0))
    src.subscribe(["2330"])
    src.on_quote(quotes_seen.append)
    src.on_depth(depths_seen.append)

    with patch("realtime.mis_source.requests.get", return_value=_fake_json_response(body)):
        quotes = src.poll_once()

    assert len(quotes) == 1
    q = quotes[0]
    assert q.price == 1000.0
    assert q.prev_close == 990.0
    assert q.change == pytest.approx(10.0)
    assert q.change_pct == pytest.approx(10.0 / 990.0 * 100)
    assert q.vol_lots == 12345
    assert q.name == "台積電"
    assert q.source == "twse-mis"
    assert q.is_pretrade is False
    # bid: high->low, 5 levels
    assert q.bid_prices == [999.0, 998.0, 997.0, 996.0, 995.0]
    assert q.bid_vols == [10, 20, 30, 40, 50]
    # ask: low->high, 5 levels
    assert q.ask_prices == [1000.0, 1001.0, 1002.0, 1003.0, 1004.0]
    assert q.ask_vols == [5, 15, 25, 35, 45]

    assert quotes_seen == quotes
    assert len(depths_seen) == 1
    assert depths_seen[0].bid_prices == q.bid_prices
    assert depths_seen[0].ask_prices == q.ask_prices


def test_mis_poll_once_uses_pz_during_pretrade_and_flags_it():
    body = {"msgArray": [_mis_item(z="-", pz="998.5")]}
    src = MISSource(now_fn=lambda: dt.datetime(2026, 7, 20, 8, 45, 0))
    src.subscribe(["2330"])

    with patch("realtime.mis_source.requests.get", return_value=_fake_json_response(body)):
        quotes = src.poll_once()

    assert quotes[0].price == 998.5
    assert quotes[0].is_pretrade is True


def test_mis_poll_once_not_pretrade_before_0830_or_after_0900():
    body = {"msgArray": [_mis_item()]}
    for hour, minute in [(8, 0), (9, 0), (13, 30)]:
        src = MISSource(now_fn=lambda h=hour, m=minute: dt.datetime(2026, 7, 20, h, m, 0))
        src.subscribe(["2330"])
        with patch("realtime.mis_source.requests.get", return_value=_fake_json_response(body)):
            quotes = src.poll_once()
        assert quotes[0].is_pretrade is False


def test_mis_poll_once_degrades_to_yfinance_on_request_exception():
    import requests as real_requests

    class FakeSeries(list):
        def dropna(self):
            return [x for x in self if x is not None]

        def fillna(self, v):
            return [v if x is None else x for x in self]

    class FakeDF(dict):
        def __getitem__(self, key):
            return FakeSeries(dict.__getitem__(self, key))

    fake_df = FakeDF({"Close": [980.0, 985.0], "Volume": [1000, 2000]})

    src = MISSource(now_fn=lambda: dt.datetime(2026, 7, 20, 10, 0, 0))
    src.subscribe(["2330"])

    with patch("realtime.mis_source.requests.get", side_effect=real_requests.RequestException("boom")), \
         patch("realtime.mis_source._yf_history", return_value=fake_df):
        quotes = src.poll_once()

    assert len(quotes) == 1
    assert quotes[0].source == "yfinance-degraded"
    assert quotes[0].price == 985.0
    assert quotes[0].prev_close == 980.0


def test_mis_poll_once_no_codes_returns_empty_without_request():
    src = MISSource()
    with patch("realtime.mis_source.requests.get", side_effect=AssertionError("must not be called")):
        assert src.poll_once() == []


def test_mis_on_trade_raises_not_implemented():
    src = MISSource()
    with pytest.raises(NotImplementedError):
        src.on_trade(lambda t: None)


# ------------------------------------------------------- BinanceSource --

def test_binance_parse_agg_trade_taker_sell_when_buyer_is_maker():
    src = BinanceSource()
    data = {"p": "65000.10", "q": "0.5", "m": True, "T": 1700000000000}
    trade = src._parse_agg_trade("btcusdt", data)
    assert trade.side == "sell"
    assert trade.price == 65000.10
    assert trade.qty == 0.5
    assert trade.code == "BTCUSDT"
    assert trade.ts == 1700000000.0


def test_binance_parse_agg_trade_taker_buy_when_buyer_not_maker():
    src = BinanceSource()
    data = {"p": "65001.0", "q": "1.2", "m": False, "T": 1700000001000}
    trade = src._parse_agg_trade("btcusdt", data)
    assert trade.side == "buy"


def test_binance_parse_depth():
    src = BinanceSource()
    data = {
        "bids": [["64999.0", "1.0"], ["64998.0", "2.0"]],
        "asks": [["65001.0", "0.5"], ["65002.0", "0.7"]],
    }
    depth = src._parse_depth("btcusdt", data)
    assert depth.code == "BTCUSDT"
    assert depth.bid_prices == [64999.0, 64998.0]
    assert depth.ask_prices == [65001.0, 65002.0]
    assert depth.bid_vols == [1.0, 2.0]


def test_binance_handle_frame_dispatches_by_stream_suffix():
    src = BinanceSource()
    trades, depths = [], []
    src.on_trade(trades.append)
    src.on_depth(depths.append)

    src._handle_frame({"stream": "btcusdt@aggTrade", "data": {"p": "1.0", "q": "1.0", "m": False, "T": 1}})
    src._handle_frame({"stream": "btcusdt@depth20@1000ms", "data": {"bids": [], "asks": []}})

    assert len(trades) == 1
    assert len(depths) == 1


def test_binance_build_url_joins_multiple_streams():
    src = BinanceSource()
    src.subscribe(["BTCUSDT", "ETHUSDT"])
    url = src._build_url()
    assert "btcusdt@aggTrade" in url
    assert "ethusdt@aggTrade" in url
    assert "btcusdt@depth20@1000ms" in url
    assert url.startswith("wss://stream.binance.com:9443/stream?streams=")


def test_binance_backoff_doubles_and_caps():
    src = BinanceSource(initial_backoff=1.0, max_backoff=30.0)
    seq = [src.initial_backoff]
    b = src.initial_backoff
    for _ in range(8):
        b = src.next_backoff(b)
        seq.append(b)
    assert seq == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]


def test_binance_on_quote_raises_not_implemented():
    src = BinanceSource()
    with pytest.raises(NotImplementedError):
        src.on_quote(lambda q: None)


# ----------------------------------------------------------- KGISource --

def test_kgi_source_all_methods_raise_not_implemented():
    src = KGISource()
    with pytest.raises(NotImplementedError):
        src.subscribe(["2330"])
    with pytest.raises(NotImplementedError):
        src.on_quote(lambda q: None)
    with pytest.raises(NotImplementedError):
        src.on_trade(lambda t: None)
    with pytest.raises(NotImplementedError):
        src.on_depth(lambda d: None)

    import asyncio

    async def _run():
        await src.run()

    with pytest.raises(NotImplementedError):
        asyncio.run(_run())
