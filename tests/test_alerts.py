# -*- coding: utf-8 -*-
"""Unit tests for alerts.py: rule engine, dedupe/cooldown, Telegram push.
No real network calls: requests.post is patched. Mirrors the mock style in
tests/test_data_sources.py (_fake_response(), no live HTTP)."""
import datetime as dt
import os


from unittest.mock import patch

import pytest

import alerts


# ---------------------------------------------------------- evaluate_alerts --

def test_vwap_breakout_up_triggers_above_upper2():
    indicators = {
        "price": 105.0,
        "vwap": {"vwap": 100.0, "upper1": 102.0, "lower1": 98.0, "upper2": 104.0, "lower2": 96.0, "sample_count": 10},
    }
    result = alerts.evaluate_alerts("2330", indicators)
    assert len(result) == 1
    assert result[0]["event_type"] == "vwap_breakout_up"
    assert result[0]["code"] == "2330"
    assert result[0]["priority"] == alerts.ALERT_RULES["priorities"]["vwap_breakout"]


def test_vwap_breakout_down_triggers_below_lower2():
    indicators = {
        "price": 90.0,
        "vwap": {"vwap": 100.0, "upper1": 102.0, "lower1": 98.0, "upper2": 104.0, "lower2": 96.0, "sample_count": 10},
    }
    result = alerts.evaluate_alerts("2330", indicators)
    assert [a["event_type"] for a in result] == ["vwap_breakout_down"]


def test_vwap_no_trigger_within_bands():
    indicators = {
        "price": 101.0,
        "vwap": {"vwap": 100.0, "upper1": 102.0, "lower1": 98.0, "upper2": 104.0, "lower2": 96.0, "sample_count": 10},
    }
    assert alerts.evaluate_alerts("2330", indicators) == []


def test_orb_breakout_up_triggers():
    indicators = {"orb": {"high": 110.0, "low": 105.0, "breakout": "up", "window_minutes": 15}}
    result = alerts.evaluate_alerts("2330", indicators)
    assert [a["event_type"] for a in result] == ["orb_breakout_up"]


def test_orb_no_breakout_no_trigger():
    indicators = {"orb": {"high": 110.0, "low": 105.0, "breakout": None, "window_minutes": 15}}
    assert alerts.evaluate_alerts("2330", indicators) == []


def test_cvd_reversal_detects_sign_flip():
    # prior window (5 pts): 1,2,3,4,5 -> slope +4 ; recent window (5 pts): 4,3,2,1,0 -> slope -4
    cum = [1, 2, 3, 4, 5, 4, 3, 2, 1, 0]
    indicators = {"cvd": {"cum": cum, "delta_total": 0, "is_estimated": True}}
    result = alerts.evaluate_alerts("2330", indicators)
    assert [a["event_type"] for a in result] == ["cvd_reversal_down"]
    assert "估算" in result[0]["message"]


def test_cvd_no_reversal_when_same_direction():
    cum = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    indicators = {"cvd": {"cum": cum, "delta_total": 10, "is_estimated": False}}
    assert alerts.evaluate_alerts("2330", indicators) == []


def test_cvd_insufficient_history_no_trigger():
    indicators = {"cvd": {"cum": [1, 2, 3], "delta_total": 3, "is_estimated": True}}
    assert alerts.evaluate_alerts("2330", indicators) == []


def test_depth_wall_imbalance_triggers_with_wall():
    indicators = {
        "depth": {
            "bid_total": 100, "ask_total": 20, "imbalance_ratio": 0.67,
            "imbalance_side": "bid_heavy",
            "walls": [{"side": "bid", "price": 99.0, "vol": 500}],
        },
    }
    result = alerts.evaluate_alerts("2330", indicators)
    assert [a["event_type"] for a in result] == ["depth_wall_bid_heavy"]


def test_depth_imbalance_without_wall_no_trigger():
    indicators = {
        "depth": {
            "bid_total": 100, "ask_total": 20, "imbalance_ratio": 0.67,
            "imbalance_side": "bid_heavy", "walls": [],
        },
    }
    assert alerts.evaluate_alerts("2330", indicators) == []


def test_depth_balanced_with_wall_no_trigger():
    indicators = {
        "depth": {
            "bid_total": 60, "ask_total": 55, "imbalance_ratio": 0.04,
            "imbalance_side": "balanced",
            "walls": [{"side": "bid", "price": 99.0, "vol": 500}],
        },
    }
    assert alerts.evaluate_alerts("2330", indicators) == []


