# -*- coding: utf-8 -*-
"""Daily scan for the 強勢股回調買法 (src/pullback_strategy.py) -- the second
stock-selection method next to the multi-factor ranking
(scripts/run_daily_recommendations.py). See docs/pullback_strategy.md.

Two stages, because cache/factor_run/*.json only stores close+volume
(scripts/run_factor_backtest.py daily_to_bars()) and is refreshed weekly:

  1. Screen (no network): pullback_strategy.strength_screen() ranks every
     liquid cached code by 1/3/6-month return (c_adj) and keeps the ones up
     >30% and in the top 30% of any window; close_only_prefilter() then keeps
     those sitting above a flat-or-rising EMA21 and not too deep below their
     recent high. Strongest first, capped at --max-fetch.
  2. Setup (one FinMind request per code via data_sources.get_daily(), same
     per-day cache key the live UI uses): fresh OHLCV, back-adjusted with the
     cache's own c_adj/c factors so every level is on today's price scale,
     then pullback_strategy.evaluate_setup() -- consolidation checks, support
     zone, first-retest flag, planned stop/R targets.

Writes meta.pullback_watchlist (backtest.db, same store as
meta.daily_recommendations) and sends one Telegram summary.

Usage:
    python scripts/run_pullback_scan.py [--top-n 20] [--max-fetch 120] [--dry-run]
    python scripts/run_pullback_scan.py --codes 2330,3017   # skip stage 1, evaluate these
    python scripts/run_pullback_scan.py --universe watchlist

An empty factor cache prints a message and leaves meta untouched (same
"don't fail the schedule, don't clobber meta" stance as
run_daily_recommendations.py).
"""
import argparse
import bisect
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import alerts  # noqa: E402
import data_sources as ds  # noqa: E402
import db  # noqa: E402
import pipeline  # noqa: E402
import pullback_strategy as ps  # noqa: E402

import report_factor_ic as rfi  # noqa: E402

META_KEY = "pullback_watchlist"
FETCH_SLEEP_SECONDS = 0.2
MAX_FETCH_FAIL_RATIO = 0.5   # more failed fetches than this => data source is down, don't publish


def adjustment_factors(cached_bars):
    """(sorted dates, factors) from the factor cache's c_adj/c -- adjust.py's
    convention, latest segment == 1.0. Empty when the cache never got c_adj."""
    pairs = [(b["date"], b["c_adj"] / b["c"]) for b in cached_bars or []
             if b.get("c_adj") is not None and b.get("c")]
    pairs.sort()
    return [d for d, _ in pairs], [f for _, f in pairs]


def daily_to_ohlc_bars(daily, factor_dates=None, factors=None):
    """data_sources.get_daily() {t,o,h,l,c,v,adjusted} -> ascending
    [{date,o,h,l,c,v}] with o/h/l/c multiplied by the back-adjustment factor
    of the first cached date on/after each bar (a bar between two cached
    dates shares the later one's factor; a bar newer than the cache gets 1.0).
    Volume stays raw. A yfinance-sourced series (adjusted=True, already
    split-adjusted upstream) is left as-is to avoid double-counting splits
    (evaluate_codes() skips such codes anyway -- different price scale).
    Known gap: an ex-dividend date AFTER the cache's last bar isn't in
    `factors` yet, so the few bars before it stay unadjusted until the next
    weekly cache refresh (docs/pullback_strategy.md 已知限制)."""
    factor_dates = factor_dates or []
    factors = factors or []
    use = bool(factor_dates) and not daily.get("adjusted")
    bars = []
    for i in range(len(daily["t"])):
        date = dt.datetime.fromtimestamp(daily["t"][i]).date().isoformat()
        f = 1.0
        if use:
            j = bisect.bisect_left(factor_dates, date)
            f = factors[j] if j < len(factors) else 1.0
        bars.append({
            "date": date,
            "o": daily["o"][i] * f, "h": daily["h"][i] * f,
            "l": daily["l"][i] * f, "c": daily["c"][i] * f,
            "v": daily["v"][i],
        })
    return bars


# TWSE/TPEX daily price limit is ±10% of the reference price; a close-to-
# close move beyond this can only be a corporate action (split, 減資, large
# stock dividend) that the back-adjustment didn't cover.
LIMIT_MOVE = 0.11
DISCONTINUITY_WINDOW = 160   # bars evaluate_setup() actually looks at (base/levels/EMA50 warmup)


