# -*- coding: utf-8 -*-
"""Squeeze-breakout (inside day / range contraction) daily-bar proxy event study.

Implements docs/squeeze_breakout_preregistration_2026-10.md section by section
(the section number is cited next to each rule). Pure functions, stdlib only,
no I/O -- scripts/run_squeeze_backtest.py does the loading, the lock check and
the printing. Nothing here may change a rule in that document; a rule change
means a new pre-registration.

Only the signal differs from the EP-TW study: stop, entry trigger, exits,
limit-price handling, costs, the control draw and every statistic are the
SAME code (ep_strategy.simulate_trade() / run_trades() with the frozen
EP_PARAMS, pullback_backtest's gates), so any difference in the results comes
from the entry pattern alone. A "signal day" here is the SETUP bar s (the plan's
t-1: the inside / contracted bar); the entry is attempted on bar s+1 (the plan's
t) at s's high + 1 tick, the stop is s's low - 1 tick -- exactly ep_strategy's
signal-bar convention.

Nothing in ep_strategy.py / pullback_*.py is modified.
"""
import ep_strategy as ep
import finlab_factors as ff
import pullback_backtest as bt
import pullback_strategy as ps

# Section 2.2, frozen. Stop / exit / cost / limit parameters are ep.EP_PARAMS (not copied:
# the pre-registration names them and tests check that); the keys below are the signal's.
SQUEEZE_PARAMS = {
    # strength screen: same values as pullback_strategy.PULLBACK_PARAMS (a code qualifies on ANY window)
    "return_windows": {"1m": 21, "3m": 63, "6m": 126},
    "min_return_pct": 30.0, "top_rank_pct": 0.30,
    "min_turnover": 50_000_000, "turnover_window": 20,
    # pattern
    "squeeze_recent_bars": 5, "squeeze_base_bars": 20, "squeeze_ratio_max": 0.7,
    # trend
    "ema_fast": 9, "ema_mid": 21, "ema_slow": 50, "ema_gap_max_pct": 2.0,
}
EPS = 1e-9


# -------------------------------------------------------- strength screen ----

def _screen_rows(raw_bars, p):
    """Per bar k of one code: None when the code fails liquidity at k, else
    {window: return pct | None}. Same arithmetic as pullback_strategy.strength_screen():
    liquidity = raw close x volume averaged over the turnover_window bars ending at k
    (inclusive); returns use c_adj (ff._close) k vs k-n, None when k-n < 0 or the base is falsy."""
    w = p["turnover_window"]
    pt = [0.0]
    for b in raw_bars:
        pt.append(pt[-1] + (b["c"] or 0) * (b["v"] or 0))
    closes = [ff._close(b) for b in raw_bars]
    rows = []
    for k in range(len(raw_bars)):
        if k + 1 < w or (pt[k + 1] - pt[k + 1 - w]) / w <= p["min_turnover"]:
            rows.append(None)
            continue
        rets = {}
        for name, n in p["return_windows"].items():
            if k - n < 0:
                rets[name] = None
                continue
            base = closes[k - n]
            rets[name] = (closes[k] - base) / base * 100 if base else None
        rows.append(rets)
    return rows


def strong_by_day(raw, universe, params=None):
    """Section 2.3 item 1b: {date: sorted [codes]} that pass the strength screen
    evaluated at that date using only data <= that date -- liquid codes, return
    over a window > min_return_pct AND within the top top_rank_pct of the liquid
    codes that have that window (percentile rank, ties averaged), on any window.
    Must reproduce pullback_strategy.strength_screen() day by day (a test checks).
    raw: {code: factor-cache bars}; universe: {code: adjusted bars}, same dates;
    only codes in `universe` are ranked (the pullback study's convention)."""
    p = {**SQUEEZE_PARAMS, **(params or {})}
    windows = list(p["return_windows"])
    per_day = {}                                     # date -> {code: rets} for liquid codes
    for code in sorted(universe):
        rows = _screen_rows(raw[code], p)
        for b, r in zip(raw[code], rows):
            if r is not None:
                per_day.setdefault(b["date"], {})[code] = r
    out = {}
    for d in sorted(per_day):
        day = per_day[d]
        ranks = {}
        for w in windows:
            vals = {c: r[w] for c, r in day.items() if r[w] is not None}
            ranks[w] = ff._pct_rank(vals) if vals else {}
        strong = [c for c, r in day.items() if any(
            r[w] is not None and r[w] > p["min_return_pct"]
            and ranks[w].get(c) is not None and ranks[w][c] >= 1 - p["top_rank_pct"]
            for w in windows)]
        if strong:
            out[d] = sorted(strong)
    return out


