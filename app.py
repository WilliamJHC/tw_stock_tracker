# -*- coding: utf-8 -*-
"""FastAPI backend: serves the UI and per-stock analysis JSON.

Run:  .venv\\Scripts\\python -m uvicorn app:app --port 8932
"""
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import analysis as an
import data_sources as ds
import pipeline as pl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="台股追蹤器 v2")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/api/watchlist")
def api_watchlist():
    return {"watchlist": pl.load_watchlist(), "tick_tier": ds.tick_tier_available()}


@app.post("/api/watchlist/{code}")
def api_add(code: str, name: str = ""):
    code = code.strip().upper()
    wl = pl.load_watchlist()
    if any(s["code"] == code for s in wl):
        raise HTTPException(409, "已在追蹤清單中")
    wl.append({"code": code, "name": name.strip()})
    pl.save_watchlist(wl)
    return {"ok": True}


@app.delete("/api/watchlist/{code}")
def api_remove(code: str):
    wl = [s for s in pl.load_watchlist() if s["code"] != code.strip().upper()]
    pl.save_watchlist(wl)
    return {"ok": True}


@app.get("/api/analysis/{code}")
def api_analysis(code: str):
    code = code.strip().upper()
    try:
        return pl.build_stock_payload(code)
    except ds.SourceError as e:
        raise HTTPException(404, f"抓不到 {code} 的日線資料：{e}")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/config")
def api_config():
    return {
        "score_bands": an.SCORE_BANDS,
        "score_labels": an.SCORE_LABELS,
        "action_thresholds": an.ACTION_THRESHOLDS,
        "chip_weights": an.CHIP_WEIGHTS,
        "data_tier": ds.data_tier(),
    }
