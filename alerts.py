# -*- coding: utf-8 -*-
"""Alert rule engine + Telegram push. Phase 3 of docs/dev_plan_intraday.md.

evaluate_alerts() is a pure function (dict indicators in, alert dicts out),
mirroring the "event trigger + priority" pattern in
analysis.py:459-535 compute_attention() -- it does not reuse that function's
logic, just its shape: a flat list of (rule, points) checks, no shared state.

send_telegram() follows the .env-then-environment-variable loading pattern in
data_sources.py:25-45 _load_token(), duplicated here (not imported) so this
module has zero dependency on data_sources.py's FinMind-specific state.
"""
import datetime as dt
import os

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Thresholds/points centralised here, mirroring intraday.py's
# INTRADAY_THRESHOLDS pattern. All values are product judgment calls, not
# derived -- tune here if real trading throws false positives/negatives.
ALERT_RULES = {
    # realtime_cvd()["cum"] reversal detector: compares the net slope of the
    # last `cvd_window` points against the `cvd_window` points before that;
    # a sign flip between the two windows counts as a reversal. Needs at
    # least 2 * cvd_window points of CVD history before it can fire.
    "cvd_window": 5,
    "priorities": {
        "vwap_breakout": 20,
        "orb_breakout": 20,
        "cvd_reversal": 15,
        "depth_wall": 15,
        "volume_anomaly": 10,
    },
}


# --------------------------------------------------------- rule engine ----

def evaluate_alerts(code, indicators):
    """Pure function: given the current indicator readings for one code,
    return the list of alerts that should fire right now (no dedupe/cooldown
    -- that's dedupe_and_cooldown()'s job).

    indicators: dict, keys are optional (missing/None keys are simply
    skipped, so callers can pass a partial dict when some indicators
    couldn't be computed yet, e.g. too little history for CVD reversal):
        "price": float                         -- latest trade/quote price
        "vwap": intraday.vwap_bands() result
        "orb": intraday.opening_range() result
        "cvd": intraday.realtime_cvd() result
        "depth": intraday.depth_imbalance() result
        "volume": intraday.volume_anomaly() result

    Returns: [{"code": str, "event_type": str, "priority": int, "message": str}, ...]
    """
    alerts = []
    pr = ALERT_RULES["priorities"]

    price = indicators.get("price")
    vwap = indicators.get("vwap")
    if vwap and price is not None and vwap.get("upper2") is not None:
        if price > vwap["upper2"]:
            alerts.append({
                "code": code, "event_type": "vwap_breakout_up", "priority": pr["vwap_breakout"],
                "message": f"[{code}] 價格突破 VWAP+2σ（{price:.2f} > {vwap['upper2']:.2f}）",
            })
        elif price < vwap["lower2"]:
            alerts.append({
                "code": code, "event_type": "vwap_breakout_down", "priority": pr["vwap_breakout"],
                "message": f"[{code}] 價格跌破 VWAP-2σ（{price:.2f} < {vwap['lower2']:.2f}）",
            })

    orb = indicators.get("orb")
    if orb and orb.get("breakout") in ("up", "down"):
        direction = "向上" if orb["breakout"] == "up" else "向下"
        alerts.append({
            "code": code, "event_type": f"orb_breakout_{orb['breakout']}", "priority": pr["orb_breakout"],
            "message": f"[{code}] 開盤區間{direction}突破（區間 {orb['low']:.2f}~{orb['high']:.2f}）",
        })

    cvd = indicators.get("cvd")
    if cvd and cvd.get("cum"):
        reversal = _detect_cvd_reversal(cvd["cum"])
        if reversal:
            label = "轉強（轉買）" if reversal == "up" else "轉弱（轉賣）"
            est = "（估算）" if cvd.get("is_estimated") else ""
            alerts.append({
                "code": code, "event_type": f"cvd_reversal_{reversal}", "priority": pr["cvd_reversal"],
                "message": f"[{code}] CVD 急速{label}{est}",
            })

    depth = indicators.get("depth")
    if depth and depth.get("imbalance_side") in ("bid_heavy", "ask_heavy") and depth.get("walls"):
        side_label = "買盤" if depth["imbalance_side"] == "bid_heavy" else "賣盤"
        alerts.append({
            "code": code, "event_type": f"depth_wall_{depth['imbalance_side']}", "priority": pr["depth_wall"],
            "message": f"[{code}] 五檔失衡且有掛牆：{side_label}偏重（失衡比 {depth['imbalance_ratio']:.2f}）",
        })

    volume = indicators.get("volume")
    if volume and volume.get("is_anomaly"):
        alerts.append({
            "code": code, "event_type": "volume_anomaly", "priority": pr["volume_anomaly"],
            "message": f"[{code}] 量能異常（現量/基準 = {volume['ratio']:.2f}x）",
        })

    return alerts


def _detect_cvd_reversal(cum, window=None):
    """Pure helper: split the tail of `cum` into two adjacent windows and
    flag a reversal when their net slopes have opposite signs. Returns
    "up"/"down" (the new direction) or None."""
    window = window or ALERT_RULES["cvd_window"]
    if len(cum) < 2 * window:
        return None
    recent = cum[-window:]
    prior = cum[-2 * window:-window]
    recent_slope = recent[-1] - recent[0]
    prior_slope = prior[-1] - prior[0]
    if recent_slope == 0 or prior_slope == 0:
        return None
    if (recent_slope > 0) != (prior_slope > 0):
        return "up" if recent_slope > 0 else "down"
    return None


# ------------------------------------------------------- dedupe/cooldown ----

def dedupe_and_cooldown(alerts, state, cooldown_minutes=5, now=None):
    """Filters `alerts` down to the ones allowed to fire, given `state`
    (a dict of {(code, event_type): last_sent_datetime} the caller owns and
    persists across calls). Mutates `state` in place for every alert that
    passes. `now` is injectable for tests; defaults to datetime.now()."""
    now = now or dt.datetime.now()
    cooldown = dt.timedelta(minutes=cooldown_minutes)
    passed = []
    for alert in alerts:
        key = (alert["code"], alert["event_type"])
        last = state.get(key)
        if last is None or (now - last) >= cooldown:
            passed.append(alert)
            state[key] = now
    return passed


# -------------------------------------------------------------- Telegram ----

def _load_env(key):
    """.env-then-environment-variable loader, same convention as
    data_sources.py:25-45 _load_token() but generalised to any key name."""
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    path = os.path.join(BASE_DIR, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() != key:
                    continue
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                return v
    except OSError:
        pass
    return ""


def send_telegram(message):
    """Sends `message` via the Telegram Bot API. Reads TELEGRAM_BOT_TOKEN /
    TELEGRAM_CHAT_ID (env var first, then .env). Never raises: missing
    credentials degrade to a skipped no-op (Phase 0 user to-do not yet
    done), and HTTP-level problems are returned in the result dict rather
    than propagated, so a bad Telegram response never kills monitor.py's
    loop.
    """
    token = _load_env("TELEGRAM_BOT_TOKEN")
    chat_id = _load_env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[alerts] Telegram 未設定，警報未送出：" + message)
        return {"skipped": True, "reason": "telegram not configured"}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return {
        "skipped": False,
        "ok": bool(body.get("ok")),
        "status_code": resp.status_code,
        "response": body,
    }
