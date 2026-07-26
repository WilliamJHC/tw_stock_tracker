# -*- coding: utf-8 -*-
"""Data layer with source priority FinMind > yfinance > TWSE.

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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

def get_daily(code, days=730):
    """2y daily OHLCV. Volume unit: shares. Returns {t,o,h,l,c,v,source}."""
    cached = _cache_get(f"daily_{code}")
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
    except (SourceError, requests.RequestException, ValueError, KeyError):
        hist = None

    # 2. yfinance
    if not hist or not hist["c"]:
        try:
            hist = _df_to_hist(_yf_history(code, "2y", "1d"))
            hist["source"] = "yfinance"
        except (SourceError, Exception):
            hist = None

    # 3. TWSE (short window fallback)
    if not hist or not hist["c"]:
        hist = _twse_daily(code)
        hist["source"] = "twse"

    _cache_put(f"daily_{code}", hist)
    return hist


def get_quote(code):
    """Realtime-ish quote. TWSE MIS first (covers TWSE+TPEX), then yfinance."""
    # 1. TWSE MIS
    try:
        ex_ch = f"tse_{code}.tw|otc_{code}.tw"
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
        body = requests.get(url, headers=UA, timeout=15).json()
        for item in body.get("msgArray") or []:
            def num(k):
                s = item.get(k, "")
                try:
                    return float(s)
                except (TypeError, ValueError):
                    return None
            prev = num("y")
            price = num("z") or num("pz") or prev  # z='-' outside trading
            if price is None:
                continue
            change = price - prev if prev else None
            return {
                "price": price,
                "prev_close": prev,
                "change": change,
                "change_pct": (change / prev * 100) if (change is not None and prev) else None,
                "vol_lots": int(num("v") or 0),  # accumulated lots (張)
                "name": item.get("n") or code,
                "source": "twse-mis",
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


def get_institutional(code, days=40):
    """Recent institutional investors net buy/sell per day (FinMind, free).

    Returns list of {date, foreign, trust} in shares, oldest first, or None.
    """
    cached = _cache_get(f"inst_{code}")
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
            slot = by_date.setdefault(d, {"date": d, "foreign": 0, "trust": 0})
            if "foreign" in name:
                slot["foreign"] += net
            elif "investment_trust" in name:
                slot["trust"] += net
        if by_date:
            result = [by_date[d] for d in sorted(by_date)]
    except (SourceError, requests.RequestException, ValueError, KeyError):
        result = None
    _cache_put(f"inst_{code}", result or [])
    return result


def get_margin(code, days=40):
    """Margin purchase / short sale balances via TaiwanStockMarginPurchaseShortSale.
    Returns [{date, margin_balance, margin_delta, short_balance, short_delta}] in shares
    (source unit is lots; multiply by 1000), oldest first, or None on failure."""
    cached = _cache_get(f"margin_{code}")
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
    _cache_put(f"margin_{code}", result or [])
    return result


def get_shareholding(code, days=40):
    """Foreign shareholding via TaiwanStockShareholding.
    Returns [{date, foreign_ratio, foreign_shares}] (ratio in %), oldest first, or None."""
    cached = _cache_get(f"sh_{code}")
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
    _cache_put(f"sh_{code}", result or [])
    return result


def get_sbl(code, days=40):
    """SBL short sale balances via TaiwanDailyShortSaleBalances.
    Returns [{date, sbl_balance, sbl_sales, sbl_returns}] in shares (source is already
    in shares — do NOT multiply by 1000), oldest first, or None."""
    cached = _cache_get(f"sbl_{code}")
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
    _cache_put(f"sbl_{code}", result or [])
    return result
