# -*- coding: utf-8 -*-
"""Market-data fetch layer for the forward shadow portfolio (spec: docs/shadow_portfolio_spec_2026-11.md,
A2 / A5b-1 / A5b-4 / A5c-1,5 / A5d-6 / A9).

Fetches, once per trading day: whole-market closes for TWSE (listed) and TPEx (OTC), ex-rights/dividend
tables for both, and the TWSE holiday calendar. Design rule: when the data is in doubt, report "missing"
(``None`` / ``adj_missing``) instead of guessing. Public functions never raise to the caller; the reason is
logged with ``logging.warning``.

Endpoints (all verified 2026-11-16/17; the first four are recorded in spec A9)
-------------------------------------------------------------------------------
* TWSE close  https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999&response=json
    Top-level ``stat`` ("OK") and ``date`` (YYYYMMDD, must equal the request). The stock table is the one whose
    header starts with 證券代號 (the response also carries index / statistics tables). Non-trading days answer
    ``{"stat": "很抱歉，沒有符合條件的資料!"}`` (also true for typhoon-closure days, e.g. 2024-07-24).
    Column 9 (漲跌(+/-)) is HTML; ``<p>X</p>`` marks an ex-right/dividend day. No-trade rows show ``--``.
* TPEx close  https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date=YYYY%2FMM%2FDD&type=EW&response=json
    Top-level ``date`` is YYYYMMDD, ``tables[0]['date']`` is ROC "115/07/16". Non-trading days return an empty
    ``data`` list. Change column holds 除息 / 除權 / 除權息 on ex days; no-trade rows show ``----``.
    The legacy ``stk_quote_result.php`` endpoint ignores the date and must not be used.
* TWSE ex-rights  https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate=YYYYMMDD&endDate=YYYYMMDD&response=json
    Rows start with ROC "115年07月16日". An empty range answers ``{"stat": "很抱歉，沒有符合條件的資料!"}``.
* TPEx ex-rights  https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ?startDate=YYYY%2FMM%2FDD&endDate=...
    Rows start with ROC "115/07/16". Sending only ``date``/``d`` silently returns the latest day, so row
    dates are always checked against the requested range. An empty range returns ``data: []``.
* TWSE holidays  https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=YYYY&response=json
    (explored 2026-11-17; see "Trading calendar findings" below).

Trading calendar findings (probed years 2024, 2025, 2026, 2027, 2028, 1990)
---------------------------------------------------------------------------
* Response: ``{"stat": "ok", "queryYear": Y, "fields": ["日期","名稱","說明"], "data": [["YYYY-MM-DD", name, note]]}``.
* The table lists days the market does not open, including weekend days inside a holiday block and
  make-up holidays (e.g. 2026-02-27, 2026-10-09 補假). Saturday make-up working days are not trading days
  either and are not listed; we never add Saturdays, so they stay closed.
* It also lists days that are NOT closures: rows named "...開始交易日" / "...最後交易日" (e.g. 2026-01-02,
  2026-02-11, 2026-02-23) are trading days and are excluded from the closure set. Rows named
  "市場無交易，僅辦理結算交割作業" (2026-02-12/13) are closures.
* Typhoon closures are NOT in the table (2024-07-24/25, 2024-10-02, 2024-10-31 are absent from the 2024
  table; MI_INDEX for 2024-07-24 answers "no data"). Such days therefore look like trading days here and must
  be handled as missing-data days by the caller (spec A5b-4: the close endpoint failing = missing-data day).
* A year that is not published yet (2028, 1990) answers ``stat: ok`` with ``data: []``. That is treated as
  failure (``None``); otherwise every weekday would be taken as a trading day. 2027 is already published.
* Sanity gate: fewer than ``CALENDAR_MIN_CLOSED_WEEKDAYS`` weekday closures (every real year has the
  Lunar New Year block alone) is treated as failure.

Interface notes
---------------
* ``fetch_*_close`` return ``{code: {"o","h","l","c","v","status"}}`` for 4-digit numeric codes only; ``None``
  on any failure. A malformed 4-digit row (unparseable price/volume other than the no-trade marker, short
  row, duplicate code) fails the whole fetch rather than silently dropping a stock.
* ``fetch_*_exright`` return ``{(date, code): {"pre_close", "ref_price"}}``; ``{}`` when the endpoint
  cleanly says "no data" for the range; ``None`` on failure. A 4-digit row whose prices cannot be parsed is
  skipped with a warning (``adj_factor`` then falls back because the quote is still flagged exright).
* ``adj_factor``'s ``exright_tables`` argument is a dict with these optional keys:
    ``"twse"`` / ``"tpex"``              result of ``fetch_*_exright`` (``None`` = fetch failed)
    ``"twse_close"`` / ``"tpex_close"``  result of ``fetch_*_close`` for the same day (``None`` = failed)
"""
import datetime
import json
import logging
import math
import re
import time

