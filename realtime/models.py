# -*- coding: utf-8 -*-
"""Shared data models for the realtime/ package.

Every adapter (MISSource, BinanceSource, KGISource) normalizes its raw
wire format into these dataclasses before invoking callbacks, so consumers
(Phase 2 intraday.py, Phase 3 monitor.py) never need to know which source
produced a given event.
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Quote:
    """Snapshot-style quote (TWSE MIS) or last-trade summary."""
    code: str
    price: Optional[float]
    prev_close: Optional[float]
    change: Optional[float]
    change_pct: Optional[float]
    vol_lots: int  # accumulated lots (張) for TW; 0 if not applicable
    bid_prices: List[float] = field(default_factory=list)   # high -> low, up to 5
    bid_vols: List[int] = field(default_factory=list)
    ask_prices: List[float] = field(default_factory=list)   # low -> high, up to 5
    ask_vols: List[int] = field(default_factory=list)
    name: str = ""
    source: str = ""
    ts: Optional[float] = None  # unix timestamp
    is_pretrade: bool = False   # True during TW pre-open call auction 08:30-09:00


@dataclass
class Trade:
    """Single trade / tick. side is None when only estimated (MIS tick-rule)."""
    code: str
    price: float
    qty: float
    side: Optional[str]  # "buy" | "sell" | None
    ts: Optional[float] = None
    source: str = ""


@dataclass
class Depth:
    """Order-book depth snapshot."""
    code: str
    bid_prices: List[float] = field(default_factory=list)
    bid_vols: List[int] = field(default_factory=list)
    ask_prices: List[float] = field(default_factory=list)
    ask_vols: List[int] = field(default_factory=list)
    ts: Optional[float] = None
    source: str = ""
