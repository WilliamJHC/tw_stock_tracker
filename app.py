# -*- coding: utf-8 -*-
"""FastAPI backend: serves the UI and per-stock analysis JSON.

Run:  .venv\\Scripts\\python -m uvicorn app:app --port 8932
"""
import datetime as dt
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import analysis as an
import data_sources as ds

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
DEFAULT_WATCHLIST = [
    {"code": "2330", "name": "台積電"},
    {"code": "2317", "name": "鴻海"},
    {"code": "0050", "name": "元大台灣50"},
    {"code": "2454", "name": "聯發科"},
    {"code": "6869", "name": "雲豹能源"},
    {"code": "3443", "name": "創意"},
    {"code": "2881", "name": "富邦金"},
    {"code": "2891", "name": "中信金"},
    {"code": "6446", "name": "藥華藥"},
    {"code": "8046", "name": "南電"},
    {"code": "3653", "name": "健策"},
    {"code": "2313", "name": "華通"},
    {"code": "2327", "name": "國巨"},
]
CHART_BARS = 120      # daily bars sent to the chart
TICK_OF_DAYS = 20     # tick days fetched when paid tier is available

app = FastAPI(title="台股追蹤器 v2")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def load_watchlist():
    try:
        with open(WATCHLIST_PATH, encoding="utf-8") as f:
            wl = json.load(f)
        if isinstance(wl, list):
            return wl
    except (OSError, ValueError):
        pass
    save_watchlist(DEFAULT_WATCHLIST)
    return list(DEFAULT_WATCHLIST)


def save_watchlist(wl):
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(wl, f, ensure_ascii=False, indent=1)


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/api/watchlist")
def api_watchlist():
    return {"watchlist": load_watchlist(), "tick_tier": ds.tick_tier_available()}


@app.post("/api/watchlist/{code}")
def api_add(code: str, name: str = ""):
    code = code.strip().upper()
    wl = load_watchlist()
    if any(s["code"] == code for s in wl):
        raise HTTPException(409, "已在追蹤清單中")
    wl.append({"code": code, "name": name.strip()})
    save_watchlist(wl)
    return {"ok": True}


@app.delete("/api/watchlist/{code}")
def api_remove(code: str):
    wl = [s for s in load_watchlist() if s["code"] != code.strip().upper()]
    save_watchlist(wl)
    return {"ok": True}


def _fetch_tick_days(code, times):
    """Last TICK_OF_DAYS trading days of tick data (paid tier only)."""
    out = {}
    for ts in times[-TICK_OF_DAYS:]:
        date = dt.datetime.fromtimestamp(ts).date().isoformat()
        try:
            rows = ds.get_ticks(code, date)
            if rows:
                out[date] = rows
        except ds.SourceError:
            return None  # tier revoked mid-flight -> fall back to estimate
    return out or None


@app.get("/api/analysis/{code}")
def api_analysis(code: str):
    code = code.strip().upper()
    try:
        daily = ds.get_daily(code)
    except ds.SourceError as e:
        raise HTTPException(404, f"抓不到 {code} 的日線資料：{e}")

    minute = None
    try:
        minute = ds.get_minute(code)
    except Exception:
        minute = None

    tick_days = None
    if ds.tick_tier_available():
        tick_days = _fetch_tick_days(code, daily["t"])

    inst = ds.get_institutional(code)
    margin = ds.get_margin(code)
    shareholding = ds.get_shareholding(code)
    sbl = ds.get_sbl(code)
    result = an.analyze(daily, minute, tick_days, inst,
                        margin=margin, shareholding=shareholding, sbl=sbl)
    if result is None:
        raise HTTPException(422, f"{code} 歷史資料不足，無法分析")

    attention = an.compute_attention(result["score"], result.get("chip"), result)

    quote = ds.get_quote(code)
    pe = ds.get_pe(code)

    # chart payload: trim series to the last CHART_BARS bars
    n = len(daily["c"])
    s = max(0, n - CHART_BARS)
    chart = {
        "t": daily["t"][s:], "o": daily["o"][s:], "h": daily["h"][s:],
        "l": daily["l"][s:], "c": daily["c"][s:], "v": daily["v"][s:],
        "ma5": result["ma5"][s:], "ma20": result["ma20"][s:],
        "ma60": result["ma60"][s:], "vol20": result["vol20"][s:],
    }
    analysis_out = {k: v for k, v in result.items()
                    if k not in ("ma5", "ma20", "ma60", "vol20")}
    lastidx = n - 1
    return {
        "code": code,
        "name": (quote or {}).get("name") or code,
        "quote": quote,
        "pe": pe,
        "daily_source": daily["source"],
        "last_close": daily["c"][lastidx],
        "last_date": dt.datetime.fromtimestamp(daily["t"][lastidx]).date().isoformat(),
        "ma60_last": result["ma60"][lastidx],
        "chart": chart,
        "analysis": analysis_out,
        "attention": attention,
    }


@app.get("/api/config")
def api_config():
    return {
        "score_bands": an.SCORE_BANDS,
        "score_labels": an.SCORE_LABELS,
        "action_thresholds": an.ACTION_THRESHOLDS,
        "chip_weights": an.CHIP_WEIGHTS,
    }
