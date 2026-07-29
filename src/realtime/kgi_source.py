# -*- coding: utf-8 -*-
"""KGI (凱基) SUPER PY adapter -- stub.

Not implemented this phase: the real integration depends on the SUPER PY
Python API (superpy.kgieworld.com.tw), which wraps a Windows-only .NET
component via pythonnet. It requires the user to sign the online API
consent form and confirm fees with KGI customer service before any code
here can be exercised (see docs/dev_plan_intraday.md Phase 0 / row "凱基
KGI SUPER PY" in section 1).

The interface below is shaped to match MISSource/BinanceSource so that once
the account is ready, this class can be filled in without touching
monitor.py or any other Phase 3 caller -- every method currently raises.
"""
from typing import List

from .base import RealtimeSource

_NOT_READY = (
    "KGI SUPER PY adapter 待使用者完成簽約與費用確認後實作，"
    "見 docs/dev_plan_intraday.md Phase 0"
)


class KGISource(RealtimeSource):
    """Stub adapter for KGI SUPER PY. All methods raise NotImplementedError."""

    supports_quote = False
    supports_trade = False
    supports_depth = False

    def subscribe(self, codes: List[str]) -> None:
        raise NotImplementedError(_NOT_READY)

    def on_quote(self, callback) -> None:
        raise NotImplementedError(_NOT_READY)

    def on_trade(self, callback) -> None:
        raise NotImplementedError(_NOT_READY)

    def on_depth(self, callback) -> None:
        raise NotImplementedError(_NOT_READY)

    async def run(self) -> None:
        raise NotImplementedError(_NOT_READY)