import requests

logger = logging.getLogger(__name__)

REQUEST_INTERVAL = 3.0  # seconds between two requests (tests monkeypatch this to 0)
TIMEOUT = 40  # seconds; TPEx close takes 6-12 s from GitHub Actions
HEADERS = {"User-Agent": "Mozilla/5.0"}

TWSE_MIN_CODES = 900  # fewer 4-digit listed codes than this = abnormal (normal: ~1,090-1,099)
TPEX_MIN_CODES = 700  # fewer 4-digit OTC codes than this = abnormal (normal: ~835-895)
CALENDAR_MIN_CLOSED_WEEKDAYS = 5

TWSE_CLOSE_URL = ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
                  "?date={ymd}&type=ALLBUT0999&response=json")
TPEX_CLOSE_URL = ("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
                  "?date={y}%2F{m}%2F{d}&type=EW&response=json")
TWSE_EXRIGHT_URL = ("https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
                    "?startDate={s}&endDate={e}&response=json")
TPEX_EXRIGHT_URL = ("https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ"
                    "?startDate={sy}%2F{sm}%2F{sd}&endDate={ey}%2F{em}%2F{ed}&response=json")
TWSE_HOLIDAY_URL = ("https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"
                    "?date={year}&response=json")
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_CODE_RE = re.compile(r"\d{4}")
_TAG_RE = re.compile(r"<[^>]*>")
_NO_TRADE_MARKERS = {"--", "----"}
_NO_DATA_TEXT = "沒有符合條件"
# Names of rows in the holiday table that are trading days (NOT closures).
_TRADING_DAY_NAME_RE = re.compile(r"(開始|最後)交易日$")

_last_request_at = None


# --------------------------------------------------------------------------- HTTP

def _throttle():
    """Sleep so that consecutive requests are at least REQUEST_INTERVAL apart."""
    global _last_request_at
    if _last_request_at is not None and REQUEST_INTERVAL > 0:
        wait = REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
    _last_request_at = time.monotonic()


def _get_json(url, label):
    """GET ``url`` and return the parsed JSON, or None (with a warning) on any failure."""
    _throttle()
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.warning("%s: request failed: %s", label, e)
        return None
    if r.status_code != 200:
        logger.warning("%s: HTTP %s", label, r.status_code)
        return None
    r.encoding = "utf-8"
    try:
        return json.loads(r.text)
    except ValueError as e:
        logger.warning("%s: JSON parse failed: %s", label, e)
        return None


# --------------------------------------------------------------------------- cleaning helpers

def _clean(s):
    return str(s).replace(",", "").strip() if s is not None else ""


def _parse_price(s):
    """Return a positive float, None for the no-trade marker; raise ValueError for anything else."""
    t = _clean(s)
    if t in _NO_TRADE_MARKERS:
        return None
    v = float(t)  # ValueError on garbage
    if not math.isfinite(v) or v <= 0:
        raise ValueError("non-positive price %r" % s)
    return v


def _parse_volume(s):
    t = _clean(s)
    v = int(t)  # ValueError on garbage
    if v < 0:
        raise ValueError("negative volume %r" % s)
    return v


