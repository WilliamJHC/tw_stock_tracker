# -*- coding: utf-8 -*-
"""TWSE MIS polling adapter.

Batches every subscribed code into a single request (MIS accepts `|`-joined
ex_ch values, e.g. `tse_2330.tw|tse_2317.tw`) so N codes cost 1 HTTP call per
poll, not N. Throttle default is a *conservative Phase 0 placeholder*: MIS
was never load-tested during live trading hours while this module was built
(see docs/dev_plan_intraday.md Phase 0), so poll_interval defaults to 10s
between batches -- to be recalibrated ("待下一交易日盤中補測校正") once the
spike in docs/spike_notes.md has real numbers.

Per-item field parsing (z/pz/bid-ask-midpoint fallback chain, field mapping,
the pre-open-vs-continuous-session KNOWN GAP) lives in
data_sources.parse_mis_item() -- shared with data_sources.get_quote(), which
polls the same endpoint for a single code (pipeline/snapshot use). Was two
separate implementations until 2026-08-19: get_quote() had never picked up
this module's 2026-08-13 midpoint fix, so it silently rendered an intraday
quote as an unchanged prev-close whenever z/pz went missing mid-session. See
parse_mis_item()'s docstring for the fallback order and the KNOWN GAP detail.

On request failure (RequestException, timeout, or unparsable body), or when
not even a bid/ask midpoint is available, the whole batch degrades to
yfinance via data_sources._yf_history -- imported, never copied, so
data_sources.py stays untouched.
"""
import datetime as dt
from typing import Callable, List, Optional

import requests

from data_sources import SourceError, UA, _yf_history, parse_mis_item
from .base import RealtimeSource
from .models import Depth, Quote

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
DEFAULT_POLL_INTERVAL = 10.0  # seconds; conservative Phase 0 default, see module docstring
PRETRADE_START = dt.time(8, 30)
PRETRADE_END = dt.time(9, 0)


def _is_pretrade(now: dt.datetime) -> bool:
    t = now.time()
    return PRETRADE_START <= t < PRETRADE_END


class MISSource(RealtimeSource):
    """Polling adapter for TWSE MIS snapshot quotes + 5-level depth."""

    supports_quote = True
    supports_depth = True
    supports_trade = False  # MIS gives snapshots, not tick-level trades

    def __init__(self, poll_interval: float = DEFAULT_POLL_INTERVAL,
                 now_fn: Optional[Callable[[], dt.datetime]] = None):
        super().__init__()
        self.poll_interval = poll_interval
        self._now_fn = now_fn or dt.datetime.now

    def subscribe(self, codes: List[str]) -> None:
        self._codes = list(codes)

    def _fetch_raw(self) -> Optional[dict]:
        ex_ch = "|".join(f"tse_{c}.tw|otc_{c}.tw" for c in self._codes)
        url = f"{MIS_URL}?ex_ch={ex_ch}&json=1&delay=0"
        resp = requests.get(url, headers=UA, timeout=15)
        return resp.json()

    def _parse_item(self, item: dict) -> Optional[Quote]:
        """Field parsing (z/pz/midpoint fallback) lives in
        data_sources.parse_mis_item(), shared with get_quote(); this method
        just adapts that plain dict to this module's Quote dataclass and adds
        the polling-specific bits (ts, is_pretrade) parse_mis_item() can't
        know about."""
        parsed = parse_mis_item(item)
        if parsed is None:
            return None

        now = self._now_fn()
        return Quote(
            code=parsed["code"],
            price=parsed["price"],
            prev_close=parsed["prev_close"],
            change=parsed["change"],
            change_pct=parsed["change_pct"],
            vol_lots=parsed["vol_lots"],
            bid_prices=parsed["bid_prices"],
            bid_vols=parsed["bid_vols"],
            ask_prices=parsed["ask_prices"],
            ask_vols=parsed["ask_vols"],
            name=parsed["name"],
            source=parsed["source"],
            ts=now.timestamp(),
            is_pretrade=_is_pretrade(now),
        )

    def _degrade_to_yfinance(self, codes: List[str]) -> List[Quote]:
        quotes = []
        now = self._now_fn()
        for code in codes:
            try:
                df = _yf_history(code, "5d", "1d")
                closes = [float(x) for x in df["Close"].dropna()]
                if not closes:
                    continue
                price = closes[-1]
                prev = closes[-2] if len(closes) > 1 else None
                change = price - prev if prev else None
                quotes.append(Quote(
                    code=code, price=price, prev_close=prev, change=change,
                    change_pct=(change / prev * 100) if (change is not None and prev) else None,
                    vol_lots=0, name=code, source="yfinance-degraded",
                    ts=now.timestamp(), is_pretrade=False,
                ))
            except (SourceError, Exception):
                continue
        return quotes

    def poll_once(self) -> List[Quote]:
        """One polling round: fetch, parse, emit callbacks, return quotes.
        Synchronous and side-effect-only on network -- safe to unit test
        without an event loop."""
        if not self._codes:
            return []
        quotes: List[Quote] = []
        try:
            body = self._fetch_raw()
            for item in (body.get("msgArray") or []):
                q = self._parse_item(item)
                if q is not None:
                    quotes.append(q)
            if not quotes:
                raise SourceError("MIS: empty msgArray")
        except (requests.RequestException, ValueError, SourceError, AttributeError):
            quotes = self._degrade_to_yfinance(self._codes)

        for q in quotes:
            self._emit_quote(q)
            if q.bid_prices or q.ask_prices:
                self._emit_depth(Depth(
                    code=q.code, bid_prices=q.bid_prices, bid_vols=q.bid_vols,
                    ask_prices=q.ask_prices, ask_vols=q.ask_vols,
                    ts=q.ts, source=q.source,
                ))
        return quotes

    async def run(self) -> None:
        """Poll forever at self.poll_interval seconds until cancelled."""
        import asyncio
        while True:
            self.poll_once()
            await asyncio.sleep(self.poll_interval)
