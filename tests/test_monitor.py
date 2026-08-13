# -*- coding: utf-8 -*-
"""Unit + replay tests for monitor.py. No real network calls and no real
sleep/wall-clock waiting: `now` is always injected, `source` is always a
fake/mocked object, and send_telegram is always mocked (following the mock
style in tests/test_realtime.py and tests/test_data_sources.py)."""
import asyncio
import datetime as dt
import json
import os


import pytest

import data_sources as ds
import monitor
from realtime.models import Depth, Quote, Trade


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "CACHE_DIR", str(tmp_path))


# -------------------------------------------------------------- schedule --

@pytest.mark.parametrize("hour,minute,expected", [
    (8, 29, False),
    (8, 30, True),
    (13, 30, True),
    (13, 31, False),
    (10, 0, True),
])
def test_is_market_open_tw_boundaries(hour, minute, expected):
    now = dt.datetime(2026, 7, 20, hour, minute, 0)
    assert monitor.is_market_open("tw", now) is expected


def test_is_market_open_crypto_always_true():
    assert monitor.is_market_open("crypto", dt.datetime(2026, 7, 20, 3, 0, 0)) is True


def test_is_market_open_unknown_market_raises():
    with pytest.raises(ValueError):
        monitor.is_market_open("nasdaq")


# -------------------------------------------------------------- recording --

def test_record_quote_accumulates_ticks_and_skips_pretrade_session_open():
    state = monitor.new_state()
    pretrade = Quote(code="2330", price=100.0, prev_close=99.0, change=1.0, change_pct=1.0,
                      vol_lots=10, ts=1000.0, is_pretrade=True)
    live = Quote(code="2330", price=101.0, prev_close=99.0, change=2.0, change_pct=2.0,
                 vol_lots=20, ts=2000.0, is_pretrade=False)
    monitor.record_quote(state, pretrade)
    monitor.record_quote(state, live)
    hist = state["history"]["2330"]
    assert len(hist["ticks"]) == 2
    assert hist["session_open"] == monitor._ts_to_dt(2000.0)


def test_record_quote_tracks_prev_depth():
    state = monitor.new_state()
    q1 = Quote(code="2330", price=100.0, prev_close=99.0, change=1.0, change_pct=1.0, vol_lots=10,
               bid_prices=[99.0], bid_vols=[5], ask_prices=[101.0], ask_vols=[5], ts=1000.0)
    q2 = Quote(code="2330", price=100.5, prev_close=99.0, change=1.5, change_pct=1.5, vol_lots=15,
               bid_prices=[99.5], bid_vols=[50], ask_prices=[101.0], ask_vols=[5], ts=1010.0)
    monitor.record_quote(state, q1)
    monitor.record_quote(state, q2)
    hist = state["history"]["2330"]
    assert hist["depth"]["bid_vols"] == [50]
    assert hist["prev_depth"]["bid_vols"] == [5]


def test_record_trade_and_record_depth():
    state = monitor.new_state()
    trade = Trade(code="BTCUSDT", price=65000.0, qty=0.1, side="buy", ts=1000.0)
    monitor.record_trade(state, trade)
    depth = Depth(code="BTCUSDT", bid_prices=[64999.0], bid_vols=[1.0], ask_prices=[65001.0], ask_vols=[1.0], ts=1000.0)
    monitor.record_depth(state, depth)
    hist = state["history"]["BTCUSDT"]
    assert hist["trade_ticks"] == [{"price": 65000.0, "qty": 0.1, "ts": 1000.0}]
    assert hist["sides"] == ["buy"]
    assert hist["depth"]["bid_prices"] == [64999.0]


# ------------------------------------------------------- _compute_indicators --

def test_cumulative_to_incremental_diffs_and_clamps_negative():
    assert monitor._cumulative_to_incremental([10, 20, 20, 15, 25]) == [10, 10, 0, 0, 10]


def test_compute_indicators_tw_vwap_uses_incremental_volume_not_cumulative():
    """Regression: vwap_bands()/realtime_cvd() must weight each tick by how
    much volume traded AT that tick, not by MIS's running cumulative total
    (vol_lots). Two ticks with the SAME cumulative reading (no new trade
    between polls) must contribute zero weight on the second tick -- so
    VWAP stays anchored to the first tick's price, not pulled toward a
    50/50 blend the way summing raw cumulative values would."""
    state = monitor.new_state()
    monitor.record_quote(state, Quote(code="2330", price=100.0, prev_close=99.0, change=1.0,
                                       change_pct=1.0, vol_lots=100, ts=1000.0))
    monitor.record_quote(state, Quote(code="2330", price=200.0, prev_close=99.0, change=1.0,
                                       change_pct=1.0, vol_lots=100, ts=1001.0))  # cumulative unchanged -> 0 new volume
    indicators = monitor._compute_indicators(state["history"]["2330"])
    assert indicators["vwap"]["vwap"] == pytest.approx(100.0)  # not 150.0 (the buggy 50/50 blend)


