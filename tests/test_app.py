# -*- coding: utf-8 -*-
"""Unit tests for app.py's route handlers. Called as plain functions (FastAPI's
@app.get(...) decorator returns the original function unchanged) rather than
via a TestClient -- avoids adding httpx as a new test-only dependency for a
single thin route (project is stdlib-only outside the live-service data
path, see requirements.txt). The substantive recommendation-building logic
lives in pipeline.build_recommendations() and is tested in test_pipeline.py;
this file only locks that the route delegates to it."""
import app as app_module
import pipeline as pl


def test_api_recommendations_delegates_to_pipeline(monkeypatch):
    sentinel = {"recommendations": [{"code": "2330"}], "as_of": "2026-08-20"}
    monkeypatch.setattr(pl, "build_recommendations", lambda: sentinel)
    assert app_module.api_recommendations() is sentinel


def test_api_pullback_delegates_to_pipeline(monkeypatch):
    sentinel = {"setups": [{"code": "2330"}], "as_of": "2026-10-12"}
    monkeypatch.setattr(pl, "build_pullback_watchlist", lambda: sentinel)
    assert app_module.api_pullback() is sentinel
