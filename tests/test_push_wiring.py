# -*- coding: utf-8 -*-
"""Unit tests for the async push-adapter wiring (Phase 3 crypto path).

Replaces scripts/verify_phase3_binance_alert.py, a throwaway harness that proved
this path against live Binance data and real Telegram on 2026-07-21 (log:
docs/spike_phase3_verify_log.txt, 78 alerts delivered) but was never a test.

The wiring it exercised -- source.run() as a concurrent asyncio task,
on_trade/on_depth registered as monitor.record_trade/record_depth, and a
periodic run_once() evaluating whatever has accumulated so far -- is now also
what monitor.main() does for its crypto_codes track (Phase 5). These tests
keep pinning the contract at the low level, against a scripted fake source:
no socket, no Telegram, no wall-clock waiting. See tests/test_monitor.py's
main()-level tests for the same contract exercised through the public API.

Async is driven with asyncio.run() inside ordinary sync tests, so this file
needs no pytest-asyncio dependency -- same reason tests/test_realtime.py tests
BinanceSource at the parse/backoff level only.
"""
import asyncio
import datetime as dt

import pytest

import data_sources as ds
import monitor
from realtime.base import RealtimeSource
from realtime.models import Depth, Trade


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "CACHE_DIR", str(tmp_path))


NOW0 = dt.datetime(2026, 7, 20, 12, 0, 0)
TS0 = NOW0.timestamp()
CODE = "BTCUSDT"


class _ScriptedPushSource(RealtimeSource):
    """Push adapter shaped like BinanceSource: emits a scripted event list
    through the base-class _emit_* helpers from an async run(), then idles
    until the caller cancels it (a real WS loop never returns on its own)."""

    supports_trade = True
    supports_depth = True

    def __init__(self, events):
        super().__init__()
        self._events = list(events)
        self.script_done = asyncio.Event()
        self.saw_cancel = False

    def subscribe(self, codes):
        self._codes = list(codes)

    async def run(self):
        try:
            for event in self._events:
                if isinstance(event, Trade):
                    self._emit_trade(event)
                else:
                    self._emit_depth(event)
                await asyncio.sleep(0)  # yield, so a concurrent consumer interleaves
            self.script_done.set()
            await asyncio.sleep(3600)  # idle until cancelled; never actually elapses
        except asyncio.CancelledError:
            self.saw_cancel = True
            raise


def _vwap_breakout_trades():
    """Five flat buys then a tiny high-priced buy -- the scenario
    tests/test_monitor.py uses to trigger vwap_breakout_up without depending on
    a CVD reversal (the tiny qty keeps the volume-weighted bands from catching
    up to the spike)."""
    trades = [Trade(code=CODE, price=65000.0 + i, qty=10.0, side="buy", ts=TS0 + i)
              for i in range(5)]
    trades.append(Trade(code=CODE, price=70000.0, qty=0.01, side="buy", ts=TS0 + 10))
    return trades


def _wire(state, source):
    """The wiring under test: exactly what the verify harness did, and what a
    future monitor.main() crypto branch would have to do."""
    source.on_trade(lambda t: monitor.record_trade(state, t))
    source.on_depth(lambda d: monitor.record_depth(state, d))


def test_push_callbacks_populate_state_while_run_task_is_live():
    depth = Depth(code=CODE, bid_prices=[64999.0], bid_vols=[100],
                  ask_prices=[65001.0], ask_vols=[5], ts=TS0 + 11)

    async def scenario():
        state = monitor.new_state()
        source = _ScriptedPushSource(_vwap_breakout_trades() + [depth])
        source.subscribe([CODE])
        _wire(state, source)

        task = asyncio.create_task(source.run())
        await source.script_done.wait()
        still_running = not task.done()  # run() outlives the script, as a WS loop does

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return state, still_running, source.saw_cancel

    state, still_running, saw_cancel = asyncio.run(scenario())

    hist = state["history"][CODE]
    assert len(hist["trade_ticks"]) == 6
    assert hist["sides"] == ["buy"] * 6
    assert hist["depth"] == {
        "bid_prices": [64999.0], "bid_vols": [100],
        "ask_prices": [65001.0], "ask_vols": [5],
    }
    assert still_running is True
    assert saw_cancel is True


def test_concurrent_run_once_alerts_on_state_fed_by_push_source():
    sent_messages = []

    def sender(message):
        sent_messages.append(message)
        return {"skipped": True, "reason": "test"}

    async def scenario():
        state = monitor.new_state()
        source = _ScriptedPushSource(_vwap_breakout_trades())
        source.subscribe([CODE])
        _wire(state, source)

        task = asyncio.create_task(source.run())
        rounds = []
        try:
            await source.script_done.wait()
            # The harness' evaluation loop, bounded to two rounds at a fixed
            # `now` so the second one lands inside the cooldown window.
            for _ in range(2):
                rounds.append(monitor.run_once([CODE], "crypto", state,
                                               source=None, send_fn=sender, now=NOW0))
                await asyncio.sleep(0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return rounds

    rounds = asyncio.run(scenario())

    assert "vwap_breakout_up" in {s["alert"]["event_type"] for s in rounds[0]}
    assert rounds[1] == []  # dedupe_and_cooldown stops the periodic loop re-sending
    assert len(sent_messages) == len(rounds[0])


def test_run_task_failure_leaves_accumulated_state_usable():
    """A dead socket must not cost the ticks already pushed: the evaluation
    loop still alerts on whatever made it into `state` before the failure."""

    class _BoomSource(_ScriptedPushSource):
        async def run(self):
            for event in self._events:
                self._emit_trade(event)
                await asyncio.sleep(0)
            raise RuntimeError("websocket died")

    async def scenario():
        state = monitor.new_state()
        source = _BoomSource(_vwap_breakout_trades())
        source.subscribe([CODE])
        _wire(state, source)

        task = asyncio.create_task(source.run())
        with pytest.raises(RuntimeError):
            await task
        return state

    state = asyncio.run(scenario())

    sent = monitor.run_once([CODE], "crypto", state,
                            source=None, send_fn=lambda m: None, now=NOW0)
    assert "vwap_breakout_up" in {s["alert"]["event_type"] for s in sent}