def test_volume_anomaly_triggers():
    indicators = {"volume": {"current_total": 500, "baseline_avg": 100, "ratio": 5.0, "is_anomaly": True}}
    result = alerts.evaluate_alerts("2330", indicators)
    assert [a["event_type"] for a in result] == ["volume_anomaly"]


def test_volume_no_anomaly_no_trigger():
    indicators = {"volume": {"current_total": 110, "baseline_avg": 100, "ratio": 1.1, "is_anomaly": False}}
    assert alerts.evaluate_alerts("2330", indicators) == []


def test_evaluate_alerts_empty_indicators_returns_empty():
    assert alerts.evaluate_alerts("2330", {}) == []


def test_evaluate_alerts_multiple_rules_can_fire_together():
    indicators = {
        "price": 105.0,
        "vwap": {"vwap": 100.0, "upper1": 102.0, "lower1": 98.0, "upper2": 104.0, "lower2": 96.0, "sample_count": 10},
        "orb": {"high": 110.0, "low": 105.0, "breakout": "up", "window_minutes": 15},
    }
    result = alerts.evaluate_alerts("2330", indicators)
    event_types = {a["event_type"] for a in result}
    assert event_types == {"vwap_breakout_up", "orb_breakout_up"}


# ------------------------------------------------------- dedupe_and_cooldown --

def test_dedupe_blocks_repeat_within_cooldown():
    state = {}
    now = dt.datetime(2026, 7, 20, 10, 0, 0)
    alert = {"code": "2330", "event_type": "vwap_breakout_up", "priority": 20, "message": "x"}
    first = alerts.dedupe_and_cooldown([alert], state, cooldown_minutes=5, now=now)
    second = alerts.dedupe_and_cooldown([alert], state, cooldown_minutes=5, now=now + dt.timedelta(minutes=2))
    assert first == [alert]
    assert second == []


def test_dedupe_allows_after_cooldown_expires():
    state = {}
    now = dt.datetime(2026, 7, 20, 10, 0, 0)
    alert = {"code": "2330", "event_type": "vwap_breakout_up", "priority": 20, "message": "x"}
    alerts.dedupe_and_cooldown([alert], state, cooldown_minutes=5, now=now)
    later = alerts.dedupe_and_cooldown([alert], state, cooldown_minutes=5, now=now + dt.timedelta(minutes=5))
    assert later == [alert]


def test_dedupe_different_event_types_independent():
    state = {}
    now = dt.datetime(2026, 7, 20, 10, 0, 0)
    a1 = {"code": "2330", "event_type": "vwap_breakout_up", "priority": 20, "message": "x"}
    a2 = {"code": "2330", "event_type": "orb_breakout_up", "priority": 20, "message": "y"}
    result = alerts.dedupe_and_cooldown([a1, a2], state, cooldown_minutes=5, now=now)
    assert result == [a1, a2]


def test_dedupe_different_codes_independent():
    state = {}
    now = dt.datetime(2026, 7, 20, 10, 0, 0)
    a1 = {"code": "2330", "event_type": "vwap_breakout_up", "priority": 20, "message": "x"}
    a2 = {"code": "2317", "event_type": "vwap_breakout_up", "priority": 20, "message": "y"}
    alerts.dedupe_and_cooldown([a1], state, cooldown_minutes=5, now=now)
    result = alerts.dedupe_and_cooldown([a2], state, cooldown_minutes=5, now=now)
    assert result == [a2]


# ------------------------------------------------------------ send_telegram --

def test_send_telegram_skips_without_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(alerts, "BASE_DIR", os.path.dirname(os.path.abspath(__file__)))  # no .env here
    with patch("alerts.requests.post", side_effect=AssertionError("must not call network")):
        result = alerts.send_telegram("hello")
    assert result == {"skipped": True, "reason": "telegram not configured"}


def test_send_telegram_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

    with patch("alerts.requests.post", return_value=FakeResponse()) as mock_post:
        result = alerts.send_telegram("hello")

    assert result["skipped"] is False
    assert result["ok"] is True
    assert result["status_code"] == 200
    mock_post.assert_called_once()
    url = mock_post.call_args[0][0]
    assert url == "https://api.telegram.org/botfake-token/sendMessage"
    assert mock_post.call_args[1]["data"] == {"chat_id": "12345", "text": "hello"}


def test_send_telegram_non_200_reports_content(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    class FakeResponse:
        status_code = 400

        def json(self):
            return {"ok": False, "description": "Bad Request: chat not found"}

    with patch("alerts.requests.post", return_value=FakeResponse()):
        result = alerts.send_telegram("hello")

    assert result["skipped"] is False
    assert result["ok"] is False
    assert result["status_code"] == 400
    assert result["response"]["description"] == "Bad Request: chat not found"
