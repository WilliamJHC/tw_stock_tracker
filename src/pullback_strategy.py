# -*- coding: utf-8 -*-
"""強勢股回調買法（strong-stock pullback）：多因子排名以外的第二種選股方式。

Method (docs/pullback_strategy.md has the full write-up and the mapping from
each rule below to the original four-step description):

  1. 篩強勢股、看整理：rank the liquid universe by 1/3/6-month return, keep
     codes up >30% AND in the top 30% of any window, then require a
     consolidation after the advance -- higher lows, contracting range, price
     back above a flat-or-rising 9/21 EMA. Lower highs + lower lows under a
     falling EMA21 is explicitly NOT this setup.
  2. 標支撐、等訊號：mark EMA9/21/50, prior swing highs/lows, unfilled gaps and
     an AVWAP anchored at the consolidation's stage high; a zone where several
     of these cluster (especially AVWAP + EMA21 + prior high) is the one to
     watch, the FIRST retest after reclaiming it most of all. Entry trigger
     is intraday: dip into the zone, recover, then trade above the previous
     1/5/15-minute bar's high. A slide straight through support with no
     recovery = keep waiting.
  3. 先定停損再算股數：stop at the day's low / trigger-bar low / prior-bar low,
     ideally <= 2.5-3% away; shares = (account x 0.5%) / (entry - stop).
     Taiwan adjustment: both the planned stop and the intraday stop are floored
     at stop_atr_mult (0.5) x ATR14 below the entry -- a tight confluence zone
     or a 5-minute bar is narrower than a normal day's range, and the 0.585%
     round-trip cost would eat most of the R (see atr_floor_stop()).
  4. 分批出場：take partial profits near 3R/5R, trail the rest with EMA9; trim
     into a sharp spike far above the moving averages.

Deliberately independent of finlab_factors.composite_scores() (multi-factor
ranking), analysis.analyze() and breakout_strategy.py -- same 2026-09-01
"a new strategy stays fully independent" architecture decision
breakout_strategy.py's docstring records. Nothing here feeds
optimize.py/auto-apply (ADR 0002); this method is NOT backtested yet
(docs/pullback_strategy.md "已知限制").

Pure functions, stdlib only, no I/O -- same split as finlab_factors.py /
backtest.py. scripts/run_pullback_scan.py does the fetching and writes
meta.pullback_watchlist; monitor.py evaluates the intraday trigger.

Bars: ascending list of {"date", "o", "h", "l", "c", "v"} dicts. Price
levels should be split/dividend back-adjusted ON TODAY'S SCALE (adjust.py's
convention: most recent factor == 1.0), so every support/stop/target this
module returns is directly usable as an order price. Volume is raw shares.
"""
import math

import analysis as an
import finlab_factors as ff

PULLBACK_PARAMS = {
    # ---- 第一步：篩強勢股 ----
    "return_windows": {"1m": 21, "3m": 63, "6m": 126},  # trading bars ~ 1/3/6 calendar months
    "min_return_pct": 30.0,       # 「優先看漲幅 >30%」
    "top_rank_pct": 0.30,         # 「排名前 20%-30%」-> the looser end; a code qualifies on ANY window
    "min_turnover": 50_000_000,   # NTD, 20-bar average raw close*volume. 「剔除成交稀少」: deliberately
                                  # 5x finlab_factors.LIQUIDITY_MIN_TURNOVER since entries are timed on
                                  # 1/5-minute bars and need a tradable intraday book
    "turnover_window": 20,
    # ---- 第一步：看整理 ----
    "base_lookback": 60,          # look for the advance's stage high within the last N bars
    "min_base_bars": 5,           # high must be at least this old -- otherwise still running, not consolidating
    "max_base_depth_pct": 30.0,   # pullback from stage high deeper than this = broken trend, not a base
    "pivot_span": 2,              # swing high/low = extreme vs 2 bars either side (analysis.fractal_pivots' span)
    "contraction_recent_bars": 5,
    "contraction_ratio_max": 0.85,  # recent 5-bar avg range / avg range over the base's first half
    "ema_fast": 9,
    "ema_mid": 21,
    "ema_slow": 50,
    "ema_slope_bars": 5,
    "ema_flat_tol_pct": -0.5,     # EMA change over ema_slope_bars >= this counts as 「走平或向上」
    # ---- 第二步：支撐區 ----
    "level_lookback": 120,        # bars searched for prior swing highs/lows and unfilled gaps
    "max_level_distance_pct": 12.0,  # ignore support levels further than this below the close
    "cluster_pct": 1.5,           # levels within this % of each other form one zone
    "near_support_pct": 3.0,      # close within this % above the zone top = 「接近支撐」
    "break_tolerance_pct": 1.0,   # close more than this % below the zone bottom = 跌破
    "retest_lookback": 20,        # reclaim (close crossing above the zone) must be within N bars for 「第一次回測」
    # ---- 第三步：停損與部位 ----
    "stop_buffer_ticks": 1,       # zone stop = zone bottom minus this many ticks (the plan then takes the farther of this and the ATR floor)
    "stop_atr_mult": 0.5,         # planned stop is at least this many ATR14 below the entry reference (0 = zone stop only)
    "max_stop_pct": 3.0,          # 「多數交易停損距離在 2.5%-3% 以內」
    "risk_pct": 0.5,              # 「單筆計畫帳戶風險約 0.5%」
    "lot_size": 1000,             # TW board lot; odd lots (零股) allowed via position_size(odd_lot=True)
    # ---- 第四步：分批出場 ----
    "r_targets": (3, 5),          # 「常在 3R 或 5R 附近賣出一部分」
    "trail_ema": 9,
    "extension_atr_mult": 3.0,    # close > EMA9 + N*ATR14 = 「急拉、明顯遠離均線」
    # ---- 台股交易成本（for net-R display only; gross R is the method's own definition） ----
    "fee_rate": 0.001425,         # 手續費 each side, before broker discount
    "fee_discount": 1.0,          # e.g. 0.28 for a 2.8折 account
    "tax_rate": 0.003,            # 證交稅, sell side only (0.0015 for 當沖)
    # ---- 盤中觸發 ----
    "intraday_bar_minutes": 5,
    "touch_tolerance_pct": 0.3,   # price within this % above the zone top counts as having 「下探」 into it
}