# ------------------------------------------------------------- signals ----

def scan_code(bars, strong_dates, params=None, start_date=None, end_date=None):
    """Section 2.3 item 1 for one code: every setup bar s that is a signal day.
    bars: adjusted OHLC; strong_dates: set of dates on which the code passes the
    strength screen. Only bars <= s are read. Returns
    [{"date","pattern"("inside"|"squeeze"|"both"),"range_ratio","ema_gap_pct"}]."""
    p = {**SQUEEZE_PARAMS, **(params or {})}
    nr, nb = p["squeeze_recent_bars"], p["squeeze_base_bars"]
    closes = [b["c"] for b in bars]
    e_fast = ps.ema_series(closes, p["ema_fast"])
    e_mid = ps.ema_series(closes, p["ema_mid"])
    e_slow = ps.ema_series(closes, p["ema_slow"])
    amp = [0.0] + [(bars[k]["h"] - bars[k]["l"]) / bars[k - 1]["c"] for k in range(1, len(bars))]
    pa = [0.0]
    for a in amp:
        pa.append(pa[-1] + a)
    out = []
    for s in range(max(nb + nr, p["ema_slow"]), len(bars)):
        b, prev = bars[s], bars[s - 1]
        if start_date and b["date"] < start_date:
            continue
        if end_date and b["date"] > end_date:
            continue
        if b["date"] not in strong_dates:
            continue
        if e_fast[s] is None or e_mid[s] is None or e_slow[s] is None:
            continue
        if not (b["c"] > e_mid[s] > e_slow[s]):
            continue
        gap = abs(e_fast[s] - e_mid[s]) / e_mid[s] * 100
        if gap > p["ema_gap_max_pct"] + EPS:
            continue
        inside = b["h"] < prev["h"] and b["l"] > prev["l"]
        recent = (pa[s + 1] - pa[s + 1 - nr]) / nr
        base = (pa[s + 1 - nr] - pa[s + 1 - nr - nb]) / nb
        ratio = recent / base if base > 0 else None
        squeeze = ratio is not None and ratio <= p["squeeze_ratio_max"] + EPS
        if not (inside or squeeze):
            continue
        out.append({"date": b["date"], "pattern": "both" if inside and squeeze else "inside" if inside else "squeeze",
                    "range_ratio": ratio, "ema_gap_pct": gap})
    return out


def scan_signals(universe, strong, params=None, start_date=None, end_date=None):
    """Section 2.3 item 1 over every code. universe: {code: adjusted bars};
    strong: strong_by_day() output. Only common-stock codes (section 2.1).
    Returns [{"date","code",...}] sorted by (date, code)."""
    by_code = {}
    for d, codes in strong.items():
        for c in codes:
            by_code.setdefault(c, set()).add(d)
    signals = []
    for code in sorted(universe):
        if not ep.is_common_stock(code) or code not in by_code:
            continue
        for s in scan_code(universe[code], by_code[code], params, start_date, end_date):
            signals.append({"code": code, **s})
    return sorted(signals, key=lambda s: (s["date"], s["code"]))


def control_pool(strong, signal_days):
    """Section 2.4: {day: [codes]} -- the strength-screened codes on each signal
    day (the control group draws one of them). Days with no strong code are
    dropped."""
    return {d: strong[d] for d in sorted(set(signal_days)) if strong.get(d)}


# ------------------------------------------------------ report helpers ----

def pattern_groups(trades, signals, n=bt.BOOTSTRAP_N):
    """Reported only (section 4): de-duplicated trades split by the signal's
    pattern label ("inside" / "squeeze" / "both"). Groups under MIN_GROUP_FOR_CI
    report n and mean only."""
    label = {(s["code"], s["date"]): s["pattern"] for s in signals}
    out = {}
    for name in ("inside", "squeeze", "both"):
        ts = [t for t in trades if label.get((t["code"], t["signal_date"])) == name]
        if len(ts) < bt.MIN_GROUP_FOR_CI:
            out[name] = {"n": len(ts), "mean_net_r": bt._mean([t["net_r"] for t in ts]), "note": "樣本不足"}
            continue
        s = bt.summarize(ts)
        s["ci"] = bt.cluster_bootstrap_ci(ts, n)
        out[name] = s
    return out