def test_compute_indicators_tw_cum_vol_is_last_tick_vol_not_summed():
    """MIS's vol_lots is already a session-cumulative running total (see
    realtime/mis_source.py), so cum_vol must be the LAST tick's vol, not a
    sum across ticks -- summing would double-count every earlier reading."""
    state = monitor.new_state()
    for i, v in enumerate([100, 250, 400]):  # each reading is the running total so far
        monitor.record_quote(state, Quote(code="2330", price=100.0 + i, prev_close=99.0,
                                           change=1.0, change_pct=1.0, vol_lots=v, ts=1000.0 + i))
    indicators = monitor._compute_indicators(state["history"]["2330"])
    assert indicators["cum_vol"] == 400


def test_compute_indicators_crypto_cum_vol_is_sum_of_trade_qty():
    """Binance aggTrade qty is per-trade (not cumulative), so cum_vol here
    must sum every trade recorded so far."""
    state = monitor.new_state()
    for qty in (0.1, 0.2, 0.05):
        monitor.record_trade(state, Trade(code="BTCUSDT", price=65000.0, qty=qty, side="buy", ts=1000.0))
    indicators = monitor._compute_indicators(state["history"]["BTCUSDT"])
    assert indicators["cum_vol"] == pytest.approx(0.35)


def test_compute_indicators_cum_vol_none_when_no_data_yet():
    state = monitor.new_state()
    hist = monitor._ensure_history(state, "2330")
    assert monitor._compute_indicators(hist)["cum_vol"] is None


# -------------------------------------------------------------- run_once --

class _FakeSource:
    """Replays a fixed sequence of poll_once() results, one per call."""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = 0

    def poll_once(self):
        self.calls += 1
        if not self._rounds:
            return []
        return self._rounds.pop(0)


class _SendRecorder:
    def __init__(self, result=None):
        self.messages = []
        self._result = result if result is not None else {"skipped": True, "reason": "test"}

    def __call__(self, message):
        self.messages.append(message)
        return self._result


def _tw_quote(code, price, vol_lots, ts, is_pretrade=False, bid=None, ask=None):
    bid = bid or ([], [])
    ask = ask or ([], [])
    return Quote(code=code, price=price, prev_close=price - 1, change=1.0, change_pct=1.0,
                 vol_lots=vol_lots, bid_prices=bid[0], bid_vols=bid[1],
                 ask_prices=ask[0], ask_vols=ask[1], ts=ts, is_pretrade=is_pretrade)


def test_run_once_skips_codes_with_no_data_yet():
    state = monitor.new_state()
    source = _FakeSource([[]])
    sender = _SendRecorder()
    sent = monitor.run_once(["2330"], "tw", state, source=source, send_fn=sender,
                             now=dt.datetime(2026, 7, 20, 9, 5, 0))
    assert sent == []
    assert sender.messages == []


def test_run_once_source_failure_does_not_crash():
    state = monitor.new_state()

    class BoomSource:
        def poll_once(self):
            raise RuntimeError("network boom")

    sent = monitor.run_once(["2330"], "tw", state, source=BoomSource(), send_fn=_SendRecorder(),
                             now=dt.datetime(2026, 7, 20, 9, 5, 0))
    assert sent == []


