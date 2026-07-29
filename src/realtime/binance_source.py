# -*- coding: utf-8 -*-
"""Binance public WebSocket adapter (aggTrade + partial depth).

No API key required. Subscribes to the combined stream endpoint so multiple
symbols share a single connection:
    wss://stream.binance.com:9443/stream?streams=<sym>@aggTrade/<sym>@depth20@1000ms/...
Each frame is wrapped as {"stream": "<sym>@aggTrade", "data": {...}}.

aggTrade.m (bool): True means the buyer is the maker, i.e. the trade was
initiated by an aggressive SELLER (taker sell). False -> taker buy.

Reconnection: exponential backoff starting at 1s, capped at 30s, reset to 1s
after a connection stays up long enough to receive at least one message.
These are Phase 0 conservative defaults pending the WS smoke-test spike in
docs/spike_notes.md.
"""
import asyncio
import json
from typing import List, Optional

from .base import RealtimeSource
from .models import Depth, Trade

STREAM_BASE = "wss://stream.binance.com:9443/stream"
DEFAULT_INITIAL_BACKOFF = 1.0
DEFAULT_MAX_BACKOFF = 30.0


class BinanceSource(RealtimeSource):
    """Push adapter for Binance aggTrade (tick-level trades) and partial depth."""

    supports_trade = True
    supports_depth = True
    supports_quote = False  # aggTrade/depth don't carry a full quote; use MISSource-style adapters for that

    def __init__(self, initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
                 max_backoff: float = DEFAULT_MAX_BACKOFF,
                 depth_levels: int = 20):
        super().__init__()
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.depth_levels = depth_levels

    def subscribe(self, codes: List[str]) -> None:
        self._codes = [c.lower() for c in codes]

    def _build_url(self) -> str:
        streams = []
        for sym in self._codes:
            streams.append(f"{sym}@aggTrade")
            streams.append(f"{sym}@depth{self.depth_levels}@1000ms")
        return f"{STREAM_BASE}?streams={'/'.join(streams)}"

    # -- parsing (pure, no network -- unit testable) -----------------------
    def _parse_agg_trade(self, symbol: str, data: dict) -> Trade:
        is_buyer_maker = bool(data.get("m"))
        side = "sell" if is_buyer_maker else "buy"  # buyer-maker -> aggressive seller
        return Trade(
            code=symbol.upper(),
            price=float(data["p"]),
            qty=float(data["q"]),
            side=side,
            ts=float(data.get("T") or data.get("E") or 0) / 1000.0,
            source="binance-aggtrade",
        )

    def _parse_depth(self, symbol: str, data: dict) -> Depth:
        bids = data.get("bids") or data.get("b") or []
        asks = data.get("asks") or data.get("a") or []
        bid_prices = [float(p) for p, _ in bids]
        bid_vols = [float(v) for _, v in bids]
        ask_prices = [float(p) for p, _ in asks]
        ask_vols = [float(v) for _, v in asks]
        return Depth(
            code=symbol.upper(), bid_prices=bid_prices, bid_vols=bid_vols,
            ask_prices=ask_prices, ask_vols=ask_vols, source="binance-depth",
        )

    def _handle_frame(self, frame: dict) -> None:
        """Dispatch one decoded combined-stream frame to callbacks."""
        stream = frame.get("stream", "")
        data = frame.get("data") or {}
        symbol = stream.split("@", 1)[0]
        if "@aggTrade" in stream:
            self._emit_trade(self._parse_agg_trade(symbol, data))
        elif "@depth" in stream:
            self._emit_depth(self._parse_depth(symbol, data))

    def next_backoff(self, current: float) -> float:
        """Pure function: exponential step, capped at max_backoff."""
        return min(current * 2, self.max_backoff)

    # -- long-running loop ---------------------------------------------------
    async def run(self) -> None:
        import websockets  # local import: keeps module importable without the dep at collection time
        backoff = self.initial_backoff
        url = self._build_url()
        while True:
            try:
                async with websockets.connect(url) as ws:
                    backoff = self.initial_backoff  # reset after a successful connect
                    async for raw in ws:
                        frame = json.loads(raw)
                        self._handle_frame(frame)
            except (asyncio.CancelledError,):
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = self.next_backoff(backoff)
