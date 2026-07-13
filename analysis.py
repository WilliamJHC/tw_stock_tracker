# -*- coding: utf-8 -*-
"""Indicator engine. Pure functions only: arrays in, JSON-ready dicts out.

Ported from the verified JS engine of legacy/taiwan_stock_tracker.html (2026-07-07),
with RSI removed and Volume Profile / Order Flow added per v2 requirements.
# deferred: monte_carlo / knn / pe_range (verified JS implementations exist in
# legacy/taiwan_stock_tracker.html and can be translated when needed)
"""
import datetime as dt
import math

# Composite score weights (sum of magnitudes = 100). Tune to taste:
#   maPosition  price vs MA5/20/60 (trend location)
#   maAlign     bull/bear alignment of MA5>MA20>MA60
#   volumePrice volume-price quadrant (daily)
#   trend       20-day regression slope
#   vpPosition  price vs Volume Profile value area (acceptance/rejection)
#   orderFlow   recent aggressive buy/sell net delta
SCORE_WEIGHTS = {
    "maPosition": 25, "maAlign": 10, "volumePrice": 15,
    "trend": 15, "vpPosition": 15, "orderFlow": 20,
}

QUADRANT_INFO = {
    "PU_VU": {"label": "價漲量增", "desc": "多方動能健康", "bias": 1, "score": 1},
    "PU_VD": {"label": "價漲量縮", "desc": "上漲但量能未跟上，追高留意", "bias": 0, "score": 0.25},
    "PD_VU": {"label": "價跌量增", "desc": "賣壓沉重", "bias": -1, "score": -1},
    "PD_VD": {"label": "價跌量縮", "desc": "下跌但賣壓漸輕", "bias": 0, "score": -0.25},
}


# ------------------------------------------------------ basic series ----

def ma_series(values, n):
    out = [None] * len(values)
    s = 0.0
    for i, x in enumerate(values):
        s += x
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def stdev(arr):
    if len(arr) < 2:
        return None
    m = sum(arr) / len(arr)
    return math.sqrt(sum((x - m) ** 2 for x in arr) / (len(arr) - 1))


def linreg_slope(values):
    n = len(values)
    if n < 2:
        return None
    sx = sum(range(n))
    sy = sum(values)
    sxy = sum(i * x for i, x in enumerate(values))
    sxx = sum(i * i for i in range(n))
    return (n * sxy - sx * sy) / (n * sxx - sx * sx)


def quadrant_at(closes, vols, vol20, i):
    """Volume-price quadrant: 3-day price direction x 3-day volume vs MA20."""
    if i < 20 or not vol20[i]:
        return None
    price_up = closes[i] >= closes[i - 3]
    v3 = (vols[i] + vols[i - 1] + vols[i - 2]) / 3
    vol_up = v3 >= vol20[i]
    return ("PU" if price_up else "PD") + "_" + ("VU" if vol_up else "VD")


def fractal_pivots(highs, lows, closes, lookback=60, span=2):
    n = len(closes)
    start = max(span, n - lookback)
    res, sup = [], []
    for i in range(start, n - span):
        is_high = all(highs[i] >= highs[i - j] and highs[i] >= highs[i + j] for j in range(1, span + 1))
        is_low = all(lows[i] <= lows[i - j] and lows[i] <= lows[i + j] for j in range(1, span + 1))
        if is_high:
            res.append(highs[i])
        if is_low:
            sup.append(lows[i])
    price = closes[-1]
    return {
        "resistances": sorted({p for p in res if p > price})[:2],
        "supports": sorted({p for p in sup if p < price}, reverse=True)[:2],
    }


def trend_estimate(closes, window=20, horizon=20):
    """20-day regression trend + statistical band (drift +/- 1.5 sigma sqrt(t))."""
    n = len(closes)
    if n < window + 1:
        return None
    seg = closes[-window:]
    slope = linreg_slope(seg)
    price = closes[-1]
    slope_pct = slope / price
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(n - window, n)]
    sigma = stdev(rets)
    if sigma is None:
        return None
    drift = slope_pct * horizon
    band = 1.5 * sigma * math.sqrt(horizon)
    return {
        "slope_pct_per_day": slope_pct * 100,
        "est": {"low": price * (1 + drift - band), "mid": price * (1 + drift), "high": price * (1 + drift + band)},
    }


# ---------------------------------------------------- volume profile ----

