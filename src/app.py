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
import db
import pipeline as pl

# Data files (cache/, static/, watchlist.json, .env) live at the repo root,
# one level up from src/.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(title="台股追蹤器 v3.0")
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


@app.get("/api/suggestions")
def api_suggestions():
    """Backtest-backed confidence ranking for the watchlist (docs/backtest_plan.md
    Phase 5). Degrades gracefully (confidence=None, last_run_at=None) if
    run_backtest.py hasn't been run yet -- see pl.build_suggestions()."""
    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)
    last_run_at = db.get_meta(conn, "last_run_at")
    conn.close()
    return {"suggestions": pl.build_suggestions(), "last_run_at": last_run_at}


@app.get("/api/backtest/{code}")
def api_backtest(code: str):
    """Full backtest detail for one code: signal win-rate table (11 signals
    x 3 horizons), strategy summary, and trade log. Empty/None fields (not
    a 404) if run_backtest.py hasn't covered this code yet -- same
    graceful-degradation stance as /api/suggestions."""
    code = code.strip().upper()
    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)
    strategy_run = db.query_strategy_run(conn, code)
    trades = db.query_trades(conn, strategy_run["run_id"], code) if strategy_run else []
    result = {
        "code": code,
        "signal_stats": db.query_signal_stats(conn, code),
        "strategy": strategy_run,
        "trades": trades,
        "data_as_of": db.get_meta(conn, "last_run_at"),
    }
    conn.close()
    return result


@app.get("/api/recommendations")
def api_recommendations():
    """All-market top-N recommendation cards (docs/archive/dev_plan_recommendations.md
    Phase 1/2). Degrades gracefully ({"recommendations": [], "as_of": None},
    not a 404) when scripts/run_daily_recommendations.py hasn't run yet --
    same stance as /api/suggestions. See pl.build_recommendations() for the
    shared logic (also used by snapshot.py's static-mode build)."""
    return pl.build_recommendations()


@app.get("/api/pullback")
def api_pullback():
    """強勢股回調買法 watchlist (the second selection method, see
    docs/pullback_strategy.md): meta.pullback_watchlist from
    scripts/run_pullback_scan.py plus live quotes. Degrades to an empty list
    before the scan has run, same stance as /api/recommendations."""
    return pl.build_pullback_watchlist()


@app.get("/api/config")
def api_config():
    return {
        "score_bands": an.SCORE_BANDS,
        "score_labels": an.SCORE_LABELS,
        "action_thresholds": an.ACTION_THRESHOLDS,
        "chip_weights": an.CHIP_WEIGHTS,
        "data_tier": ds.data_tier(),
    }