LEVEL_LABELS = {
    "ema9": "EMA9", "ema21": "EMA21", "ema50": "EMA50", "avwap": "AVWAP",
    "prior_high": "前高", "prior_low": "前低", "gap": "未補缺口",
}
# 「如果 AVWAP、21 日 EMA 和前期高點落在相近位置，這個區域就值得重點盯住」
KEY_LEVEL_KINDS = frozenset({"avwap", "ema21", "prior_high"})

STATUS_LABELS = {
    "near_support": "接近支撐・盤中盯分鐘圖",
    "extended": "離支撐尚遠・等回測",
    "broken": "跌破支撐・繼續等待",
}


# ------------------------------------------------------------ 台股價位 ----

def tw_tick_size(price):
    """TWSE/TPEX stock tick size (升降單位) for `price`."""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def round_to_tick(price, mode="nearest"):
    """Snap `price` onto the TW tick grid. mode: "down" (stops, supports),
    "up" (buy triggers), "nearest" (targets). None passes through."""
    if price is None:
        return None
    tick = tw_tick_size(price)
    steps = price / tick
    if mode == "down":
        n = math.floor(steps + 1e-9)
    elif mode == "up":
        n = math.ceil(steps - 1e-9)
    else:
        n = round(steps)
    out = n * tick
    # Crossing a tick-size boundary (e.g. 49.99 -> 50) can leave a price that
    # isn't on the new grid; one re-snap with the new tick fixes it.
    tick2 = tw_tick_size(out)
    if tick2 != tick:
        out = round(out / tick2) * tick2
    return round(out, 2)


# ---------------------------------------------------------- indicators ----

def ema_series(values, n):
    """Exponential moving average, seeded with the simple mean of the first
    `n` values (None before that). Same length as `values`."""
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def swing_points(highs, lows, span=2, start=0):
    """[(idx, price)] swing highs and swing lows from `start` on: bar i is a
    swing high if its high >= the `span` bars on each side (same rule as
    analysis.fractal_pivots(), but returning indices so callers can reason
    about order). The last `span` bars can't be confirmed yet and are never
    pivots."""
    n = len(highs)
    sh, sl = [], []
    for i in range(max(span, start), n - span):
        if all(highs[i] >= highs[i - j] and highs[i] >= highs[i + j] for j in range(1, span + 1)):
            sh.append((i, highs[i]))
        if all(lows[i] <= lows[i - j] and lows[i] <= lows[i + j] for j in range(1, span + 1)):
            sl.append((i, lows[i]))
    return sh, sl


def anchored_vwap(bars, anchor_idx):
    """Volume-weighted average of typical price (h+l+c)/3 from bars[anchor_idx]
    through the last bar, or None if there's no volume in that span."""
    num = den = 0.0
    for b in bars[anchor_idx:]:
        v = b.get("v") or 0
        num += (b["h"] + b["l"] + b["c"]) / 3 * v
        den += v
    return num / den if den else None


def unfilled_gaps(bars, start=1):
    """Unfilled up-gaps: bar i opened a window above bar i-1 (low[i] >
    high[i-1]) and no later bar's low has traded back down to high[i-1].
    Returns [{"idx", "bottom": high[i-1], "top": low[i]}], oldest first."""
    out = []
    n = len(bars)
    # suffix minimum of lows lets each gap's fill check be O(1)
    suffix_min = [math.inf] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_min[i] = min(bars[i]["l"], suffix_min[i + 1])
    for i in range(max(1, start), n):
        bottom, top = bars[i - 1]["h"], bars[i]["l"]
        if top > bottom and suffix_min[i + 1] > bottom:
            out.append({"idx": i, "bottom": bottom, "top": top})
    return out


# ------------------------------------------------- 第一步：強勢股篩選 ----

def _adj_close(bar):
    return ff._close(bar)


