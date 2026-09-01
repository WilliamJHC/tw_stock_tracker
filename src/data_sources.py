# -*- coding: utf-8 -*-
"""Data layer with source priority FinMind > Fugle > yfinance > TWSE.

Every public getter returns plain dicts/lists (JSON-ready) and tags the
actual source used, so the UI can show where each number came from.
Daily-granularity data is cached under cache/ and reused within the same
calendar day to stay inside FinMind's rate limit (600 req/hr with token).
"""
import datetime as dt
import json
import os
import time

import requests

import env_utils

# Data files (cache/, static/, watchlist.json, .env) live at the repo root,
# one level up from src/.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
UA = {"User-Agent": "Mozilla/5.0 (stock-tracker local app)"}

# Back-off delays (seconds) between retries when FinMind returns a transient
# failure (network error or 5xx). Its length + 1 is the total attempt count;
# set to () to disable retries. Client errors (quota 402, auth 401/403) are
# never retried. Tests monkeypatch this to keep the suite fast.
FINMIND_RETRY_DELAYS = (0.5, 1.5)
# 429 is deliberately absent: FinMind's limit is hourly, so a rate limit cannot
# clear inside our back-off window -- retrying would spend three requests of an
# already-exhausted quota instead of one, pushing recovery further out. Fail
# fast and let callers degrade, same as 402/401.
_FINMIND_TRANSIENT_HTTP = {500, 502, 503, 504}
FINMIND_TIMEOUT = 30            # per-request timeout (seconds)
# Wall-clock ceiling for one _finmind() call, retries and back-off included.
# Equal to the per-request timeout on purpose: run_full() makes ~6 calls per
# stock and walks the watchlist sequentially, so retries must not raise the
# worst-case run time above what it was before retries existed. Consequence:
# a request that burns the whole budget (server accepting but never answering)
# gets no retry, while fast failures -- connection refused, prompt 5xx -- still
# get every one, which is the case retrying actually helps.
FINMIND_CALL_BUDGET = 30.0
_FINMIND_MIN_ATTEMPT = 1.0      # don't start an attempt with less budget than this left


class SourceError(Exception):
    pass


def _load_token():
    # Reads BASE_DIR at call time so tests can monkeypatch it (see
    # tests/test_data_sources.py). Shared parser lives in env_utils.
    return env_utils.load_env_value("FINMIND_TOKEN", BASE_DIR)


FINMIND_TOKEN = _load_token()


def _load_fugle_key():
    return env_utils.load_env_value("FUGLE_API_KEY", BASE_DIR)


FUGLE_API_KEY = _load_fugle_key()


def data_tier():
    """"premium" when a FinMind token is configured, else "free". A token buys
    request head-room (600 req/hr), not extra datasets: PER / institutional /
    margin / shareholding / SBL all work without one -- see those getters --
    and access to the paid tick dataset is reported separately by
    tick_tier_available(). Reflects *configuration*, not live validity: an
    expired token still reads "premium" until a real call 401s. Published for
    humans reading /api/config and the snapshot config; nothing gates on it."""
    return "premium" if FINMIND_TOKEN else "free"

# ---------------------------------------------------------------- cache --

def _cache_path(key):
    return os.path.join(CACHE_DIR, f"{key}.json")


def intraday_cache_file(code, date_str):
    """Path of the per-day intraday snapshot written by monitor.py and read
    back by intraday_feedback.py. Single source of truth for the filename
    convention, shared by both modules."""
    return os.path.join(CACHE_DIR, f"intraday_{code}_{date_str}.json")