def test_run_once_replay_triggers_vwap_and_orb_alerts_and_persists_cache():
    """Replay a burst of ticks that (a) establishes an opening range, then
    (b) breaks well above both the ORB high and VWAP+2sigma -- both rules
    should fire, get sent (mocked), and get persisted to the cache file."""
    now0 = dt.datetime(2026, 7, 20, 9, 0, 0)
    session_open_ts = now0.timestamp()

    # First round: quiet opening-range ticks close to 100, moderate volume
    # (10 lots per tick). vol_lots is MIS's session-cumulative running total
    # (see realtime/mis_source.py), so "10 lots per tick" over 4 ticks is
    # the increasing sequence 10/20/30/40 -- NOT the same value repeated.
    round1 = [
        _tw_quote("2330", 100.0, 10, session_open_ts + 60),
        _tw_quote("2330", 100.2, 20, session_open_ts + 120),
        _tw_quote("2330", 99.8, 30, session_open_ts + 180),
        _tw_quote("2330", 100.1, 40, session_open_ts + 200),
    ]
    # Second round: still inside the 15-min opening-range window but a
    # sharp spike far above the range high and the VWAP -- triggers both
    # orb_breakout_up and vwap_breakout_up. Volume is deliberately tiny (the
    # cumulative total ticks up by just 1 lot from round1's 40 -> 41) so it
    # doesn't drag the volume-weighted VWAP/bands up to meet it.
    round2 = [
        _tw_quote("2330", 108.0, 41, session_open_ts + 20 * 60),
    ]

    state = monitor.new_state()
    source = _FakeSource([round1, round2])
    sender = _SendRecorder(result={"skipped": True, "reason": "no telegram in test"})

    sent1 = monitor.run_once(["2330"], "tw", state, source=source, send_fn=sender, now=now0)
    sent2 = monitor.run_once(["2330"], "tw", state, source=source, send_fn=sender,
                              now=now0 + dt.timedelta(minutes=25))

    assert sent1 == []  # nothing anomalous yet
    event_types = {s["alert"]["event_type"] for s in sent2}
    assert "vwap_breakout_up" in event_types
    assert "orb_breakout_up" in event_types
    assert len(sender.messages) == len(sent2) >= 2

    # cache persistence: one record per run_once call that reached persist
    cache_path = monitor._cache_file("2330", now0.date().isoformat())
    assert os.path.exists(cache_path)
    with open(cache_path, encoding="utf-8") as f:
        blob = json.load(f)
    assert blob["code"] == "2330"
    assert blob["market"] == "tw"
    assert len(blob["records"]) == 2


def test_run_once_replay_depth_wall_imbalance():
    now0 = dt.datetime(2026, 7, 20, 9, 0, 0)
    ts0 = now0.timestamp()
    # Heavy bid wall vs thin ask -> bid_heavy imbalance with a wall.
    q = _tw_quote(
        "2330", 100.0, 10, ts0,
        bid=([99.9, 99.8, 99.7], [5000, 10, 10]),
        ask=([100.1, 100.2, 100.3], [10, 10, 10]),
    )
    state = monitor.new_state()
    source = _FakeSource([[q]])
    sender = _SendRecorder()
    sent = monitor.run_once(["2330"], "tw", state, source=source, send_fn=sender, now=now0)
    event_types = {s["alert"]["event_type"] for s in sent}
    assert "depth_wall_bid_heavy" in event_types


def test_run_once_dedupes_repeat_alert_within_cooldown():
    now0 = dt.datetime(2026, 7, 20, 9, 0, 0)
    ts0 = now0.timestamp()
    # vol_lots is MIS's session-cumulative running total, so "100 lots per
    # tick" over 5 warm-up ticks is 100/200/300/400/500, and the spike's
    # own tiny 1-lot contribution continues that total to 501 -- not the
    # same value repeated across every tick.
    spike = _tw_quote("2330", 108.0, 501, ts0)
    state = monitor.new_state()
    # Pre-seed history so vwap band is already tight around 100, then feed
    # the same breakout quote twice in a row.
    warm = [_tw_quote("2330", 100.0 + i * 0.01, 100 * (i + 1), ts0 - 100 + i) for i in range(5)]
    source = _FakeSource([warm, [spike], [spike]])
    sender = _SendRecorder()

    monitor.run_once(["2330"], "tw", state, source=source, send_fn=sender, now=now0)
    sent2 = monitor.run_once(["2330"], "tw", state, source=source, send_fn=sender, now=now0 + dt.timedelta(seconds=1))
    sent3 = monitor.run_once(["2330"], "tw", state, source=source, send_fn=sender, now=now0 + dt.timedelta(seconds=2))

    assert len(sent2) >= 1
    assert sent3 == []  # same event within default 5-minute cooldown -> suppressed