def unadjusted_jump(bars, after_date=None, window=DISCONTINUITY_WINDOW):
    """Date of the first close-to-close move beyond the price limit among
    the last `window` bars dated after `after_date` (the factor cache's last
    adjusted date; None = check the whole window), or None. A hit means the
    levels would be computed across an un-adjusted split -- e.g. a 1:3 split
    a week after the weekly cache refresh -- so the caller skips the code
    instead of publishing a support zone on the wrong price scale."""
    start = max(1, len(bars) - window)
    for i in range(start, len(bars)):
        if after_date is not None and bars[i]["date"] <= after_date:
            continue
        prev = bars[i - 1]["c"]
        if prev and abs(bars[i]["c"] / prev - 1) > LIMIT_MOVE:
            return bars[i]["date"]
    return None


def _slim(setup):
    """Drop the bulky debugging fields before storing in meta / data.json."""
    keep = ("as_of", "close", "ema", "base", "zone", "retest", "status", "distance_pct", "plan", "reasons")
    out = {k: setup.get(k) for k in keep}
    out["checks"] = {k: (setup.get("checks") or {}).get(k) for k in ("higher_lows", "contraction_ratio", "reclaimed_ema")}
    if out.get("zone"):
        out["zone"] = {**out["zone"], "levels": [
            {k: lv[k] for k in ("kind", "label", "price") if k in lv} for lv in out["zone"]["levels"]
        ]}
    return out


def evaluate_codes(codes, universe_data, screen, themes, names, max_fetch, fetch_fn=None, sleep=FETCH_SLEEP_SECONDS):
    """Stage 2 over `codes` (already ordered by priority). Returns
    (setups, stats) where setups = passing setups only, each enriched with
    the stage-1 strength info, theme and name."""
    fetch_fn = fetch_fn or ds.get_daily
    setups, fetched, failed, rejected, unadjusted, off_scale = [], 0, 0, 0, 0, 0
    for code in codes[:max_fetch]:
        try:
            daily = fetch_fn(code)
        except Exception as e:  # noqa: BLE001 -- one bad code must not stop the scan
            print(f"  {code}: 日線抓取失敗（{e}）")
            failed += 1
            continue
        fetched += 1
        if sleep:
            time.sleep(sleep)
        if not daily or not daily.get("c"):
            failed += 1
            continue
        if daily.get("adjusted"):
            # yfinance fallback: split-adjusted only (no dividend factor) and
            # exempt from unadjusted_jump(), so its levels sit on a different
            # price scale than the FinMind-based rest of the list. Skip rather
            # than publish a mixed-scale watchlist.
            print(f"  {code}: 日線來源 {daily.get('source')} 價格尺度與因子快取不同，略過")
            off_scale += 1
            continue
        fd, fs = adjustment_factors((universe_data.get(code) or {}).get("bars"))
        bars = daily_to_ohlc_bars(daily, fd, fs)
        jump = unadjusted_jump(bars, fd[-1] if fd else None)
        if jump:
            print(f"  {code}: {jump} 疑似除權/分割尚未還原（因子快取只到 {fd[-1] if fd else '無'}），略過")
            unadjusted += 1
            continue
        setup = ps.evaluate_setup(bars)
        if not setup.get("passed"):
            rejected += 1
            continue
        s = screen.get(code) or {}
        theme = themes.get(code) or {}
        setups.append({
            "code": code,
            "name": names.get(code) or code,
            "theme": theme.get("theme"),
            "theme_peers": theme.get("peers"),
            "returns": {k: (round(v, 1) if v is not None else None) for k, v in (s.get("returns") or {}).items()},
            "qualifying": s.get("qualifying") or [],
            "strength": s.get("strength"),
            "status_label": ps.STATUS_LABELS.get(setup.get("status")),
            "source": daily.get("source"),
            **_slim(setup),
        })
    setups.sort(key=ps.setup_sort_key)
    return setups, {"fetched": fetched, "failed": failed, "rejected": rejected, "unadjusted": unadjusted, "off_scale": off_scale}