def volume_profile(highs, lows, vols, price, lookback=120, n_bins=50, va_pct=0.70):
    """Distribute each bar's volume uniformly across its H-L range into bins.

    Works on daily bars (default) or minute bars (pass minute arrays).
    Returns bins, POC, 70% value area, HVN/LVN lists.
    """
    n = len(vols)
    if n == 0:
        return None
    s = max(0, n - lookback)
    hs, ls, vs = highs[s:], lows[s:], vols[s:]
    pmin, pmax = min(ls), max(hs)
    if pmax <= pmin:
        return None
    width = (pmax - pmin) / n_bins
    bins = [0.0] * n_bins
    for h, l, v in zip(hs, ls, vs):
        if v <= 0:
            continue
        if h <= l:
            idx = min(n_bins - 1, int((l - pmin) / width))
            bins[idx] += v
            continue
        # uniform distribution across [l, h]
        lo = (l - pmin) / width
        hi = (h - pmin) / width
        i0, i1 = int(lo), min(n_bins - 1, int(hi))
        span = hi - lo
        for b in range(i0, i1 + 1):
            seg_lo = max(lo, b)
            seg_hi = min(hi, b + 1)
            if seg_hi > seg_lo:
                bins[b] += v * (seg_hi - seg_lo) / span
    total = sum(bins)
    if total <= 0:
        return None
    center = lambda b: pmin + (b + 0.5) * width

    poc_idx = max(range(n_bins), key=lambda b: bins[b])
    # value area: expand from POC, absorbing the larger neighbour first
    included = {poc_idx}
    acc = bins[poc_idx]
    lo_b, hi_b = poc_idx, poc_idx
    while acc < va_pct * total and (lo_b > 0 or hi_b < n_bins - 1):
        below = bins[lo_b - 1] if lo_b > 0 else -1.0
        above = bins[hi_b + 1] if hi_b < n_bins - 1 else -1.0
        if above >= below:
            hi_b += 1
            acc += bins[hi_b]
            included.add(hi_b)
        else:
            lo_b -= 1
            acc += bins[lo_b]
            included.add(lo_b)
    vah = pmin + (hi_b + 1) * width
    val = pmin + lo_b * width

    # HVN: local maxima (top 3 by volume). LVN: local minima below 30% of POC.
    hvn, lvn = [], []
    for b in range(1, n_bins - 1):
        if bins[b] >= bins[b - 1] and bins[b] >= bins[b + 1] and bins[b] > 0:
            hvn.append((bins[b], center(b)))
        if bins[b] <= bins[b - 1] and bins[b] <= bins[b + 1] and bins[b] < 0.3 * bins[poc_idx]:
            lvn.append((abs(center(b) - price), center(b)))
    hvn = [p for _, p in sorted(hvn, reverse=True)[:3]]
    lvn = [p for _, p in sorted(lvn)[:2]]  # nearest to current price

    return {
        "bins": [{"p": center(b), "v": bins[b]} for b in range(n_bins)],
        "poc": center(poc_idx), "vah": vah, "val": val,
        "hvn": hvn, "lvn": lvn,
        "bar_count": len(vs), "bin_width": width,
    }


# -------------------------------------------------------- order flow ----

def of_from_minutes(mhist, tz_offset_hours=8):
    """Estimate daily aggressive-buy/sell delta from 5m bars via the tick rule.

    Tick rule: bar closing higher than previous bar close -> buyer-initiated,
    lower -> seller-initiated, unchanged -> carry previous sign. This is an
    ESTIMATE, not true bid/ask-side classification.
    Returns [{date, delta, volume}], oldest first.
    """
    t, c, v = mhist["t"], mhist["c"], mhist["v"]
    days = {}
    sign = 0
    for i in range(len(c)):
        if i > 0:
            if c[i] > c[i - 1]:
                sign = 1
            elif c[i] < c[i - 1]:
                sign = -1
        if sign == 0:
            continue
        d = (dt.datetime.fromtimestamp(t[i], dt.timezone.utc) + dt.timedelta(hours=tz_offset_hours)).date().isoformat()
        slot = days.setdefault(d, {"date": d, "delta": 0, "volume": 0})
        slot["delta"] += sign * v[i]
        slot["volume"] += v[i]
    return [days[d] for d in sorted(days)]