def _is_code(s):
    return bool(_CODE_RE.fullmatch(str(s).strip()))


def _header_ok(fields, expected):
    """expected: {column index: substring that must appear in the stripped header}."""
    if not isinstance(fields, list):
        return False
    for idx, needle in expected.items():
        if idx >= len(fields) or needle not in str(fields[idx]).strip():
            return False
    return True


def _roc_date(text):
    """'115年07月16日' or '115/07/16' -> datetime.date; None when not parseable."""
    m = re.fullmatch(r"\s*(\d{2,3})\s*[年/]\s*(\d{1,2})\s*[月/]\s*(\d{1,2})\s*日?\s*", str(text))
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_stock_rows(rows, idx, flag_fn, label, min_codes):
    """Shared row loop for both close endpoints. Returns the result dict or None."""
    out = {}
    need = max(idx.values()) + 1
    for row in rows:
        if not isinstance(row, list) or not row:
            logger.warning("%s: malformed row %r", label, row)
            return None
        code = str(row[0]).strip()
        if not _is_code(code):
            continue
        if len(row) < need:
            logger.warning("%s: row for %s has %d columns, need %d", label, code, len(row), need)
            return None
        if code in out:
            logger.warning("%s: duplicate code %s", label, code)
            return None
        try:
            c = _parse_price(row[idx["c"]])
            entry = {
                "o": _parse_price(row[idx["o"]]),
                "h": _parse_price(row[idx["h"]]),
                "l": _parse_price(row[idx["l"]]),
                "c": c,
                "v": _parse_volume(row[idx["v"]]),
            }
        except (ValueError, TypeError) as e:
            logger.warning("%s: unparseable row for %s (%s): %r", label, code, e, row)
            return None
        if c is None:
            entry["status"] = "no_close"
        elif flag_fn(row[idx["flag"]]):
            entry["status"] = "exright"
        else:
            entry["status"] = "ok"
        out[code] = entry
    if len(out) < min_codes:
        logger.warning("%s: only %d 4-digit codes (< %d), treating as missing", label, len(out), min_codes)
        return None
    return out


# --------------------------------------------------------------------------- daily close

def fetch_twse_close(date):
    """Whole-market TWSE (listed) close for ``date``; see module docstring. None on failure."""
    label = "TWSE close %s" % date
    try:
        ymd = date.strftime("%Y%m%d")
    except AttributeError:
        logger.warning("%s: bad date argument", label)
        return None
    j = _get_json(TWSE_CLOSE_URL.format(ymd=ymd), label)
    if j is None:
        return None
    if not isinstance(j, dict) or j.get("stat") != "OK":
        logger.warning("%s: stat=%r (non-trading day or no data)", label,
                       j.get("stat") if isinstance(j, dict) else j)
        return None
    if j.get("date") != ymd:
        logger.warning("%s: response date %r != requested %s", label, j.get("date"), ymd)
        return None
    tables = j.get("tables")
    if not isinstance(tables, list):
        logger.warning("%s: no tables in response", label)
        return None
    candidates = [t for t in tables
                  if isinstance(t, dict) and isinstance(t.get("data"), list)
                  and _header_ok(t.get("fields"), {0: "代號"})]
    if not candidates:
        logger.warning("%s: stock table (header 證券代號) not found", label)
        return None
    table = max(candidates, key=lambda t: len(t["data"]))
    expected = {0: "代號", 2: "成交股數", 5: "開盤", 6: "最高", 7: "最低", 8: "收盤", 9: "漲跌"}
    if not _header_ok(table.get("fields"), expected):
        logger.warning("%s: unexpected column layout %r", label, table.get("fields"))
        return None
    idx = {"v": 2, "o": 5, "h": 6, "l": 7, "c": 8, "flag": 9}
    return _parse_stock_rows(table["data"], idx,
                             lambda cell: _TAG_RE.sub("", str(cell)).strip() == "X",
                             label, TWSE_MIN_CODES)