def strength_screen(universe_data, params=None):
    """Step 1 over the close-only factor cache (scripts/report_factor_ic.
    load_cached_universe() shape: {code: {"bars": [{date, c, v, c_adj?}]}}),
    each code evaluated at its own latest bar.

    Liquidity uses raw close*volume (adjust.py's rule); returns use c_adj.
    Each window ranks only the liquid codes that have that window's history.
    Returns {code: {"returns": {win: pct|None}, "ranks": {win: pct|None},
    "qualifying": [win, ...], "strength": best qualifying rank}} for every
    code that qualifies on at least one window."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    windows = p["return_windows"]
    liquid_returns = {w: {} for w in windows}
    all_returns = {}
    for code, d in universe_data.items():
        bars = d["bars"]
        if not bars:
            continue
        idx = len(bars) - 1
        if not ff.liquidity_ok(bars, idx, min_turnover=p["min_turnover"], window=p["turnover_window"]):
            continue
        rets = {}
        for w, n in windows.items():
            if idx - n < 0:
                rets[w] = None
                continue
            base = _adj_close(bars[idx - n])
            rets[w] = (_adj_close(bars[idx]) - base) / base * 100 if base else None
            if rets[w] is not None:
                liquid_returns[w][code] = rets[w]
        all_returns[code] = rets

    ranks = {w: ff._pct_rank(vals) if vals else {} for w, vals in liquid_returns.items()}
    out = {}
    for code, rets in all_returns.items():
        code_ranks = {w: ranks[w].get(code) for w in windows}
        qualifying = [
            w for w in windows
            if rets[w] is not None and rets[w] > p["min_return_pct"]
            and code_ranks[w] is not None and code_ranks[w] >= 1 - p["top_rank_pct"]
        ]
        if qualifying:
            out[code] = {
                "returns": rets, "ranks": code_ranks, "qualifying": qualifying,
                "strength": max(code_ranks[w] for w in qualifying),
            }
    return out


def close_only_prefilter(bars, params=None):
    """Cheap step-1b gate on close-only bars before paying for an OHLC fetch:
    close above a flat-or-rising EMA21 and no deeper than max_base_depth_pct
    below the base_lookback-bar closing high. True/False."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    closes = [_adj_close(b) for b in bars]
    if len(closes) < p["ema_mid"] + p["ema_slope_bars"]:
        return False
    e21 = ema_series(closes, p["ema_mid"])
    prev = e21[-1 - p["ema_slope_bars"]]
    if prev is None or closes[-1] < e21[-1]:
        return False
    if (e21[-1] - prev) / prev * 100 < p["ema_flat_tol_pct"]:
        return False
    hi = max(closes[-p["base_lookback"]:])
    return (hi - closes[-1]) / hi * 100 <= p["max_base_depth_pct"]


def theme_strength(codes, industries):
    """「同一板塊多檔一起走強」: codes = the strength-screened set,
    industries = {code: [category, ...]} (data_sources.get_industries()).
    Returns {code: {"theme": category, "peers": n}} picking, for each code,
    its category with the most screened codes (a code can sit in several
    FinMind categories). Codes with no category are omitted."""
    counts = {}
    for c in codes:
        for cat in industries.get(c) or []:
            counts[cat] = counts.get(cat, 0) + 1
    out = {}
    for c in codes:
        cats = industries.get(c) or []
        if not cats:
            continue
        best = max(cats, key=lambda cat: (counts.get(cat, 0), cat))
        out[c] = {"theme": best, "peers": counts.get(best, 0)}
    return out


# --------------------------------------------------- 第一步：整理形態 ----

