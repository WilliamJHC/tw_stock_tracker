# -*- coding: utf-8 -*-
"""Abstract interface shared by polling (MIS) and push (Binance WS) adapters.

Design trade-off (see report to caller for the one-line summary): the
long-running entry point `run()` is always a coroutine, so monitor.py (Phase 3)
can `await` any adapter the same way regardless of transport. Polling adapters
implement `run()` as a simple loop of "synchronous poll_once() + asyncio.sleep",
so the actual network call stays a plain blocking `requests` call -- easy to
unit test without an event loop, and easy to swap for a different scheduler
later. Push adapters implement `run()` as a real async I/O loop (websockets).
Callback registration (on_quote/on_trade/on_depth) is deliberately NOT
abstract: adapters only support the streams their upstream actually provides,
and registering an unsupported stream raises NotImplementedError immediately
(fail fast at wiring time, not at first event).
"""
import abc
from typing import Callable, List, Optional

from .models import Depth, Quote, Trade

QuoteCallback = Callable[[Quote], None]
TradeCallback = Callable[[Trade], None]
DepthCallback = Callable[[Depth], None]


class RealtimeSource(abc.ABC):
    """Common interface for all intraday data adapters."""

    def __init__(self):
        self._codes: List[str] = []
        self._quote_cbs: List[QuoteCallback] = []
        self._trade_cbs: List[TradeCallback] = []
        self._depth_cbs: List[DepthCallback] = []

    # -- subscription -----------------------------------------------------
    @abc.abstractmethod
    def subscribe(self, codes: List[str]) -> None:
        """Register the symbols/codes this source should track.

        Cheap and synchronous for every adapter: it only stores state.
        Network I/O happens later, in run() / poll_once().
        """
        raise NotImplementedError

    # -- callback registration (concrete; adapters opt out per-stream) ----
    def on_quote(self, callback: QuoteCallback) -> None:
        if not self.supports_quote:
            raise NotImplementedError(f"{type(self).__name__} does not emit Quote events")
        self._quote_cbs.append(callback)

    def on_trade(self, callback: TradeCallback) -> None:
        if not self.supports_trade:
            raise NotImplementedError(f"{type(self).__name__} does not emit Trade events")
        self._trade_cbs.append(callback)

    def on_depth(self, callback: DepthCallback) -> None:
        if not self.supports_depth:
            raise NotImplementedError(f"{type(self).__name__} does not emit Depth events")
        self._depth_cbs.append(callback)

    # -- capability flags: adapters override the ones they support --------
    supports_quote = False
    supports_trade = False
    supports_depth = False

    # -- dispatch helpers ---------------------------------------------------
    def _emit_quote(self, quote: Quote) -> None:
        for cb in self._quote_cbs:
            cb(quote)

    def _emit_trade(self, trade: Trade) -> None:
        for cb in self._trade_cbs:
            cb(trade)

    def _emit_depth(self, depth: Depth) -> None:
        for cb in self._depth_cbs:
            cb(depth)

    # -- long-running loop --------------------------------------------------
    @abc.abstractmethod
    async def run(self) -> None:
        """Run the adapter until cancelled. Polling adapters: loop of
        poll_once() + asyncio.sleep(throttle). Push adapters: connect and
        consume the stream, reconnecting with backoff on failure."""
        raise NotImplementedError