def fetch_tpex_close(date):
    """Whole-market TPEx (OTC) close for ``date``; see module docstring. None on failure."""
    label = "TPEx close %s" % date
    try:
        ymd = date.strftime("%Y%m%d")
        url = TPEX_CLOSE_URL.format(y=date.strftime("%Y"), m=date.strftime("%m"), d=date.strftime("%d"))
    except AttributeError:
        logger.warning("%s: bad date argument", label)
        return None
    j = _get_json(url, label)
    if j is None:
        return None
    if not isinstance(j, dict) or str(j.get("stat", "")).lower() != "ok":
        logger.warning("%s: stat=%r", label, j.get("stat") if isinstance(j, dict) else j)
        return None
    if j.get("date") != ymd:
        logger.warning("%s: response date %r != requested %s", label, j.get("date"), ymd)
        return None
    tables = j.get("tables")
    if not isinstance(tables, list) or not tables or not isinstance(tables[0], dict):
        logger.warning("%s: no tables in response", label)
        return None
    table = tables[0]
    tdate = table.get("date")
    if tdate is not None and _roc_date(tdate) != date:
        logger.warning("%s: table date %r != requested", label, tdate)
        return None
    if not isinstance(table.get("data"), list):
        logger.warning("%s: no data list", label)
        return None
    expected = {0: "代號", 2: "收盤", 3: "漲跌", 4: "開盤", 5: "最高", 6: "最低", 7: "成交股數"}
    if not _header_ok(table.get("fields"), expected):
        logger.warning("%s: unexpected column layout %r", label, table.get("fields"))
        return None
    idx = {"c": 2, "flag": 3, "o": 4, "h": 5, "l": 6, "v": 7}
    return _parse_stock_rows(table["data"], idx, lambda cell: "除" in str(cell),
                             label, TPEX_MIN_CODES)


# --------------------------------------------------------------------------- ex-rights / dividends

def _parse_exright_rows(rows, start, end, label):
    """rows: [date, code, name, pre_close, ref_price, ...]. Returns dict or None (date out of range)."""
    out = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            logger.warning("%s: short/malformed row %r", label, row)
            return None
        d = _roc_date(row[0])
        if d is None or not (start <= d <= end):
            logger.warning("%s: row date %r outside requested %s..%s (endpoint may have ignored the range)",
                           label, row[0], start, end)
            return None
        code = str(row[1]).strip()
        if not _is_code(code):
            continue
        try:
            pre = float(_clean(row[3]))
            ref = float(_clean(row[4]))
        except ValueError:
            logger.warning("%s: unparseable prices for %s, row skipped: %r", label, code, row[:5])
            continue
        if not (math.isfinite(pre) and math.isfinite(ref) and pre > 0 and ref > 0):
            logger.warning("%s: non-positive prices for %s, row skipped: %r", label, code, row[:5])
            continue
        out.setdefault((d, code), {"pre_close": pre, "ref_price": ref})
    return out


def fetch_twse_exright(start, end):
    """TWSE ex-rights/dividend table for ``start..end`` (inclusive). None on failure, {} when no events."""
    label = "TWSE exright %s..%s" % (start, end)
    try:
        url = TWSE_EXRIGHT_URL.format(s=start.strftime("%Y%m%d"), e=end.strftime("%Y%m%d"))
    except AttributeError:
        logger.warning("%s: bad date argument", label)
        return None
    if start > end:
        logger.warning("%s: start after end", label)
        return None
    j = _get_json(url, label)
    if j is None:
        return None
    if not isinstance(j, dict):
        logger.warning("%s: unexpected response type", label)
        return None
    stat = str(j.get("stat", ""))
    if _NO_DATA_TEXT in stat:
        return {}
    if stat != "OK":
        logger.warning("%s: stat=%r", label, stat)
        return None
    if not _header_ok(j.get("fields"), {1: "代號", 3: "前收盤", 4: "參考價"}) or not isinstance(j.get("data"), list):
        logger.warning("%s: unexpected column layout %r", label, j.get("fields"))
        return None
    return _parse_exright_rows(j["data"], start, end, label)