def of_from_ticks(tick_days):
    """Exact daily delta from FinMind tick rows. tick_days: {date: rows}.

    TickType: 1 = 內盤(成交在買價，主動賣), 2 = 外盤(成交在賣價，主動買).
    """
    out = []
    for date in sorted(tick_days):
        delta = vol = 0
        for r in tick_days[date]:
            q = int(r.get("volume") or 0)
            tt = r.get("TickType")
            vol += q
            if tt == 2:
                delta += q
            elif tt == 1:
                delta -= q
        out.append({"date": date, "delta": delta, "volume": vol})
    return out


def of_summary(of_days, closes_by_date=None):
    """Cumulative delta + recent stats + divergence check."""
    if not of_days:
        return None
    cum = []
    acc = 0
    for d in of_days:
        acc += d["delta"]
        cum.append(acc)
    recent = of_days[-5:]
    vol5 = sum(d["volume"] for d in recent)
    delta5 = sum(d["delta"] for d in recent)
    ratio5 = delta5 / vol5 if vol5 else 0.0
    # streak of same-sign delta days (positive = consecutive net-buy days)
    streak = 0
    for d in reversed(of_days):
        s = 1 if d["delta"] > 0 else -1 if d["delta"] < 0 else 0
        if s == 0 or (streak != 0 and (s > 0) != (streak > 0)):
            break
        streak += s
    # divergence over last 10 OF days: price direction vs delta sum direction
    divergence = None
    if closes_by_date and len(of_days) >= 10:
        win = of_days[-10:]
        p0 = closes_by_date.get(win[0]["date"])
        p1 = closes_by_date.get(win[-1]["date"])
        dsum = sum(d["delta"] for d in win)
        vsum = sum(d["volume"] for d in win)
        if p0 and p1 and vsum:
            pchg = (p1 - p0) / p0
            dr = dsum / vsum
            if pchg > 0.02 and dr < -0.05:
                divergence = "bearish"   # 價漲但主動賣盤佔優 -> 出貨嫌疑
            elif pchg < -0.02 and dr > 0.05:
                divergence = "bullish"   # 價跌但主動買盤佔優 -> 吸籌嫌疑
    return {
        "days": of_days, "cum": cum,
        "delta5": delta5, "vol5": vol5, "ratio5": ratio5,
        "streak": streak, "divergence": divergence,
    }


# ----------------------------------------------------------- backtest ----

# Signal predicates evaluated at historical daily index i (for win-rate backtest).
# Only signals with cheap daily-series history are backtestable; VP/OF signals
# are not (their data window is too short / recomputation too heavy) — the UI
# marks those as 回測不適用.
BACKTEST_SIGNALS = {
    "價漲量增": lambda c, i: c["quadrant"][i] == "PU_VU",
    "價漲量縮": lambda c, i: c["quadrant"][i] == "PU_VD",
    "價跌量增": lambda c, i: c["quadrant"][i] == "PD_VU",
    "價跌量縮": lambda c, i: c["quadrant"][i] == "PD_VD",
    "突破站上MA60": lambda c, i: i > 0 and c["ma60"][i] is not None and c["ma60"][i - 1] is not None
        and c["closes"][i] >= c["ma60"][i] and c["closes"][i - 1] < c["ma60"][i - 1],
    "跌破MA60": lambda c, i: i > 0 and c["ma60"][i] is not None and c["ma60"][i - 1] is not None
        and c["closes"][i] < c["ma60"][i] and c["closes"][i - 1] >= c["ma60"][i - 1],
    "均線多頭排列": lambda c, i: c["ma5"][i] is not None and c["ma60"][i] is not None
        and c["ma5"][i] > c["ma20"][i] > c["ma60"][i],
    "均線空頭排列": lambda c, i: c["ma5"][i] is not None and c["ma60"][i] is not None
        and c["ma5"][i] < c["ma20"][i] < c["ma60"][i],
    "爆量(>2倍均量)": lambda c, i: c["vol20"][i] and c["vols"][i] > 2 * c["vol20"][i],
}


def backtest_signal(ctx, fn, horizon=20, gap=5):
    """Win rate of a signal over history: forward `horizon`-day return,
    sampling at most once per `gap` days to avoid overlapping samples."""
    n = len(ctx["closes"])
    hits = []
    last = -10 ** 9
    for i in range(n - horizon):
        if not fn(ctx, i):
            continue
        if i - last < gap:
            continue
        last = i
        hits.append(ctx["closes"][i + horizon] / ctx["closes"][i] - 1)
    if not hits:
        return {"n": 0}
    wins = sum(1 for r in hits if r > 0)
    return {"n": len(hits), "wins": wins,
            "win_rate": wins / len(hits) * 100,
            "avg_ret": sum(hits) / len(hits) * 100}


