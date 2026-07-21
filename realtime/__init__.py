# -*- coding: utf-8 -*-
"""Pluggable intraday realtime data layer (Phase 1 of dev_plan_intraday.md).

Re-exports the common models/interface and the three adapters so callers can
do `from realtime import MISSource, BinanceSource, KGISource, Quote, Trade,
Depth, RealtimeSource` without reaching into submodules.
"""
from .base import RealtimeSource
from .models import Depth, Quote, Trade
from .mis_source import MISSource
from .binance_source import BinanceSource
from .kgi_source import KGISource

__all__ = [
    "RealtimeSource",
    "Quote", "Trade", "Depth",
    "MISSource", "BinanceSource", "KGISource",
]