def fetch_tpex_exright(start, end):
    """TPEx ex-rights/dividend table for ``start..end`` (inclusive). None on failure, {} when no events."""
    label = "TPEx exright %s..%s" % (start, end)
    try:
        url = TPEX_EXRIGHT_URL.format(
            sy=start.strftime("%Y"), sm=start.strftime("%m"), sd=start.strftime("%d"),
            ey=end.strftime("%Y"), em=end.strftime("%m"), ed=end.strftime("%d"))
    except AttributeError:
        logger.warning("%s: bad date argument", label)
        return None
    if start > end:
        logger.warning("%s: start after end", label)
        return None
    j = _get_json(url, label)
    if j is None:
        return None
    if not isinstance(j, dict) or str(j.get("stat", "")).lower() != "ok":
        logger.warning("%s: stat=%r", label, j.get("stat") if isinstance(j, dict) else j)
        return None
    echoed = j.get("date")
    expected_echo = "%s~%s" % (start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if echoed is not None and echoed != expected_echo:
        logger.warning("%s: echoed range %r != %s", label, echoed, expected_echo)
        return None
    tables = j.get("tables")
    if not isinstance(tables, list) or not tables or not isinstance(tables[0], dict):
        logger.warning("%s: no tables in response", label)
        return None
    table = tables[0]
    if not _header_ok(table.get("fields"), {0: "日期", 1: "代號", 3: "前收盤", 4: "參考價"}) \
            or not isinstance(table.get("data"), list):
        logger.warning("%s: unexpected column layout %r", label, table.get("fields"))
        return None
    return _parse_exright_rows(table["data"], start, end, label)


# --------------------------------------------------------------------------- adjustment factor

def yahoo_adj_ratio(date, code, market):
    """Day adjustment factor from Yahoo adjusted closes (default fallback for ``adj_factor``).

    ``market`` is "twse" (suffix .TW) or "tpex" (suffix .TWO). Factor = (adjclose/close ratio on the previous
    bar) / (same ratio on ``date``) = ref_price / pre_close when ``date`` is an ex day. Returns None when the
    data is missing or implausible (> 1.01 or <= 0).
    """
    label = "Yahoo adj %s %s" % (code, date)
    suffix = {"twse": ".TW", "tpex": ".TWO"}.get(market)
    if suffix is None:
        logger.warning("%s: unknown market %r", label, market)
        return None
    tz = datetime.timezone(datetime.timedelta(hours=8))
    p1 = datetime.datetime.combine(date - datetime.timedelta(days=10), datetime.time(0, 0), tzinfo=tz)
    p2 = datetime.datetime.combine(date + datetime.timedelta(days=4), datetime.time(0, 0), tzinfo=tz)
    url = (YAHOO_CHART_URL.format(symbol=code + suffix)
           + "?period1=%d&period2=%d&interval=1d&includeAdjustedClose=true"
           % (int(p1.timestamp()), int(p2.timestamp())))
    j = _get_json(url, label)
    if j is None:
        return None
    try:
        result = j["chart"]["result"][0]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        adj = result["indicators"]["adjclose"][0]["adjclose"]
    except (TypeError, KeyError, IndexError):
        logger.warning("%s: unexpected Yahoo response shape", label)
        return None
    if not (len(stamps) == len(closes) == len(adj)):
        logger.warning("%s: Yahoo arrays differ in length", label)
        return None
    bars = []  # (date, adjclose/close) for rows with both values
    for ts, c, a in zip(stamps, closes, adj):
        if c is None or a is None or c <= 0 or a <= 0:
            continue
        bars.append((datetime.datetime.fromtimestamp(ts, tz).date(), a / c))
    idx = [i for i, (d, _) in enumerate(bars) if d == date]
    if len(idx) != 1 or idx[0] == 0:
        logger.warning("%s: bar for the date or its previous bar missing", label)
        return None
    factor = bars[idx[0] - 1][1] / bars[idx[0]][1]
    if not (math.isfinite(factor) and 0 < factor <= 1.01):
        logger.warning("%s: implausible factor %r", label, factor)
        return None
    return min(factor, 1.0)


def adj_factor(date, code, exright_tables, fallback=None):
    """Day adjustment factor (ref_price / pre_close) for ``code`` on ``date``.

    Returns ``(factor, source)`` with source in {"twse", "tpex", "yahoo", "adj_missing"}:
      * (date, code) in the market's ex-rights table      -> (ref/pre, market)
      * not in the table, quote not flagged exright       -> (1.0, market)
      * not in the table, quote flagged exright           -> fallback(date, code) -> (f, "yahoo")
        (``fallback=None`` means the default ``yahoo_adj_ratio``); fallback fails -> (None, "adj_missing")
      * market cannot be determined (code absent from both quote dicts and tables) -> (None, "adj_missing")
    ``exright_tables`` layout: see module docstring.
    """
    tables = exright_tables or {}
    market = None
    for m in ("twse", "tpex"):
        t = tables.get(m)
        if t and (date, code) in t:
            market = m
            break
    if market is not None:
        entry = tables[market][(date, code)]
        pre, ref = entry.get("pre_close"), entry.get("ref_price")
        if pre and ref and pre > 0 and ref > 0:
            return ref / pre, market
        logger.warning("adj_factor %s %s: bad table entry %r", code, date, entry)
        return None, "adj_missing"

    quote = None
    for m in ("twse", "tpex"):
        q = tables.get(m + "_close")
        if q and code in q:
            market, quote = m, q[code]
            break
    if market is None:
        logger.warning("adj_factor %s %s: no quote and no table entry, cannot tell", code, date)
        return None, "adj_missing"
    if quote.get("status") != "exright":
        return 1.0, market

    try:
        value = fallback(date, code) if fallback is not None else yahoo_adj_ratio(date, code, market)
    except Exception as e:  # a custom fallback must never break the daily job
        logger.warning("adj_factor %s %s: fallback raised %r", code, date, e)
        value = None
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        logger.warning("adj_factor %s %s: exright day, table and fallback both failed", code, date)
        return None, "adj_missing"
    return float(value), "yahoo"


# --------------------------------------------------------------------------- trading calendar

def fetch_trading_calendar(year):
    """All trading days of ``year`` (Mon-Fri minus TWSE closure days), or None on failure.

    Typhoon closures are not in the TWSE table (see module docstring), so they appear as trading days here.
    """
    label = "TWSE calendar %s" % year
    j = _get_json(TWSE_HOLIDAY_URL.format(year=year), label)
    if j is None:
        return None
    if not isinstance(j, dict) or str(j.get("stat", "")).lower() != "ok":
        logger.warning("%s: stat=%r", label, j.get("stat") if isinstance(j, dict) else j)
        return None
    if j.get("queryYear") is not None and str(j.get("queryYear")) != str(year):
        logger.warning("%s: queryYear %r mismatch", label, j.get("queryYear"))
        return None
    rows = j.get("data")
    if not isinstance(rows, list) or not rows:
        logger.warning("%s: empty table (year not published yet?)", label)
        return None
    closed = set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            logger.warning("%s: malformed row %r", label, row)
            return None
        try:
            d = datetime.datetime.strptime(str(row[0]).strip(), "%Y-%m-%d").date()
        except ValueError:
            logger.warning("%s: bad date %r", label, row[0])
            return None
        if d.year != year:
            continue
        if _TRADING_DAY_NAME_RE.search(str(row[1]).strip()):
            continue  # e.g. 農曆春節前最後交易日: listed in the table but the market is open
        closed.add(d)
    if sum(1 for d in closed if d.weekday() < 5) < CALENDAR_MIN_CLOSED_WEEKDAYS:
        logger.warning("%s: only %d weekday closures, table looks incomplete", label,
                       sum(1 for d in closed if d.weekday() < 5))
        return None
    days = set()
    d = datetime.date(year, 1, 1)
    one = datetime.timedelta(days=1)
    while d.year == year:
        if d.weekday() < 5 and d not in closed:
            days.add(d)
        d += one
    return days