def build_scan(universe_data, top_n, max_fetch, codes=None, fetch_fn=None, industries=None, names=None, sleep=FETCH_SLEEP_SECONDS):
    """Stage 1 + 2. codes=None runs the screen; a list skips it (manual
    --codes/--universe watchlist mode). Returns the meta payload minus
    generated_at, or None when there's nothing to scan."""
    if codes is None:
        if not universe_data:
            return None
        screen = ps.strength_screen(universe_data)
        strong = sorted(screen, key=lambda c: -screen[c]["strength"])
        candidates = [c for c in strong if ps.close_only_prefilter(universe_data[c]["bars"])]
        mode = "screen"
    else:
        screen, strong, candidates, mode = {}, list(codes), list(codes), "codes"

    if industries is None:
        industries = ds.get_industries(cache=True) or {}
    if names is None:
        names = {u["code"]: u["name"] for u in (ds.get_universe(cache=True) or [])}
    themes = ps.theme_strength(strong, industries)

    setups, stats = evaluate_codes(candidates, universe_data, screen, themes, names, max_fetch,
                                   fetch_fn=fetch_fn, sleep=sleep)
    as_of = max((s["as_of"] for s in setups), default=None)
    p = ps.PULLBACK_PARAMS
    return {
        "as_of": as_of,
        "mode": mode,
        "params": {k: p[k] for k in ("min_return_pct", "top_rank_pct", "min_turnover",
                                     "max_stop_pct", "risk_pct", "r_targets", "intraday_bar_minutes")},
        "counts": {
            "universe": len(universe_data), "strong": len(strong), "candidates": len(candidates),
            **stats, "setups": len(setups),
            "near_support": sum(1 for s in setups if s["status"] == "near_support"),
        },
        "setups": setups[:top_n],
    }


def fetch_failure_too_high(counts, max_fetch, max_ratio=MAX_FETCH_FAIL_RATIO):
    """True when so many stage-2 fetches failed that the result reflects an
    outage (FinMind quota/down), not the market -- publishing it would
    replace a good watchlist with an empty one. Off-scale (yfinance-fallback)
    codes count as failures: they only show up when FinMind is limited."""
    attempted = min(counts.get("candidates", 0), max_fetch)
    bad = counts.get("failed", 0) + counts.get("off_scale", 0)
    return attempted > 0 and bad > attempted * max_ratio


def summary_text(payload):
    near = [s for s in payload["setups"] if s["status"] == "near_support"]
    scope = "強勢" if payload.get("mode") == "screen" else "指定"
    head = (f"[強勢回調] as_of={payload['as_of']} {scope} {payload['counts']['strong']} 檔 → "
            f"合格整理 {payload['counts']['setups']} 檔，接近支撐 {payload['counts']['near_support']} 檔")
    lines = [head]
    for s in near[:8]:
        z, pl = s["zone"], s["plan"]
        first = "・首次回測" if (s.get("retest") or {}).get("first_retest") else ""
        lines.append(f"{s['code']} {s['name']}：支撐 {z['low']:g}–{z['high']:g}（{'/'.join(_labels(z))}）"
                     f" 停損約 {pl['stop_pct']:.1f}%{first}")
    if near:
        lines.append("盤中：下探支撐後回升、突破前一根 1/5/15 分 K 高點才進場；跌穿不回升就等")
    return "\n".join(lines)


def _labels(zone):
    seen = []
    for lv in zone.get("levels") or []:
        if lv["label"] not in seen:
            seen.append(lv["label"])
    return seen


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--max-fetch", type=int, default=120,
                    help="第二階段最多抓幾檔日線（每檔 1 個 FinMind 請求）")
    ap.add_argument("--codes", type=str, default="", help="逗號分隔，跳過強勢股篩選直接評估")
    ap.add_argument("--universe", choices=["screen", "watchlist"], default="screen")
    ap.add_argument("--dry-run", action="store_true", help="計算並印出結果，不寫 meta 也不發 Telegram")
    args = ap.parse_args()

    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")

    codes = None
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.universe == "watchlist":
        codes = [w["code"] for w in pipeline.load_watchlist()]
    elif not universe_data:
        print("nothing cached -- run scripts/update_universe_bars.py "
              "(or scripts/run_factor_backtest.py --universe all) first")
        return

    payload = build_scan(universe_data, args.top_n, args.max_fetch, codes=codes)
    if payload is None:
        print("沒有可掃描的股票，未寫入 meta")
        return
    if codes is None and fetch_failure_too_high(payload["counts"], args.max_fetch):
        c = payload["counts"]
        print(f"日線抓取失敗 {c['failed']}/{min(c['candidates'], args.max_fetch)} 檔，疑似資料源異常，"
              "未寫入 meta、未發 Telegram", file=sys.stderr)
        sys.exit(1)
    payload = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), **payload}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    summary = summary_text(payload)
    print(summary)

    # Manual --codes/--watchlist runs are for looking, not for replacing the
    # scheduled all-market list the UI shows.
    if args.dry_run or codes is not None:
        print("（dry-run 或指定代碼模式，未寫入 meta，未發 Telegram）")
        return

    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)
    db.set_meta(conn, META_KEY, json.dumps(payload, ensure_ascii=False))
    conn.close()
    alerts.send_telegram(summary)


if __name__ == "__main__":
    main()