def find_base(bars, params=None):
    """The consolidation after the advance: stage high = highest high in the
    last base_lookback bars. Returns {"high_idx", "high", "low", "low_idx",
    "length", "depth_pct"} -- or None when the high is younger than
    min_base_bars (still running up, nothing to buy on a pullback yet) or
    there isn't enough history."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    n = len(bars)
    if n < p["min_base_bars"] + 2:
        return None
    start = max(0, n - p["base_lookback"])
    high_idx = max(range(start, n), key=lambda i: (bars[i]["h"], i))
    length = n - 1 - high_idx
    if length < p["min_base_bars"]:
        return None
    low_idx = min(range(high_idx + 1, n), key=lambda i: bars[i]["l"])
    high, low = bars[high_idx]["h"], bars[low_idx]["l"]
    return {
        "high_idx": high_idx, "high": high, "low_idx": low_idx, "low": low,
        "length": length, "depth_pct": (high - low) / high * 100 if high else None,
    }


def consolidation_checks(bars, base, ema_fast, ema_mid, params=None):
    """The three things step 1 says to watch, plus the explicit exclusion.
    Returns {"higher_lows", "contraction_ratio", "contracting", "reclaimed_ema",
    "downtrend", "passed"}."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    n = len(bars)
    lo_i, hi_i = base["high_idx"], n - 1
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]

    # 回調低點逐步抬高: last two confirmed swing lows inside the base ascend;
    # with fewer than two, the base's second-half low must sit above its first half's.
    _, sl = swing_points(highs, lows, p["pivot_span"], start=lo_i + 1)
    if len(sl) >= 2:
        higher_lows = sl[-1][1] > sl[-2][1]
    else:
        mid = lo_i + 1 + (hi_i - lo_i) // 2
        first, second = lows[lo_i + 1:mid], lows[mid:hi_i + 1]
        higher_lows = bool(first and second) and min(second) > min(first)

    # 波動越來越小: recent average bar range vs the base's first half
    ranges = [(b["h"] - b["l"]) / b["c"] * 100 if b["c"] else 0 for b in bars]
    k = p["contraction_recent_bars"]
    early = ranges[lo_i + 1: lo_i + 1 + max(k, (hi_i - lo_i) // 2)]
    recent = ranges[-k:]
    ratio = None
    if early and recent and sum(early) > 0:
        ratio = (sum(recent) / len(recent)) / (sum(early) / len(early))
    contracting = ratio is not None and ratio <= p["contraction_ratio_max"]

    # 重新站上走平或向上的 9 日、21 日 EMA
    s = p["ema_slope_bars"]

    def _flat_or_up(series):
        if len(series) <= s or series[-1] is None or series[-1 - s] is None:
            return False
        return (series[-1] - series[-1 - s]) / series[-1 - s] * 100 >= p["ema_flat_tol_pct"]

    close = bars[-1]["c"]
    above_fast = ema_fast[-1] is not None and (close > ema_fast[-1] or any(
        b["c"] > e for b, e in zip(bars[-s:], ema_fast[-s:]) if e is not None))
    above_mid = ema_mid[-1] is not None and close >= ema_mid[-1]
    reclaimed = above_fast and above_mid and _flat_or_up(ema_fast) and _flat_or_up(ema_mid)

    # 排除：高點、低點都在下降，股價還壓在向下的均線之下
    sh, _ = swing_points(highs, lows, p["pivot_span"], start=lo_i)
    lower_highs = len(sh) >= 2 and sh[-1][1] < sh[-2][1]
    lower_lows = len(sl) >= 2 and sl[-1][1] < sl[-2][1]
    falling_mid = ema_mid[-1] is not None and len(ema_mid) > s and ema_mid[-1 - s] is not None \
        and ema_mid[-1] < ema_mid[-1 - s]
    downtrend = lower_highs and lower_lows and falling_mid and close < ema_mid[-1]

    return {
        "higher_lows": higher_lows,
        "contraction_ratio": round(ratio, 3) if ratio is not None else None,
        "contracting": contracting,
        "reclaimed_ema": reclaimed,
        "downtrend": downtrend,
        "passed": higher_lows and contracting and reclaimed and not downtrend
                  and base["depth_pct"] is not None and base["depth_pct"] <= p["max_base_depth_pct"],
    }


# ----------------------------------------------------- 第二步：支撐區 ----

def support_levels(bars, base, emas, params=None):
    """Every candidate support at or below the last close (within
    max_level_distance_pct): EMA9/21/50, AVWAP anchored at the stage high,
    prior swing highs that price has since closed above (舊壓變支撐), swing
    lows inside the base, and unfilled up-gaps (their top edge, with the
    gap's range kept for display). [{"kind", "price", "label", ...}]."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    n = len(bars)
    close = bars[-1]["c"]
    floor_ = close * (1 - p["max_level_distance_pct"] / 100)
    ceil_ = close * (1 + p["touch_tolerance_pct"] / 100)

    def ok(price):
        return price is not None and floor_ <= price <= ceil_

    levels = []
    for kind, series in emas.items():
        if series and ok(series[-1]):
            levels.append({"kind": kind, "price": series[-1]})

    av = anchored_vwap(bars, base["high_idx"])
    if ok(av):
        levels.append({"kind": "avwap", "price": av, "anchor_date": bars[base["high_idx"]]["date"]})

    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    start = max(0, n - p["level_lookback"])
    sh, sl = swing_points(highs, lows, p["pivot_span"], start=start)
    for i, price in sh:
        # only a high that a later CLOSE has cleared counts as resistance-turned-support
        if i != base["high_idx"] and ok(price) and any(b["c"] > price for b in bars[i + 1:]):
            levels.append({"kind": "prior_high", "price": price, "date": bars[i]["date"]})
    for i, price in sl:
        if i > base["high_idx"] and ok(price):
            levels.append({"kind": "prior_low", "price": price, "date": bars[i]["date"]})
    for g in unfilled_gaps(bars, start=start):
        if ok(g["top"]):
            levels.append({"kind": "gap", "price": g["top"], "gap_bottom": g["bottom"],
                           "date": bars[g["idx"]]["date"]})
    for lv in levels:
        lv["label"] = LEVEL_LABELS[lv["kind"]]
    return levels


def build_zone(levels, params=None):
    """Greedy top-down clustering: levels within cluster_pct of the cluster's
    top form one zone. Score = distinct level kinds in it, +1 when at least
    two of AVWAP/EMA21/前高 coincide (the confluence step 2 singles out).
    Picks the HIGHEST (first one price meets on a pullback) zone with >= 2
    key kinds, else the highest scoring >= 2, else the best-scoring one.
    None if no levels."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    if not levels:
        return None
    ordered = sorted(levels, key=lambda lv: -lv["price"])
    clusters, cur = [], [ordered[0]]
    for lv in ordered[1:]:
        top = cur[0]["price"]
        if (top - lv["price"]) / top * 100 <= p["cluster_pct"]:
            cur.append(lv)
        else:
            clusters.append(cur)
            cur = [lv]
    clusters.append(cur)

    def score(cl):
        kinds = {lv["kind"] for lv in cl}
        return len(kinds) + (1 if len(kinds & KEY_LEVEL_KINDS) >= 2 else 0)

    def key_count(cl):
        return len({lv["kind"] for lv in cl} & KEY_LEVEL_KINDS)

    # Preference: the highest zone where >= 2 of AVWAP/EMA21/前高 coincide;
    # else the highest zone with any 2+ kinds; else the best-scoring one.
    key_zones = [cl for cl in clusters if key_count(cl) >= 2]
    strong = [cl for cl in clusters if score(cl) >= 2]
    chosen = key_zones[0] if key_zones else strong[0] if strong else max(clusters, key=score)
    prices = [lv["price"] for lv in chosen]
    return {
        "low": min(prices), "high": max(prices), "score": score(chosen),
        "kinds": sorted({lv["kind"] for lv in chosen}),
        "levels": chosen,
    }


def retest_state(bars, zone, params=None):
    """「站上重要位置後的第一次回測」. reclaim = the latest bar whose close
    crossed from <= zone top to > zone top. tests_since = later bars (before
    the last one) whose low came back within touch tolerance of the zone top.
    first_retest: reclaim within retest_lookback bars and no earlier test,
    i.e. the current/next touch is the first one."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    top = zone["high"]
    touch = top * (1 + p["touch_tolerance_pct"] / 100)
    n = len(bars)
    reclaim_idx = None
    for i in range(n - 1, 0, -1):
        if bars[i]["c"] > top and bars[i - 1]["c"] <= top:
            reclaim_idx = i
            break
    if reclaim_idx is None:
        return {"reclaim_date": None, "bars_since_reclaim": None, "tests_since": None, "first_retest": False}
    tests = sum(1 for b in bars[reclaim_idx + 1:n - 1] if b["l"] <= touch)
    since = n - 1 - reclaim_idx
    return {
        "reclaim_date": bars[reclaim_idx]["date"],
        "bars_since_reclaim": since,
        "tests_since": tests,
        "first_retest": since <= p["retest_lookback"] and tests == 0,
    }


def zone_status(bars, zone, params=None):
    """near_support / extended / broken from the last daily bar."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    last = bars[-1]
    if last["c"] < zone["low"] * (1 - p["break_tolerance_pct"] / 100):
        return "broken"
    dist = (last["c"] - zone["high"]) / zone["high"] * 100
    if dist <= p["near_support_pct"] or last["l"] <= zone["high"] * (1 + p["touch_tolerance_pct"] / 100):
        return "near_support"
    return "extended"


# ------------------------------------------- 第三、四步：停損/部位/R ----

def round_trip_cost_per_share(entry, exit_price, params=None):
    p = {**PULLBACK_PARAMS, **(params or {})}
    fee = p["fee_rate"] * p["fee_discount"]
    return entry * fee + exit_price * (fee + p["tax_rate"])


def r_targets(entry, stop, multiples=None, params=None):
    """1R = entry - stop. Returns {"r": 1R, "targets": [{"r_mult", "price",
    "net_r"}]} -- price tick-rounded; net_r = (target - entry - TW round-trip
    cost) / 1R, i.e. what that target is really worth after 手續費+證交稅
    (the original method quotes gross R, 「均未計交易成本」). None if
    stop >= entry."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    if entry is None or stop is None or stop >= entry:
        return None
    r = entry - stop
    out = []
    for m in (multiples or p["r_targets"]):
        price = round_to_tick(entry + m * r, "nearest")
        net = (price - entry - round_trip_cost_per_share(entry, price, p)) / r
        out.append({"r_mult": m, "price": price, "net_r": round(net, 2)})
    return {"r": r, "targets": out}


def position_size(account, entry, stop, risk_pct=None, lot_size=None, odd_lot=True, params=None):
    """「先定買點和停損點，再用允許虧損的金額除以每股風險，得出股數」.
    Capped so notional never exceeds the account (no margin). odd_lot=False
    rounds down to whole board lots. Returns {"shares", "lots", "notional",
    "pct_of_account", "risk_amount", "planned_loss"} or None when the stop
    isn't below entry. planned_loss is the loss AT the stop for the rounded
    share count -- gaps/slippage can exceed it (step 3's own caveat)."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    risk_pct = p["risk_pct"] if risk_pct is None else risk_pct
    lot_size = lot_size or p["lot_size"]
    if not account or entry is None or stop is None or stop >= entry:
        return None
    per_share = entry - stop
    risk_amount = account * risk_pct / 100
    shares = int(risk_amount // per_share)
    shares = min(shares, int(account // entry))
    if not odd_lot:
        shares = shares // lot_size * lot_size
    notional = shares * entry
    return {
        "shares": shares,
        "lots": shares / lot_size,
        "notional": round(notional, 2),
        "pct_of_account": round(notional / account * 100, 2),
        "risk_amount": round(risk_amount, 2),
        "planned_loss": round(shares * per_share, 2),
    }


def management_signal(entry, stop, price, ema9, atr=None, partials_taken=0, params=None):
    """Step 4 for an open position. partials_taken: how many of the r_targets
    have already been sold into. Returns {"r_now", "actions": [str, ...]}
    -- advice text, no orders, no state (ADR 0003: this app never records
    trades)."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    if stop >= entry:
        return None
    r = entry - stop
    r_now = (price - entry) / r
    actions = []
    if price <= stop:
        actions.append("觸及停損，依計畫出場")
        return {"r_now": round(r_now, 2), "actions": actions}
    targets = list(p["r_targets"])
    for i, m in enumerate(targets):
        if i >= partials_taken and r_now >= m:
            actions.append(f"已達 {m}R，賣出一部分")
    if ema9 is not None and atr and price > ema9 + p["extension_atr_mult"] * atr:
        actions.append(f"急拉遠離 EMA9（> {p['extension_atr_mult']:g} ATR），可趁強減碼")
    if partials_taken > 0 and ema9 is not None and price < ema9:
        actions.append("剩餘部位跌破 EMA9，出場")
    if not actions:
        actions.append("續抱，剩餘部位以 EMA9 追蹤" if partials_taken else "續抱，等 3R")
    return {"r_now": round(r_now, 2), "actions": actions}


# ------------------------------------------------------ setup (daily) ----

def evaluate_setup(bars, params=None):
    """Steps 1(整理)-3 on one code's adjusted OHLCV bars, as of the last bar.
    Always returns a dict; "passed" says whether it's a valid setup and
    "reasons" lists every failed check (Chinese, for the UI/Telegram)."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    reasons = []
    need = max(p["ema_slow"], p["ema_mid"] + p["ema_slope_bars"]) + 1
    if len(bars) < need:
        return {"passed": False, "reasons": [f"歷史不足 {need} 根日 K"]}
    closes = [b["c"] for b in bars]
    emas = {
        "ema9": ema_series(closes, p["ema_fast"]),
        "ema21": ema_series(closes, p["ema_mid"]),
        "ema50": ema_series(closes, p["ema_slow"]),
    }
    last = bars[-1]
    out = {
        "as_of": last["date"], "close": round(last["c"], 2),
        "ema": {k: round(v[-1], 2) if v[-1] is not None else None for k, v in emas.items()},
    }

    base = find_base(bars, p)
    if base is None:
        out.update(passed=False, reasons=["近期仍在創高，尚未進入整理"])
        return out
    out["base"] = {
        "high": round(base["high"], 2), "high_date": bars[base["high_idx"]]["date"],
        "low": round(base["low"], 2), "length": base["length"], "depth_pct": round(base["depth_pct"], 2),
    }
    checks = consolidation_checks(bars, base, emas["ema9"], emas["ema21"], p)
    out["checks"] = checks
    if checks["downtrend"]:
        reasons.append("高低點下降且壓在下彎均線下，不符回調買法")
    if not checks["higher_lows"]:
        reasons.append("回調低點未抬高")
    if not checks["contracting"]:
        reasons.append("波動未收斂")
    if not checks["reclaimed_ema"]:
        reasons.append("未重新站上走平/向上的 9、21 日 EMA")
    if base["depth_pct"] > p["max_base_depth_pct"]:
        reasons.append(f"整理回檔 {base['depth_pct']:.0f}% 過深")

    levels = support_levels(bars, base, emas, p)
    zone = build_zone(levels, p)
    if zone is None:
        reasons.append("附近找不到支撐位")
        out.update(passed=False, reasons=reasons)
        return out

    zone_out = {
        "low": round_to_tick(zone["low"], "down"),
        "high": round_to_tick(zone["high"], "up"),
        "score": zone["score"],
        "kinds": zone["kinds"],
        "levels": [{**lv, "price": round(lv["price"], 2)} for lv in zone["levels"]],
    }
    out["zone"] = zone_out
    out["all_levels"] = [{"kind": lv["kind"], "label": lv["label"], "price": round(lv["price"], 2)}
                         for lv in sorted(levels, key=lambda lv: -lv["price"])]
    out["retest"] = retest_state(bars, zone, p)
    out["status"] = zone_status(bars, zone, p)
    out["distance_pct"] = round((last["c"] - zone["high"]) / zone["high"] * 100, 2)

    # Planning estimate only -- the real stop comes from the intraday trigger
    # bar (intraday_trigger()). Entry reference = zone top: the trigger fires
    # just above a prior minute-bar high that formed in/near the zone.
    #
    # The zone-based stop only measures how wide the confluence zone is -- the
    # tighter the AVWAP/EMA21/prior-high overlap, the smaller it gets (a 0.2%
    # zone -> 0.3% "stop", ~0.1x daily ATR). That sits inside normal noise and
    # lets the 0.585% round-trip cost eat most of the R, so the planned stop is
    # the farther of the zone stop and entry - stop_atr_mult * ATR14.
    entry = zone_out["high"]
    zone_stop = round_to_tick(zone_out["low"] - p["stop_buffer_ticks"] * tw_tick_size(zone_out["low"]), "down")
    atr14 = an.atr([b["h"] for b in bars], [b["l"] for b in bars], closes)
    stop, stop_basis = zone_stop, "zone"
    atr_stop = atr_floor_stop(entry, atr14, p)
    if atr_stop is not None and atr_stop < zone_stop:
        stop, stop_basis = atr_stop, "atr"
    stop_pct = (entry - stop) / entry * 100
    plan = {
        "entry_ref": entry, "stop_ref": stop, "stop_pct": round(stop_pct, 2),
        "stop_basis": stop_basis, "zone_stop_ref": zone_stop,
        "stop_ok": stop_pct <= p["max_stop_pct"],
        "risk_pct": p["risk_pct"],
        "r_targets": r_targets(entry, stop, params=p),
        "atr14": _round_or_none(atr14),
    }
    out["plan"] = plan
    if not plan["stop_ok"]:
        if stop_basis == "atr":
            # The intraday trigger stop is floored at the same ATR distance, so a
            # narrower trigger bar can't rescue this setup -- don't suggest it.
            zone_pct = (entry - zone_stop) / entry * 100
            reasons.append(f"預估停損 {stop_pct:.1f}% > {p['max_stop_pct']:g}%"
                           f"（支撐區停損僅 {zone_pct:.1f}%，已放寬到 {p['stop_atr_mult']:g}×ATR；盤中停損同樣不會更近）")
        else:
            reasons.append(f"預估停損 {stop_pct:.1f}% > {p['max_stop_pct']:g}%（支撐區太寬；盤中觸發 K 線若更窄仍可做）")

    hard_fail = checks["downtrend"] or not checks["passed"]
    out["passed"] = not hard_fail
    out["reasons"] = reasons
    return out


def _round_or_none(x, nd=2):
    return round(x, nd) if x is not None else None


def setup_sort_key(setup):
    """Actionability order for the watchlist: near_support first, then first
    retest, then zone confluence score, then a sane stop, then strength."""
    status_rank = {"near_support": 0, "extended": 1, "broken": 2}
    return (
        status_rank.get(setup.get("status"), 3),
        0 if (setup.get("retest") or {}).get("first_retest") else 1,
        -((setup.get("zone") or {}).get("score") or 0),
        0 if (setup.get("plan") or {}).get("stop_ok") else 1,
        -(setup.get("strength") or 0),
    )


# ----------------------------------------------------- 盤中觸發（分鐘） ----

def ticks_to_bars(prices, ts_list, minutes=5):
    """Group (price, epoch-seconds) ticks into `minutes`-minute OHLC bars,
    ascending: [{"t": bucket start epoch, "o", "h", "l", "c"}]. The last bar
    is still forming."""
    bars = []
    span = minutes * 60
    for price, ts in zip(prices, ts_list):
        if price is None or ts is None:
            continue
        bucket = int(ts // span * span)
        if bars and bars[-1]["t"] == bucket:
            b = bars[-1]
            b["h"] = max(b["h"], price)
            b["l"] = min(b["l"], price)
            b["c"] = price
        else:
            bars.append({"t": bucket, "o": price, "h": price, "l": price, "c": price})
    return bars


def intraday_trigger(mbars, zone_low, zone_high, params=None, atr=None):
    """Step 2's minute-chart trigger on today's 1/5/15-minute bars
    (ascending, last one may still be forming):

      waiting   -- price hasn't come down into the zone yet
      dipped    -- traded into the zone (下探) and still holding it, no
                   break of a prior bar's high yet
      triggered -- after the dip's low bar, a bar traded above the previous
                   bar's high (回升 + 突破前一根 K 線高點) while the session
                   low still held above the zone's break level
      broken    -- latest close is below the zone (一路跌穿，沒有回升 -> 繼續等待)

    For "triggered": trigger_price = previous bar's high + 1 tick (the buy-stop
    level), stop = min(trigger bar low, previous bar low) - 1 tick
    (「突破 K 線或前一根 K 線的低點」), day_low_stop = session low - 1 tick
    (「當天低點」, the wider alternative), plus stop_pct for both. Pass the
    daily `atr` (plan["atr14"]) to floor both stops at stop_atr_mult * ATR
    below the trigger price -- see apply_atr_stop_floor()."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    if not mbars:
        return {"state": "waiting"}
    touch = zone_high * (1 + p["touch_tolerance_pct"] / 100)
    floor_ = zone_low * (1 - p["break_tolerance_pct"] / 100)
    first_touch = next((i for i, b in enumerate(mbars) if b["l"] <= touch), None)
    if first_touch is None:
        return {"state": "waiting"}
    if mbars[-1]["c"] < floor_:
        return {"state": "broken", "last": mbars[-1]["c"]}

    day_low = min(b["l"] for b in mbars)
    # The dip's low bar = lowest low from the first touch on; the reversal
    # must come after it (a "break of the previous high" on the way down
    # isn't 下探後回升).
    low_idx = min(range(first_touch, len(mbars)), key=lambda i: (mbars[i]["l"], -i))
    for j in range(max(low_idx + 1, 1), len(mbars)):
        prev, cur = mbars[j - 1], mbars[j]
        trig = prev["h"] + tw_tick_size(prev["h"])
        if cur["h"] >= trig and min(b["l"] for b in mbars[:j + 1]) >= floor_:
            raw_stop = min(prev["l"], cur["l"])
            stop = round_to_tick(raw_stop - tw_tick_size(raw_stop), "down")
            day_stop = round_to_tick(day_low - tw_tick_size(day_low), "down")
            entry = round(trig, 2)
            return apply_atr_stop_floor({
                "state": "triggered", "bar_index": j, "trigger_price": entry,
                "stop": stop, "stop_pct": round((entry - stop) / entry * 100, 2),
                "day_low_stop": day_stop, "day_low_stop_pct": round((entry - day_stop) / entry * 100, 2),
                "stop_ok": (entry - stop) / entry * 100 <= p["max_stop_pct"],
            }, atr, p)
    return {"state": "dipped", "low": mbars[low_idx]["l"],
            "next_trigger": round(mbars[-1]["h"] + tw_tick_size(mbars[-1]["h"]), 2)}


def atr_floor_stop(entry, atr, params=None):
    """Lowest-risk-allowed stop: `entry - stop_atr_mult * atr`, snapped down to
    the tick grid. None when there is no ATR or the multiple is 0. The single
    definition used by both the daily plan (evaluate_setup()) and the intraday
    trigger (apply_atr_stop_floor()), so the two can't drift apart."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    if not atr or not p["stop_atr_mult"]:
        return None
    return round_to_tick(entry - p["stop_atr_mult"] * atr, "down")


def apply_atr_stop_floor(trig, atr, params=None):
    """Return a copy of a "triggered" result whose stops are no closer than
    stop_atr_mult * ATR14 below the trigger price -- the same floor the daily
    plan uses (atr_floor_stop()). A 5-minute trigger bar is often only
    0.2-0.5% tall, well inside a normal day's range, and the 0.585% round-trip
    cost then eats most of the R. Applies to both `stop` and `day_low_stop`;
    the first call records the bar-only stop in `bar_stop`/`bar_stop_pct`.
    Idempotent for the same ATR (the floor can only move a stop down), so
    intraday_trigger() and format_trigger_message() may both call it; a second
    call with a LARGER ATR floors again and keeps the original bar_stop. No-op
    without an ATR or a multiple of 0, or on a non-triggered result."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    entry = trig.get("trigger_price")
    floor_stop = atr_floor_stop(entry, atr, p) if trig.get("state") == "triggered" else None
    if floor_stop is None:
        return trig
    out = dict(trig)
    out.setdefault("bar_stop", trig["stop"])
    out.setdefault("bar_stop_pct", trig["stop_pct"])
    out["stop"] = min(trig["stop"], floor_stop)
    out["day_low_stop"] = min(trig["day_low_stop"], floor_stop)
    out["stop_pct"] = round((entry - out["stop"]) / entry * 100, 2)
    out["day_low_stop_pct"] = round((entry - out["day_low_stop"]) / entry * 100, 2)
    out["stop_ok"] = (entry - out["stop"]) / entry * 100 <= p["max_stop_pct"]
    out["stop_basis"] = "atr" if out["stop"] < out["bar_stop"] else "bar"
    return out


def format_trigger_message(code, name, setup, trig, account=None, params=None):
    """Telegram text for a fired trigger (monitor.py). Includes the share
    count only when an account size is configured."""
    p = {**PULLBACK_PARAMS, **(params or {})}
    z = setup["zone"]
    # monitor.py calls intraday_trigger() without the daily ATR, so floor here
    # too (idempotent) -- the alert must never show a stop tighter than the plan's.
    trig = apply_atr_stop_floor(trig, (setup.get("plan") or {}).get("atr14"), p)
    entry, stop = trig["trigger_price"], trig["stop"]
    rt = r_targets(entry, stop, params=p)
    widened = (f"（觸發棒停損僅 {trig['bar_stop_pct']:.2f}%，已放寬到 {p['stop_atr_mult']:g}×ATR）"
               if trig.get("stop_basis") == "atr" else "")
    lines = [
        f"[{code}{' ' + name if name else ''}] 強勢回調觸發：下探支撐區 {z['low']:g}–{z['high']:g} 後"
        f"突破前一根{p['intraday_bar_minutes']}分K高點",
        f"買進參考 {entry:g}｜停損 {stop:g}（-{trig['stop_pct']:.2f}%）{widened}"
        + ("" if trig["stop_ok"] else f" ⚠ 超過 {p['max_stop_pct']:g}%"),
    ]
    if rt:
        lines.append("目標 " + "／".join(f"{t['r_mult']}R {t['price']:g}" for t in rt["targets"]))
    size = position_size(account, entry, stop, params=p) if account else None
    if size and size["shares"] > 0:
        lines.append(f"帳戶 {account:,.0f}、風險 {p['risk_pct']:g}% → {size['shares']:,} 股"
                     f"（{size['lots']:.2f} 張，投入 {size['pct_of_account']:.0f}%）")
    lines.append("入場後立即掛停損；跳空/滑價可能使虧損超過計畫")
    return "\n".join(lines)