def test_run_once_crypto_source_none_evaluates_pre_populated_state():
    """Crypto/push-adapter path: state is fed via record_trade() directly
    (as a real BinanceSource on_trade callback would), and run_once() is
    called with source=None to just evaluate + alert on what's there."""
    state = monitor.new_state()
    now0 = dt.datetime(2026, 7, 20, 12, 0, 0)
    ts0 = now0.timestamp()
    for i in range(5):
        monitor.record_trade(state, Trade(code="BTCUSDT", price=65000.0 + i, qty=10.0, side="buy", ts=ts0 + i))
    # Sharp spike, still "buy" side (true CVD stays monotonic, so this
    # exercises the VWAP path without depending on CVD reversal). Tiny
    # qty so it doesn't drag the volume-weighted VWAP/bands up to meet it.
    monitor.record_trade(state, Trade(code="BTCUSDT", price=70000.0, qty=0.01, side="buy", ts=ts0 + 10))

    sender = _SendRecorder()
    sent = monitor.run_once(["BTCUSDT"], "crypto", state, source=None, send_fn=sender, now=now0)
    event_types = {s["alert"]["event_type"] for s in sent}
    assert "vwap_breakout_up" in event_types


# ------------------------------------------------------------------ main --

def test_main_respects_max_iterations_and_uses_injected_source():
    calls = {"n": 0}

    class CountingSource:
        def subscribe(self, codes):
            self.codes = codes

        def poll_once(self):
            calls["n"] += 1
            return []

    state = monitor.main(
        codes=["2330"], crypto_codes=[], max_iterations=2, sleep_seconds=0,
        mis_source=CountingSource(), send_fn=_SendRecorder(),
    )
    assert isinstance(state, dict)
    # is_market_open gates whether run_once (and thus poll_once) actually
    # runs; regardless of the real wall-clock time, main() must stop after
    # exactly 2 iterations without hanging.
    assert calls["n"] <= 2


class _FakeBinanceSource:
    """Stands in for realtime.binance_source.BinanceSource: same
    subscribe()/on_trade()/on_depth()/run() surface, but run() replays a
    fixed event list with no internal `await` between emits, so by the time
    it yields (its own idle `await asyncio.sleep(3600)`) every event has
    already reached the registered callbacks -- no real socket, no
    scheduling nondeterminism to guard against in the assertions below."""

    def __init__(self, events):
        self._events = list(events)
        self._trade_cbs = []
        self._depth_cbs = []
        self.subscribed = None
        self.saw_cancel = False

    def subscribe(self, codes):
        self.subscribed = list(codes)

    def on_trade(self, callback):
        self._trade_cbs.append(callback)

    def on_depth(self, callback):
        self._depth_cbs.append(callback)

    async def run(self):
        try:
            for event in self._events:
                for cb in self._trade_cbs:
                    cb(event)
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.saw_cancel = True
            raise


def test_main_wires_binance_task_feeds_state_and_alerts_on_crypto_codes():
    """crypto_codes non-empty -> main() must start the injected Binance-like
    source, register record_trade as its on_trade callback so pushed trades
    land in `state`, and evaluate/alert/persist on the crypto track via the
    existing source=None run_once() path -- without ever touching the TW/MIS
    side (codes=[] here)."""
    ts0 = dt.datetime(2026, 7, 20, 12, 0, 0).timestamp()
    trades = [Trade(code="BTCUSDT", price=65000.0 + i, qty=10.0, side="buy", ts=ts0 + i)
              for i in range(5)]
    trades.append(Trade(code="BTCUSDT", price=70000.0, qty=0.01, side="buy", ts=ts0 + 10))
    fake_binance = _FakeBinanceSource(trades)
    sender = _SendRecorder()

    state = monitor.main(
        codes=[], crypto_codes=["BTCUSDT"], max_iterations=5, sleep_seconds=0,
        binance_source=fake_binance, send_fn=sender,
    )

    assert fake_binance.subscribed == ["BTCUSDT"]
    assert fake_binance.saw_cancel is True  # main()'s finally: cancels the task on exit

    hist = state["history"]["BTCUSDT"]
    assert len(hist["trade_ticks"]) == 6
    assert hist["sides"] == ["buy"] * 6

    # The vwap breakout alert this trade sequence produces (same scenario as
    # tests/test_push_wiring.py) must have been sent and persisted.
    cache_path = monitor._cache_file("BTCUSDT", dt.date.today().isoformat())
    assert os.path.exists(cache_path)
    with open(cache_path, encoding="utf-8") as f:
        blob = json.load(f)
    assert blob["market"] == "crypto"
    assert len(blob["records"]) >= 1
    assert len(sender.messages) >= 1


def test_main_without_crypto_codes_never_starts_binance_task():
    fake_binance = _FakeBinanceSource([])
    monitor.main(
        codes=[], crypto_codes=[], max_iterations=1, sleep_seconds=0,
        binance_source=fake_binance, send_fn=_SendRecorder(),
    )
    assert fake_binance.subscribed is None  # subscribe() never called -> task never started