def _cache_get(key):
    try:
        with open(_cache_path(key), encoding="utf-8") as f:
            blob = json.load(f)
        if blob.get("date") == dt.date.today().isoformat():
            return blob["data"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def _cache_put(key, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_path(key), "w", encoding="utf-8") as f:
        json.dump({"date": dt.date.today().isoformat(), "data": data}, f)


# -------------------------------------------------------------- finmind --

def _finmind(dataset, data_id=None, start_date=None, end_date=None):
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    headers = dict(UA)
    if FINMIND_TOKEN:
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"

    last_err = None
    deadline = time.monotonic() + FINMIND_CALL_BUDGET
    for attempt in range(len(FINMIND_RETRY_DELAYS) + 1):
        remaining = deadline - time.monotonic()
        if attempt and remaining < _FINMIND_MIN_ATTEMPT:
            break  # budget spent -- a further attempt would only add wall time
        try:
            resp = requests.get(
                FINMIND_URL, params=params, headers=headers,
                timeout=min(FINMIND_TIMEOUT, max(remaining, _FINMIND_MIN_ATTEMPT)),
            )
            body = resp.json()
        except requests.RequestException as e:
            last_err = SourceError(f"FinMind {dataset}: request failed ({e})")
        else:
            if resp.status_code == 200 and body.get("status") == 200:
                return body.get("data") or []
            err = SourceError(f"FinMind {dataset}: HTTP {resp.status_code} / {body.get('msg')}")
            body_status = body.get("status") or 0
            transient = resp.status_code in _FINMIND_TRANSIENT_HTTP or body_status >= 500
            if not transient:
                # Client-side errors (quota 402, auth 401/403, bad request) will
                # not fix themselves on retry -- fail fast so callers can degrade.
                raise err
            last_err = err
        if attempt < len(FINMIND_RETRY_DELAYS):
            time.sleep(min(FINMIND_RETRY_DELAYS[attempt], max(deadline - time.monotonic(), 0)))
    raise last_err


# ------------------------------------------------------------- yfinance --

def _yf_history(code, period, interval):
    """Try .TW then .TWO; return a non-empty DataFrame or raise."""
    import yfinance as yf
    for suffix in (".TW", ".TWO"):
        try:
            df = yf.Ticker(code + suffix).history(period=period, interval=interval, auto_adjust=False)
            if df is not None and len(df) > 0:
                return df
        except Exception:
            continue
    raise SourceError(f"yfinance: no data for {code}(.TW/.TWO)")


def _yf_history_since(code, start_date, interval):
    """Like _yf_history() but for an arbitrary-length window given by a
    start date instead of yfinance's fixed period buckets ("2y"/"5y"/...) --
    used by get_daily()'s fallback so a `days` far past 2y (e.g. the 10y
    backtest import) doesn't silently get truncated to 2y of history."""
    import yfinance as yf
    for suffix in (".TW", ".TWO"):
        try:
            df = yf.Ticker(code + suffix).history(start=start_date, interval=interval, auto_adjust=False)
            if df is not None and len(df) > 0:
                return df
        except Exception:
            continue
    raise SourceError(f"yfinance: no data for {code}(.TW/.TWO)")


def _df_to_hist(df):
    hist = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    for ts, row in df.iterrows():
        c = row.get("Close")
        if c is None or c != c:  # NaN
            continue
        def val(name):
            x = row.get(name)
            return float(c) if x is None or x != x else float(x)
        hist["t"].append(int(ts.timestamp()))
        hist["o"].append(val("Open"))
        hist["h"].append(val("High"))
        hist["l"].append(val("Low"))
        hist["c"].append(float(c))
        hist["v"].append(0 if row.get("Volume") != row.get("Volume") else int(row.get("Volume") or 0))
    if not hist["c"]:
        raise SourceError("yfinance: all rows NaN")
    return hist


# ----------------------------------------------------------------- fugle --

FUGLE_URL = "https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/{symbol}"


def _fugle_daily(code, start_date):
    """Fugle MarketData v1.0 daily candles -- a FinMind 429/402 (rate-limit/
    quota) long-range fallback, inserted in get_daily()'s chain AFTER
    FinMind and BEFORE yfinance (docs/https-finlab-finance-blog-python-get-
    tai-memoized-lighthouse.md Phase 4: "接，但只接一個位置" -- NOT a
    replacement for FinMind, and yfinance must stay below it since it's the
    only free source with a full back-adjusted series, see get_daily()'s
    docstring and that Phase's "不要為了接富果而降級 yfinance" warning).

    Verified 2026-08-18 against a live key: endpoint path, X-API-KEY header,
    and response shape (`{"data": [{"date","open","high","low","close",
    "volume"}, ...]}`) all confirmed correct for 2330. Close is raw
    (unadjusted, FinMind-scale) -- 30/30 overlapping trading days matched
    FinMind's raw close exactly (diff=0.000% every day), so `get_daily()`'s
    `adjusted=False` for this branch is correct. Caveat: the verification
    window had no corporate action for 2330, so it confirms price *scale*
    matches FinMind but doesn't independently exercise a split/dividend
    event; if a future check on a code with a recent action disagrees,
    revisit this assumption. Still inert with no key configured:
    _load_fugle_key() returns "" and this raises immediately, so
    get_daily() behaves exactly as it did before this function existed.
    """
    if not FUGLE_API_KEY:
        raise SourceError("fugle: no FUGLE_API_KEY configured")
    end_date = dt.date.today().isoformat()
    resp = requests.get(
        FUGLE_URL.format(symbol=code),
        headers={"X-API-KEY": FUGLE_API_KEY, **UA},
        params={"from": start_date, "to": end_date, "fields": "open,high,low,close,volume"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise SourceError(f"fugle: HTTP {resp.status_code} for {code}")
    rows = (resp.json() or {}).get("data") or []
    if not rows:
        raise SourceError(f"fugle: no data for {code}")
    hist = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    for r in rows:
        c = r.get("close")
        if c is None:
            continue
        d = dt.datetime.strptime(r["date"], "%Y-%m-%d")
        hist["t"].append(int(d.timestamp()))
        hist["o"].append(float(r.get("open") or c))
        hist["h"].append(float(r.get("high") or c))
        hist["l"].append(float(r.get("low") or c))
        hist["c"].append(float(c))
        hist["v"].append(int(r.get("volume") or 0))
    if not hist["c"]:
        raise SourceError(f"fugle: all rows unusable for {code}")
    return hist


# ----------------------------------------------------------------- twse --

def _roc_to_ts(roc_date):
    parts = str(roc_date).split("/")
    if len(parts) != 3:
        return None
    y = int(parts[0]) + 1911
    d = dt.datetime(y, int(parts[1]), int(parts[2]))
    return int(d.timestamp())


def _twse_daily(code, months=4):
    """TWSE STOCK_DAY monthly reports (listed stocks only)."""
    hist = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    today = dt.date.today()
    for i in range(months - 1, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        url = ("https://www.twse.com.tw/exchangeReport/STOCK_DAY"
               f"?response=json&date={y}{m:02d}01&stockNo={code}")
        try:
            body = requests.get(url, headers=UA, timeout=30).json()
        except Exception:
            continue
        for row in body.get("data") or []:
            # columns: 0 date(ROC) 1 shares 2 amount 3 open 4 high 5 low 6 close
            def num(s):
                s = str(s).replace(",", "")
                try:
                    return float(s)
                except ValueError:
                    return None
            close = num(row[6])
            ts = _roc_to_ts(row[0])
            if close is None or ts is None:
                continue
            o, h, l = num(row[3]), num(row[4]), num(row[5])
            hist["t"].append(ts)
            hist["o"].append(o if o is not None else close)
            hist["h"].append(h if h is not None else close)
            hist["l"].append(l if l is not None else close)
            hist["c"].append(close)
            hist["v"].append(int(num(row[1]) or 0))
    if not hist["c"]:
        raise SourceError(f"TWSE STOCK_DAY: no data for {code}")
    return hist


# ---------------------------------------------------------- public API --

def get_daily(code, days=730, cache=True):
    """`days`-window daily OHLCV (default ~2y). Volume unit: shares. Returns
    {t,o,h,l,c,v,source,adjusted}.

    adjusted=False for finmind/fugle/twse (raw, unadjusted for splits/
    dividends -- see src/adjust.py for the separate adj_factor pipeline that
    back-adjusts these; fugle's raw-scale assumption confirmed 2026-08-18
    against a live key, see _fugle_daily()'s docstring). adjusted=True for
    yfinance: its "Close"
    column (and the o/h/l we derive alongside it) is already split-adjusted
    upstream by Yahoo even with auto_adjust=False, though not dividend-
    adjusted -- so a yfinance-sourced bar is NOT on the same raw price scale
    as a finmind/fugle/twse one. Callers combining adj_factor (built from
    finmind-scale corporate_actions, see scripts/build_adj_factors.py) with
    a yfinance-sourced bar would double-count any split. In practice this
    only matters when FinMind is down for a `code` long enough to fall
    through past Fugle to yfinance -- the backtest DB's bars are 100%
    finmind-sourced as of Phase 1 (see docs/archive/backtest_plan.md).

    cache=False skips the daily on-disk cache entirely (read and write) --
    used by the backtest importer, which pulls a much longer `days` window
    than the live cache key (f"daily_{code}", no `days` in the key) can
    safely hold without corrupting what the live UI reads back."""
    cached = _cache_get(f"daily_{code}") if cache else None
    if cached:
        return cached

    hist = None
    # 1. FinMind
    try:
        start = (dt.date.today() - dt.timedelta(days=days + 30)).isoformat()
        rows = _finmind("TaiwanStockPrice", code, start)
        if rows:
            hist = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
            for r in rows:
                c = r.get("close")
                if not c:
                    continue
                d = dt.datetime.strptime(r["date"], "%Y-%m-%d")
                hist["t"].append(int(d.timestamp()))
                hist["o"].append(float(r.get("open") or c))
                hist["h"].append(float(r.get("max") or c))
                hist["l"].append(float(r.get("min") or c))
                hist["c"].append(float(c))
                hist["v"].append(int(r.get("Trading_Volume") or 0))
            hist["source"] = "finmind"
            hist["adjusted"] = False
    except (SourceError, requests.RequestException, ValueError, KeyError):
        hist = None

    # 2. Fugle (FinMind 429/402 long-range fallback -- verified 2026-08-18
    # against a live key, see _fugle_daily()'s docstring; inert without one)
    if not hist or not hist["c"]:
        try:
            start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
            hist = _fugle_daily(code, start)
            hist["source"] = "fugle"
            hist["adjusted"] = False  # confirmed raw/FinMind-scale, see _fugle_daily() docstring
        except (SourceError, requests.RequestException, ValueError, KeyError):
            hist = None

    # 3. yfinance
    if not hist or not hist["c"]:
        try:
            start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
            hist = _df_to_hist(_yf_history_since(code, start, "1d"))
            hist["source"] = "yfinance"
            hist["adjusted"] = True
        except (SourceError, Exception):
            hist = None

    # 4. TWSE (short window fallback)
    if not hist or not hist["c"]:
        hist = _twse_daily(code)
        hist["source"] = "twse"
        hist["adjusted"] = False

    if cache:
        _cache_put(f"daily_{code}", hist)
    return hist


def _mis_num(item, key):
    s = item.get(key, "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _mis_book_levels(item, price_key, vol_key, limit=5):
    """Parse an MIS '_'-joined price/vol depth string (e.g. item['b']/item['g'])
    into aligned float/int lists, dropping trailing empty slots (MIS pads
    unused levels with '_').

    Also drops a leading 0.0000 price slot -- confirmed live 2026-08-26 on
    limit-up stocks (3653/3443): when one side of the book is completely
    empty (no sellers at limit-up), MIS pads that side with '-' as expected,
    but can place a bogus 0.0000 in the *other* side's best-level slot ahead
    of the real resting orders (e.g. bid "0.0000_5725.0000_..." with 5725
    being the actual best bid). A 0 price is never a real order, so callers
    that fall back to prices[0] as "best level" (parse_mis_item()) would
    otherwise treat this placeholder as the current price.
    """
    raw_p = (item.get(price_key) or "").split("_")
    raw_v = (item.get(vol_key) or "").split("_")
    prices, vols = [], []
    for p, v in zip(raw_p, raw_v):
        p, v = p.strip(), v.strip()
        if not p or not v:
            continue
        try:
            fp = float(p)
            fv = int(float(v))
        except ValueError:
            continue
        if fp <= 0:
            continue
        prices.append(fp)
        vols.append(fv)
        if len(prices) >= limit:
            break
    return prices, vols


def parse_mis_item(item):
    """Parse one MIS msgArray item (one code) into a plain dict, or None if
    no code, or no price at all (not even a bid/ask estimate), can be
    determined -- deliberately does NOT fall back to prev close on its own;
    that last-resort call is caller-specific (see below), not shared parsing.
    Shared by get_quote() (single-code, used by pipeline/snapshot) and
    realtime.mis_source.MISSource (batched polling) so the z/pz fallback
    chain has exactly one implementation -- see docs/spike_notes.md for why
    this file mirrored mis_source.py's fix late (2026-08-13 fix landed
    there, not here, until 2026-08-19).

    Field mapping (verified against a live getStockInfo.jsp response):
        c = stock code; z = last price ('-' during the pre-open call auction
            -> fall back to pz); pz = simulated/pre-open matched price;
            y = previous close; v = accumulated volume, in lots (張);
            n = stock name; a/f = ask prices/volumes, '_'-joined, low->high
            (up to 5, aligned); b/g = bid prices/volumes, '_'-joined,
            high->low (up to 5, aligned); t / '%' = time string "HH:MM:SS".

    Price fallback, in order:
    1. z (last trade price)
    2. pz (pre-open call-auction simulated match price; z is legitimately
       "-" 08:30-09:00 Taipei time, before any real trade has happened)
    3. best bid/ask midpoint -- KNOWN GAP, confirmed live 2026-08-13 13:00-
       13:04 Taipei time (continuous session, not pre-open): z and pz can
       both come back "-" for many consecutive polls on a liquid, actively-
       trading stock even though v/t and the full bid/ask depth keep
       advancing normally each poll. Root cause unconfirmed (undocumented
       endpoint, no official spec to check against). Only one side present
       -> use that side. source is tagged "twse-mis-midpoint" (vs "twse-mis"
       for a real z/pz) so callers can tell an estimate from a real trade.

    When even the book is empty, this returns None rather than silently
    falling back to prev close (`y`) -- whether "nothing at all" should
    still render a stale prev-close price is a caller decision: get_quote()
    (single-stock dashboard display) does fall back to it so the UI always
    shows a number; MISSource._parse_item() (batched realtime poller)
    deliberately does not, and instead drops the quote so poll_once() can
    degrade the whole batch to yfinance rather than fabricate one code's
    price -- see that method's docstring.
    """
    code = (item.get("c") or "").strip()
    if not code:
        return None

    prev = _mis_num(item, "y")
    bid_prices, bid_vols = _mis_book_levels(item, "b", "g")
    ask_prices, ask_vols = _mis_book_levels(item, "a", "f")

    price = _mis_num(item, "z")
    if price is None:
        price = _mis_num(item, "pz")
    source = "twse-mis"
    if price is None:
        if bid_prices and ask_prices:
            price = (bid_prices[0] + ask_prices[0]) / 2
            source = "twse-mis-midpoint"
        elif bid_prices:
            price = bid_prices[0]
            source = "twse-mis-midpoint"
        elif ask_prices:
            price = ask_prices[0]
            source = "twse-mis-midpoint"
    if price is None:
        return None

    change = price - prev if prev else None
    return {
        "code": code,
        "price": price,
        "prev_close": prev,
        "change": change,
        "change_pct": (change / prev * 100) if (change is not None and prev) else None,
        "vol_lots": int(_mis_num(item, "v") or 0),  # accumulated lots (張)
        "name": item.get("n") or code,
        "source": source,
        "bid_prices": bid_prices, "bid_vols": bid_vols,
        "ask_prices": ask_prices, "ask_vols": ask_vols,
    }


def get_quote(code):
    """Realtime-ish quote. TWSE MIS first (covers TWSE+TPEX), then yfinance."""
    # 1. TWSE MIS
    try:
        ex_ch = f"tse_{code}.tw|otc_{code}.tw"
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
        body = requests.get(url, headers=UA, timeout=15).json()
        for item in body.get("msgArray") or []:
            parsed = parse_mis_item(item)
            if parsed is None:
                # parse_mis_item() found no z/pz and no bid/ask book at all --
                # unlike MISSource (batched realtime poller, which would
                # rather drop this quote and degrade the whole poll to
                # yfinance), this is a single-stock dashboard display: still
                # show prev close so the UI has *a* number rather than blank.
                prev = _mis_num(item, "y")
                if prev is None:
                    continue
                return {
                    "price": prev, "prev_close": prev, "change": 0.0, "change_pct": 0.0,
                    "vol_lots": int(_mis_num(item, "v") or 0),
                    "name": item.get("n") or code, "source": "twse-mis",
                }
            return {
                "price": parsed["price"],
                "prev_close": parsed["prev_close"],
                "change": parsed["change"],
                "change_pct": parsed["change_pct"],
                "vol_lots": parsed["vol_lots"],
                "name": parsed["name"],
                "source": parsed["source"],
            }
    except (requests.RequestException, ValueError):
        pass

    # 2. yfinance: last two daily closes
    try:
        df = _yf_history(code, "5d", "1d")
        closes = [float(x) for x in df["Close"].dropna()]
        vols = [int(x) for x in df["Volume"].fillna(0)]
        if closes:
            price = closes[-1]
            prev = closes[-2] if len(closes) > 1 else None
            change = price - prev if prev else None
            return {
                "price": price, "prev_close": prev, "change": change,
                "change_pct": (change / prev * 100) if (change is not None and prev) else None,
                "vol_lots": int(vols[-1] / 1000) if vols else 0,
                "name": code, "source": "yfinance",
            }
    except (SourceError, Exception):
        pass
    return None


def get_pe(code):
    """Current PER/PBR/dividend yield from FinMind (free tier). None on failure."""
    cached = _cache_get(f"pe_{code}")
    if cached is not None:
        return cached or None  # cached {} means known-missing
    result = None
    try:
        start = (dt.date.today() - dt.timedelta(days=14)).isoformat()
        rows = _finmind("TaiwanStockPER", code, start)
        if rows:
            r = rows[-1]
            result = {
                "pe": float(r["PER"]) if r.get("PER") else None,
                "pbr": float(r["PBR"]) if r.get("PBR") else None,
                "dividend_yield": float(r["dividend_yield"]) if r.get("dividend_yield") else None,
                "date": r.get("date"),
            }
    except (SourceError, requests.RequestException, ValueError, KeyError):
        result = None
    _cache_put(f"pe_{code}", result or {})
    return result


def get_minute(code):
    """5m bars, ~60 days, for order-flow estimation. Volume unit: shares."""
    cached = _cache_get(f"minute_{code}")
    if cached:
        return cached
    hist = _df_to_hist(_yf_history(code, "60d", "5m"))
    hist["source"] = "yfinance-5m"
    _cache_put(f"minute_{code}", hist)
    return hist


def get_ticks(code, date):
    """FinMind tick data for one day. Raises SourceError if tier is insufficient."""
    rows = _finmind("TaiwanStockPriceTick", code, date, date)
    return rows


_tick_tier_ok = None


def tick_tier_available(probe_code="2330"):
    """Probe once whether the token can access TaiwanStockPriceTick (paid tier)."""
    global _tick_tier_ok
    if _tick_tier_ok is not None:
        return _tick_tier_ok
    try:
        daily = get_daily(probe_code)
        last_date = dt.datetime.fromtimestamp(daily["t"][-1]).date().isoformat()
        rows = get_ticks(probe_code, last_date)
        _tick_tier_ok = bool(rows)
    except (SourceError, requests.RequestException, ValueError, KeyError, IndexError):
        _tick_tier_ok = False
    return _tick_tier_ok


def get_institutional(code, days=40, cache=True):
    """Recent institutional investors net buy/sell per day (FinMind, free).

    Returns list of {date, foreign, trust, dealer} in shares, oldest first,
    or None. `dealer` sums FinMind's two self-trade/hedging rows
    (Dealer_self / Dealer_Hedging) into one bucket.
    cache=False skips the on-disk cache (see get_daily()'s docstring)."""
    cached = _cache_get(f"inst_{code}") if cache else None
    if cached is not None:
        return cached or None
    result = None
    try:
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start)
        by_date = {}
        for r in rows:
            d = r.get("date")
            name = (r.get("name") or "").lower()
            net = int(r.get("buy") or 0) - int(r.get("sell") or 0)
            slot = by_date.setdefault(d, {"date": d, "foreign": 0, "trust": 0, "dealer": 0})
            if "foreign" in name:
                slot["foreign"] += net
            elif "investment_trust" in name:
                slot["trust"] += net
            elif "dealer" in name:
                slot["dealer"] += net
        if by_date:
            result = [by_date[d] for d in sorted(by_date)]
    except (SourceError, requests.RequestException, ValueError, KeyError):
        result = None
    if cache:
        _cache_put(f"inst_{code}", result or [])
    return result


def get_margin(code, days=40, cache=True):
    """Margin purchase / short sale balances via TaiwanStockMarginPurchaseShortSale.
    Returns [{date, margin_balance, margin_delta, short_balance, short_delta}] in shares
    (source unit is lots; multiply by 1000), oldest first, or None on failure.
    cache=False skips the on-disk cache (see get_daily()'s docstring)."""
    cached = _cache_get(f"margin_{code}") if cache else None
    if cached is not None:
        return cached or None
    result = None
    try:
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = _finmind("TaiwanStockMarginPurchaseShortSale", code, start)
        by_date = {}
        for r in rows:
            d = r.get("date")
            if not d:
                continue
            today_bal = int(float(r.get("MarginPurchaseTodayBalance") or 0)) * 1000
            yest_bal = int(float(r.get("MarginPurchaseYesterdayBalance") or 0)) * 1000
            short_today = int(float(r.get("ShortSaleTodayBalance") or 0)) * 1000
            short_yest = int(float(r.get("ShortSaleYesterdayBalance") or 0)) * 1000
            by_date[d] = {
                "date": d,
                "margin_balance": today_bal,
                "margin_delta": today_bal - yest_bal,
                "short_balance": short_today,
                "short_delta": short_today - short_yest,
            }
        if by_date:
            result = [by_date[d] for d in sorted(by_date)]
    except (SourceError, requests.RequestException, ValueError, KeyError):
        result = None
    if cache:
        _cache_put(f"margin_{code}", result or [])
    return result


def get_shareholding(code, days=40, cache=True):
    """Foreign shareholding via TaiwanStockShareholding.
    Returns [{date, foreign_ratio, foreign_shares}] (ratio in %), oldest first, or None.
    cache=False skips the on-disk cache (see get_daily()'s docstring)."""
    cached = _cache_get(f"sh_{code}") if cache else None
    if cached is not None:
        return cached or None
    result = None
    try:
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = _finmind("TaiwanStockShareholding", code, start)
        by_date = {}
        for r in rows:
            d = r.get("date")
            if not d:
                continue
            by_date[d] = {
                "date": d,
                "foreign_ratio": float(r.get("ForeignInvestmentSharesRatio") or 0),
                "foreign_shares": int(float(r.get("ForeignInvestmentShares") or 0)),
            }
        if by_date:
            result = [by_date[d] for d in sorted(by_date)]
    except (SourceError, requests.RequestException, ValueError, KeyError):
        result = None
    if cache:
        _cache_put(f"sh_{code}", result or [])
    return result


def get_sbl(code, days=40, cache=True):
    """SBL short sale balances via TaiwanDailyShortSaleBalances.
    Returns [{date, sbl_balance, sbl_sales, sbl_returns}] in shares (source is already
    in shares — do NOT multiply by 1000), oldest first, or None.
    cache=False skips the on-disk cache (see get_daily()'s docstring)."""
    cached = _cache_get(f"sbl_{code}") if cache else None
    if cached is not None:
        return cached or None
    result = None
    try:
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = _finmind("TaiwanDailyShortSaleBalances", code, start)
        by_date = {}
        for r in rows:
            d = r.get("date")
            if not d:
                continue
            by_date[d] = {
                "date": d,
                "sbl_balance": int(float(r.get("SBLShortSalesCurrentDayBalance") or 0)),
                "sbl_sales": int(float(r.get("SBLShortSalesShortSales") or 0)),
                "sbl_returns": int(float(r.get("SBLShortSalesReturns") or 0)),
            }
        if by_date:
            result = [by_date[d] for d in sorted(by_date)]
    except (SourceError, requests.RequestException, ValueError, KeyError):
        result = None
    if cache:
        _cache_put(f"sbl_{code}", result or [])
    return result


# ------------------------------------------------------- factor backtest --
# Getters below feed src/finlab_factors.py's multi-factor ranking --
# reproducing finlab.finance's AI-agent guide strategy on FinMind data
# instead of a FinLab subscription (see src/finlab_strategy.py for the
# FinLab-backed original this was ported from).

def get_universe(cache=True):
    """All TWSE/TPEX-listed 4-digit common stocks: [{code, name, market}]
    sorted by code, or None on failure. TaiwanStockInfo returns one row per
    (stock_id, listing-date-ish 'date'); duplicates collapse to the last
    seen row per stock_id since name/market don't change across rows."""
    cached = _cache_get("universe") if cache else None
    if cached is not None:
        return cached or None
    result = None
    try:
        rows = _finmind("TaiwanStockInfo")
        by_code = {}
        for r in rows:
            code = r.get("stock_id") or ""
            market = r.get("type")
            if len(code) == 4 and code.isdigit() and market in ("twse", "tpex"):
                by_code[code] = {"code": code, "name": r.get("stock_name"), "market": market}
        if by_code:
            result = sorted(by_code.values(), key=lambda x: x["code"])
    except (SourceError, requests.RequestException, ValueError, KeyError):
        result = None
    if cache:
        _cache_put("universe", result or [])
    return result


def get_monthly_revenue(code, days=760, cache=True):
    """Monthly revenue history (>=24mo so callers can derive YoY growth).
    Returns [{date, revenue, announce_date}] ascending, date = 1st of the
    revenue month (built from revenue_year/revenue_month, not FinMind's raw
    'date' field -- that field is an announcement-BATCH marker, not the
    revenue period, and not the same thing as announce_date below), or None.
    announce_date is FinMind's raw 'create_time' when non-empty -- close to
    the real disclosure date for RECENT periods (TWSE's "by the 10th of the
    following month" deadline), None for older rows: spot-checked on 2330
    back to 2013, create_time is "" for every row before ~2026-03's revenue
    period -- FinMind only started populating it recently, it is not a
    backfilled historical field. finlab_factors.revenue_yoy_series() uses it
    when present and falls back to the fixed REVENUE_LAG_DAYS lag otherwise,
    so this is a real point-in-time improvement only for months this recent;
    the rest of any historical backtest still runs on the fixed-lag
    assumption exactly as before. Deduplicates by (year, month) keeping the
    last row FinMind returns for that period -- restated revenue figures are
    a real occurrence, and without this a period could otherwise produce two
    output rows sharing one date. cache=False skips the on-disk cache (see
    get_daily()'s docstring)."""
    cached = _cache_get(f"revenue_{code}") if cache else None
    if cached is not None:
        return cached or None
    result = None
    try:
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = _finmind("TaiwanStockMonthRevenue", code, start)
        by_date = {}
        for r in rows:
            rev, year, month = r.get("revenue"), r.get("revenue_year"), r.get("revenue_month")
            if rev is None or not year or not month:
                continue
            date = f"{int(year):04d}-{int(month):02d}-01"
            by_date[date] = {"date": date, "revenue": float(rev), "announce_date": r.get("create_time") or None}
        if by_date:
            result = [by_date[d] for d in sorted(by_date)]
    except (SourceError, requests.RequestException, ValueError, KeyError):
        result = None
    if cache:
        _cache_put(f"revenue_{code}", result or [])
    return result


def get_financials(code, days=900, cache=True):
    """Quarterly net income, total equity, revenue, gross profit and
    operating income. Returns [{date, net_income, equity, revenue,
    gross_profit, operating_income}] ascending, date = quarter-end, or None.
    net_income (FinMind type IncomeAfterTaxes) is the single quarter's
    figure, not year-to-date cumulative -- spot-checked against 2330's
    2023 annual report (sum of 4 quarters ~= reported FY net income).

    revenue/gross_profit/operating_income (FinMind types Revenue/GrossProfit/
    OperatingIncome, confirmed present on 2330's TaiwanStockFinancialStatements
    response) ride along on the SAME income_rows request that net_income
    already reads -- zero extra requests (docs/https-finlab-finance-blog-
    python-get-tai-memoized-lighthouse.md Phase 6b). They're None for a date
    where that specific type is missing from the response, independent of
    the net_income/equity intersection below -- finlab_factors.py's margin-
    trend functions check for None themselves rather than this function
    silently dropping an otherwise-usable ROE row over a missing margin
    figure.

    cache=False skips the on-disk cache (see get_daily()'s docstring)."""
    cached = _cache_get(f"financials_{code}") if cache else None
    if cached is not None:
        return cached or None
    result = None
    try:
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        income_rows = _finmind("TaiwanStockFinancialStatements", code, start)
        equity_rows = _finmind("TaiwanStockBalanceSheet", code, start)
        income_by_date = {
            r["date"]: float(r["value"]) for r in income_rows
            if r.get("type") == "IncomeAfterTaxes" and r.get("value") is not None
        }
        equity_by_date = {
            r["date"]: float(r["value"]) for r in equity_rows
            if r.get("type") == "Equity" and r.get("value") is not None
        }
        revenue_by_date = {
            r["date"]: float(r["value"]) for r in income_rows
            if r.get("type") == "Revenue" and r.get("value") is not None
        }
        gross_profit_by_date = {
            r["date"]: float(r["value"]) for r in income_rows
            if r.get("type") == "GrossProfit" and r.get("value") is not None
        }
        operating_income_by_date = {
            r["date"]: float(r["value"]) for r in income_rows
            if r.get("type") == "OperatingIncome" and r.get("value") is not None
        }
        dates = sorted(set(income_by_date) & set(equity_by_date))
        out = [
            {
                "date": d, "net_income": income_by_date[d], "equity": equity_by_date[d],
                "revenue": revenue_by_date.get(d), "gross_profit": gross_profit_by_date.get(d),
                "operating_income": operating_income_by_date.get(d),
            }
            for d in dates
        ]
        if out:
            result = out
    except (SourceError, requests.RequestException, ValueError, KeyError):
        result = None
    if cache:
        _cache_put(f"financials_{code}", result or [])
    return result


def get_per_pbr(code, days=900, cache=True):
    """Daily PER/PBR/dividend yield from FinMind's TaiwanStockPER dataset.
    Returns [{date, per, pbr}] ascending, or None. Already trading-date-
    indexed off whatever EPS/book value FinMind treats as current knowledge
    that day -- unlike get_monthly_revenue()/get_financials()'s period-end
    dates, this needs no extra disclosure-lag applied by this project (see
    finlab_factors.value_series()). cache=False skips the on-disk cache
    (see get_daily()'s docstring)."""
    cached = _cache_get(f"per_pbr_{code}") if cache else None
    if cached is not None:
        return cached or None
    result = None
    try:
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = _finmind("TaiwanStockPER", code, start)
        out = [
            {
                "date": r["date"],
                "per": float(r["PER"]) if r.get("PER") else None,
                "pbr": float(r["PBR"]) if r.get("PBR") else None,
            }
            for r in rows if r.get("date")
        ]
        if out:
            result = out
    except (SourceError, requests.RequestException, ValueError, KeyError):
        result = None
    if cache:
        _cache_put(f"per_pbr_{code}", result or [])
    return result
