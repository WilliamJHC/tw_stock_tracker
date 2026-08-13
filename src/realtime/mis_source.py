# -*- coding: utf-8 -*-
"""TWSE MIS polling adapter.

Batches every subscribed code into a single request (MIS accepts `|`-joined
ex_ch values, e.g. `tse_2330.tw|tse_2317.tw`) so N codes cost 1 HTTP call per
poll, not N. Throttle default is a *conservative Phase 0 placeholder*: MIS
was never load-tested during live trading hours while this module was built
(see docs/dev_plan_intraday.md Phase 0), so poll_interval defaults to 10s
between batches -- to be recalibrated ("待下一交易日盤中補測校正") once the
spike in docs/spike_notes.md has real numbers.

Field mapping (verified against a live getStockInfo.jsp response):
    z = last price ('-' during the pre-open call auction -> fall back to pz)
    pz = simulated/pre-open matched price
    y = previous close
    v = accumulated volume, in lots (張)
    n = stock name
    a = ask prices, '_'-joined, low -> high (up to 5)
    f = ask volumes, '_'-joined, aligned with `a`
    b = bid prices, '_'-joined, high -> low (up to 5)
    g = bid volumes, '_'-joined, aligned with `b`
    t / '%' = time string "HH:MM:SS"

KNOWN GAP, confirmed live 2026-08-13 13:00-13:04 Taipei time (continuous
session, not pre-open): z and pz can both come back "-" for many consecutive
polls on a liquid, actively-trading stock (2330/2317/0050 all reproduced it)
even though v/t and the full bid/ask depth keep advancing normally each poll
-- ruled out both throttling (still happens at a clean 15s-spaced cadence)
and the tse+otc batch pairing (still happens querying a single tse_ code
alone). Root cause unconfirmed (undocumented endpoint, no official spec to
check against), but it is not a code bug in this module and not new. _parse_item()
falls back to the best bid/ask midpoint when z/pz are both unavailable
(source="twse-mis-midpoint") rather than letting the whole poll round degrade
to yfinance's delayed close just because one field is missing -- see its
docstring. If z/pz recover, revisit whether this fallback is still needed.

On request failure (RequestException, timeout, or unparsable body), or when
not even a bid/ask midpoint is available, the whole batch degrades to
yfinance via data_sources._yf_history -- imported, never copied, so
data_sources.py stays untouched.
"""
import datetime as dt
from typing import Callable, List, Optional

import requests

from data_sources import SourceError, UA, _yf_history
from .base import RealtimeSource
from .models import Depth, Quote

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
DEFAULT_POLL_INTERVAL = 10.0  # seconds; conservative Phase 0 default, see module docstring
PRETRADE_START = dt.time(8, 30)
PRETRADE_END = dt.time(9, 0)


def _num(item: dict, key: str) -> Optional[float]:
    s = item.get(key, "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _split_levels(item: dict, price_key: str, vol_key: str, limit: int = 5):
    """Parse '_'-joined price/vol strings into aligned float/int lists,
    dropping trailing empty slots (MIS pads unused levels with '_')."""
    raw_p = (item.get(price_key) or "").split("_")
    raw_v = (item.get(vol_key) or "").split("_")
    prices, vols = [], []
    for p, v in zip(raw_p, raw_v):
        p, v = p.strip(), v.strip()
        if not p or not v:
            continue
        try:
            prices.append(float(p))
            vols.append(int(float(v)))
        except ValueError:
            continue
        if len(prices) >= limit:
            break
    return prices, vols


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
        prev = _num(item, "y")
        bid_prices, bid_vols = _split_levels(item, "b", "g")
        ask_prices, ask_vols = _split_levels(item, "a", "f")

        price = _num(item, "z")
        if price is None:
            price = _num(item, "pz")
        source = "twse-mis"
        if price is None:
            # z/pz can both be "-" even on a live, actively-trading snapshot
            # (see module docstring's KNOWN GAP) while the order book stays
            # populated -- fall back to the best bid/ask midpoint rather than
            # dropping this quote and forcing the whole poll to degrade to
            # yfinance's delayed close over one missing field.
            if bid_prices and ask_prices:
                price = (bid_prices[0] + ask_prices[0]) / 2
            elif bid_prices:
                price = bid_prices[0]
            elif ask_prices:
                price = ask_prices[0]
            if price is not None:
                source = "twse-mis-midpoint"
        if price is None:
            return None

        change = price - prev if prev else None
        now = self._now_fn()
        code = (item.get("c") or "").strip()
        return Quote(
            code=code,
            price=price,
            prev_close=prev,
            change=change,
            change_pct=(change / prev * 100) if (change is not None and prev) else None,
            vol_lots=int(_num(item, "v") or 0),
            bid_prices=bid_prices,
            bid_vols=bid_vols,
            ask_prices=ask_prices,
            ask_vols=ask_vols,
            name=item.get("n") or code,
            source=source,
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