# ------------------------------------------------------------ analyze ----

def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def analyze(daily, minute=None, tick_days=None, inst=None):
    """Full analysis of one stock.

    daily: {t,o,h,l,c,v} 2y daily bars (shares).
    minute: optional 5m bars for OF estimation.
    tick_days: optional {date: tick_rows} for exact OF (paid tier).
    inst: optional institutional rows [{date, foreign, trust}].
    """
    closes, highs, lows, vols, times = daily["c"], daily["h"], daily["l"], daily["v"], daily["t"]
    n = len(closes)
    if n < 5:
        return None
    ma5 = ma_series(closes, 5)
    ma20 = ma_series(closes, 20)
    ma60 = ma_series(closes, 60)
    vol20 = ma_series(vols, 20)
    quadrant = [quadrant_at(closes, vols, vol20, i) for i in range(n)]
    ctx = {"closes": closes, "vols": vols, "ma5": ma5, "ma20": ma20,
           "ma60": ma60, "vol20": vol20, "quadrant": quadrant}
    last = n - 1
    price = closes[last]
    w = SCORE_WEIGHTS
    signals = []
    score = 0.0

    # ---- MA position / alignment ----
    ma_pos, ma_cnt = 0, 0
    for m in (ma5, ma20, ma60):
        if m[last] is not None:
            ma_cnt += 1
            ma_pos += 1 if price >= m[last] else -1
    if ma_cnt:
        score += ma_pos / ma_cnt * w["maPosition"]
    aligned = None
    if ma5[last] is not None and ma20[last] is not None and ma60[last] is not None:
        if ma5[last] > ma20[last] > ma60[last]:
            aligned = 1
            signals.append({"text": "均線多頭排列（MA5 > MA20 > MA60）", "bias": 1, "bt": "均線多頭排列"})
        elif ma5[last] < ma20[last] < ma60[last]:
            aligned = -1
            signals.append({"text": "均線空頭排列（MA5 < MA20 < MA60）", "bias": -1, "bt": "均線空頭排列"})
        score += (aligned or 0) * w["maAlign"]
    if BACKTEST_SIGNALS["突破站上MA60"](ctx, last):
        signals.append({"text": "今日突破站上 60 日均線", "bias": 1, "bt": "突破站上MA60"})
    if BACKTEST_SIGNALS["跌破MA60"](ctx, last):
        signals.append({"text": "今日跌破 60 日均線", "bias": -1, "bt": "跌破MA60"})

    # ---- volume-price quadrant ----
    q = quadrant[last]
    if q:
        info = QUADRANT_INFO[q]
        signals.append({"text": f"{info['label']}：{info['desc']}", "bias": info["bias"], "bt": info["label"]})
        score += info["score"] * w["volumePrice"]
    if BACKTEST_SIGNALS["爆量(>2倍均量)"](ctx, last):
        signals.append({"text": f"爆量：今日量為 20 日均量 {vols[last] / vol20[last]:.1f} 倍",
                        "bias": 0, "bt": "爆量(>2倍均量)"})

    # ---- trend ----
    trend = trend_estimate(closes)
    if trend:
        score += _clamp(trend["slope_pct_per_day"] * 20 / 10) * w["trend"]

    # ---- volume profile (daily 120d; minute bars if available for finer bins) ----
    vp = volume_profile(highs, lows, vols, price)
    vp_src = "daily-120d"
    if minute and minute.get("c"):
        vp_min = volume_profile(minute["h"], minute["l"], minute["v"], price,
                                lookback=len(minute["v"]))
        if vp_min:
            vp, vp_src = vp_min, "minute-60d"
    vp_position = None
    if vp:
        if price > vp["vah"]:
            vp_position = "above_va"
            score += w["vpPosition"]
            signals.append({"text": f"現價高於 Value Area（VAH {vp['vah']:.1f}）：多方接受、價值區上移", "bias": 1, "bt": None})
        elif price < vp["val"]:
            vp_position = "below_va"
            score -= w["vpPosition"]
            signals.append({"text": f"現價低於 Value Area（VAL {vp['val']:.1f}）：空方主導、價值區下移", "bias": -1, "bt": None})
        else:
            vp_position = "inside_va"
            rel = 0.0
            if vp["vah"] > vp["poc"] and price >= vp["poc"]:
                rel = (price - vp["poc"]) / (vp["vah"] - vp["poc"]) * 0.5
            elif vp["poc"] > vp["val"] and price < vp["poc"]:
                rel = -(vp["poc"] - price) / (vp["poc"] - vp["val"]) * 0.5
            score += rel * w["vpPosition"]
            signals.append({"text": f"現價位於 Value Area 內（POC {vp['poc']:.1f}）：籌碼密集區，趨勢待表態", "bias": 0, "bt": None})
        for lv in vp["lvn"]:
            if abs(lv - price) / price < 0.03:
                direction = "上方" if lv > price else "下方"
                signals.append({"text": f"{direction}逼近低量區 LVN {lv:.1f}：成交真空、穿越時速度易加快", "bias": 0, "bt": None})
                break

    # ---- order flow ----
    closes_by_date = {dt.datetime.fromtimestamp(times[i]).date().isoformat(): closes[i] for i in range(n)}
    of = None
    of_mode = None
    if tick_days:
        of = of_summary(of_from_ticks(tick_days), closes_by_date)
        of_mode = "tick"
    elif minute and minute.get("c"):
        of = of_summary(of_from_minutes(minute), closes_by_date)
        of_mode = "estimate"
    if of:
        score += _clamp(of["ratio5"] / 0.15) * w["orderFlow"]
        pct = of["ratio5"] * 100
        side = "買盤" if of["delta5"] >= 0 else "賣盤"
        tag = "逐筆內外盤" if of_mode == "tick" else "分鐘K推估"
        signals.append({"text": f"近5日主動{side}淨額佔總量 {abs(pct):.1f}%（{tag}）",
                        "bias": 1 if of["delta5"] > 0 else -1 if of["delta5"] < 0 else 0, "bt": None})
        if abs(of["streak"]) >= 3:
            side = "買超" if of["streak"] > 0 else "賣超"
            signals.append({"text": f"主動{side}已連續 {abs(of['streak'])} 個交易日",
                            "bias": 1 if of["streak"] > 0 else -1, "bt": None})
        if of["divergence"] == "bearish":
            signals.append({"text": "⚠ 量價背離：近10日價格上漲但主動賣盤佔優（出貨嫌疑）", "bias": -1, "bt": None})
            score -= 10
        elif of["divergence"] == "bullish":
            signals.append({"text": "量價背離：近10日價格下跌但主動買盤佔優（吸籌嫌疑）", "bias": 1, "bt": None})
            score += 10

    # ---- institutional (auxiliary, free FinMind data) ----
    inst_summary = None
    if inst:
        f_streak = 0
        for r in reversed(inst):
            if r["foreign"] > 0 and f_streak >= 0:
                f_streak += 1
            elif r["foreign"] < 0 and f_streak <= 0:
                f_streak -= 1
            else:
                break
        inst_summary = {"foreign_streak": f_streak,
                        "foreign_recent": sum(r["foreign"] for r in inst[-5:]),
                        "trust_recent": sum(r["trust"] for r in inst[-5:])}
        if abs(f_streak) >= 3:
            side = "買超" if f_streak > 0 else "賣超"
            signals.append({"text": f"外資連續{side} {abs(f_streak)} 日（近5日淨{'買' if inst_summary['foreign_recent']>0 else '賣'} {abs(inst_summary['foreign_recent'])//1000:,} 張）",
                            "bias": 1 if f_streak > 0 else -1, "bt": None})

    # ---- finalize ----
    score = round(max(-100.0, min(100.0, score)))
    label = ("強多" if score >= 60 else "偏多" if score >= 20 else
             "中性" if score > -20 else "偏空" if score > -60 else "強空")

    backtest = {}
    for sig in signals:
        key = sig.get("bt")
        if key and key in BACKTEST_SIGNALS and key not in backtest:
            backtest[key] = backtest_signal(ctx, BACKTEST_SIGNALS[key])

    return {
        "price": price,
        "ma5": ma5, "ma20": ma20, "ma60": ma60, "vol20": vol20,
        "quadrant_last": q,
        "sr": fractal_pivots(highs, lows, closes),
        "trend": trend,
        "vp": vp, "vp_source": vp_src if vp else None, "vp_position": vp_position,
        "of": of, "of_mode": of_mode,
        "inst": inst_summary,
        "signals": signals, "backtest": backtest,
        "score": score, "label": label,
    }
