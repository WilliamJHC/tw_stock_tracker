# -*- coding: utf-8 -*-
"""Purged walk-forward comparison of two entry-timing rules for the SAME
monthly top-N selection -- docs/factor_timeframe_optimization_directions.md
S3.2: "不是選到當下的下一根 K 棒開盤就買，而是等
breakout_strategy.technical_signal()/chip_signal() 確認後才進場". Monthly
selection (which stocks) is untouched; this only tests WHEN a newly
selected code actually gets bought.

- baseline: portfolio_backtest.simulate() unchanged -- buy immediately at
  the rebalance date's close, exactly like the live pipeline does today.
- gated: a new simulate_with_entry_gate() below. Every code newly entering
  the target-N set (transitioning from not-held to held) is NOT bought at
  the rebalance date; instead it waits in a `pending` pool, re-checked
  EVERY subsequent trading day (not just the next rebalance) for
  breakout_strategy.entry_signal() to confirm, and is dropped from
  `pending` without ever being bought if it falls out of the target set at
  a later rebalance before confirming. A code already held before this
  rebalance that stays selected is resized exactly like baseline (no
  re-gating) -- this only gates genuinely NEW entries, not continuing
  positions.

Requires scripts/fetch_breakout_layer_data.py to have populated
cache/breakout_layer/ first (full OHLC + raw daily institutional rows for
every code that was ever in the monthly top-N -- see that script's
docstring for why this data doesn't already exist in cache/factor_run/).

## Deliberate simplifications (documented, not silent)

1. **Margin gate skipped.** chip_signal()'s margin_window is always passed
   as [] (never fetched -- see fetch_breakout_layer_data.py's docstring on
   scope). chip_signal()'s own docstring says a missing margin_window does
   NOT block entry (margin_ok defaults True), so `confirmed` here depends
   only on the 三大法人合力 leg (>=2 of foreign/trust/dealer net-buying over
   BREAKOUT_PARAMS["chip_lookback_days"]), not the full 3-gate fusion
   entry_signal() gets in its normal single-stock usage
   (replay_breakout()/simulate_breakout()). This is a partial-fusion test,
   not the complete breakout_strategy engine -- flagged clearly in every
   report this script prints.

2. **Fill price/timing.** src/breakout_strategy.py's OWN convention
   (simulate_breakout()'s docstring) is signal-day-D / fill-at-D+1-OPEN,
   because technical_signal(...,i) reads bar i's own close/volume/high/low
   and so can only be known after day i's close -- this script keeps that
   exact D/D+1 anti-lookahead timing (a code confirmed using day i's
   breakout-layer bar is queued and bought using day i+1's price, never
   day i's), but fills at day i+1's CLOSE from the SAME adjusted-close
   price series (universe_data's prices[code], c_adj-preferring) baseline
   buys/sells/marks-to-market with, not day i+1's raw OPEN from the
   separate cache/breakout_layer/ OHLC. Using breakout_layer's own raw,
   unadjusted open would put the gated variant's fills on a different,
   split-unadjusted price scale than every other trade in this comparison
   -- building a second adjusted-open series just for this one fill event
   is exactly the kind of pipeline expansion this project's "不過度工程化"
   rule argues against for an unproven candidate. The D-vs-D+1 anti-
   lookahead boundary (the part that actually matters for not cheating)
   is preserved; only the intraday open-vs-close choice within day D+1 is
   simplified.

Usage:
    python scripts/report_breakout_entry_walkforward.py [--folds 4] [--purge-days 60]
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import analysis as an  # noqa: E402
import breakout_strategy as bs  # noqa: E402
import finlab_factors as ff  # noqa: E402
import optimize as opt  # noqa: E402
import portfolio_backtest as pb  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report_factor_ic import load_cached_universe  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BREAKOUT_LAYER_DIR = os.path.join(BASE_DIR, "cache", "breakout_layer")

CANDIDATES = ["baseline (現況，選到就買)", "gated (等 breakout 訊號確認)"]


def load_breakout_data():
    """{code: {"bar_dates", "ctx", "highs", "lows", "inst_rows"}} from
    cache/breakout_layer/*.json (scripts/fetch_breakout_layer_data.py's
    output) -- ctx/highs/lows built ONCE per code here (not per day), same
    anti-lookahead ctx/at pattern as breakout_strategy.replay_breakout()."""
    out = {}
    if not os.path.isdir(BREAKOUT_LAYER_DIR):
        return out
    for fname in os.listdir(BREAKOUT_LAYER_DIR):
        if not fname.endswith(".json"):
            continue
        code = fname[:-5]
        with open(os.path.join(BREAKOUT_LAYER_DIR, fname), encoding="utf-8") as f:
            data = json.load(f)
        bars = data.get("bars") or []
        if len(bars) <= bs.WARMUP:
            continue
        closes = [b["c"] for b in bars]
        vols = [b["v"] for b in bars]
        opens = [b["o"] for b in bars]
        ctx = an.build_backtest_ctx({"c": closes, "v": vols, "o": opens})
        out[code] = {
            "bar_dates": [b["date"] for b in bars],
            "ctx": ctx,
            "highs": [b["h"] for b in bars],
            "lows": [b["l"] for b in bars],
            "inst_rows": data.get("inst_rows") or [],
        }
    return out


def simulate_with_entry_gate(universe_data, breakout_data, params=None, ranks_cache=None):
    """See module docstring. Same return contract as
    portfolio_backtest.simulate(): {"equity_curve", "trades", "summary"}."""
    p = dict(pb.PORTFOLIO_PARAMS)
    if params:
        p.update(params)
    leg_fee = p["fee_pct"] / 2 / 100
    slip = p.get("slippage_pct", 0.0) / 100
    lot_size = p.get("lot_size", 0)
    chip_days = bs.BREAKOUT_PARAMS["chip_lookback_days"]

    calendar = pb._trading_calendar(universe_data)
    if not calendar:
        return {"equity_curve": [], "trades": [], "summary": {}}
    rebalance_dates = set(pb._rebalance_dates(calendar, freq=p.get("rebalance_freq", "monthly")))

    prices = {
        code: {b["date"]: (b["c_adj"] if b.get("c_adj") is not None else b["c"]) for b in d["bars"]}
        for code, d in universe_data.items()
    }
    bar_dates = {code: d["bar_dates"] for code, d in universe_data.items()}

    cash = p["initial_capital"]
    holdings = {}
    pending = {}       # code -> {"target_value": float} -- selected, not yet confirmed
    to_buy_next = {}    # code -> target_value -- confirmed yesterday, fill at TODAY's price
    equity_curve, trades = [], []

    for date in calendar:
        if date in rebalance_dates:
            ranks = None
            if ranks_cache is not None:
                ranks = ranks_cache.get(date)
                if ranks is None:
                    ranks = ff.factor_ranks(universe_data, date, extra_factors=p.get("extra_factors"))
                    ranks_cache[date] = ranks
            scores = ff.composite_scores(
                universe_data, date, weights=p["ranking_weights"], ranks=ranks, extra_factors=p.get("extra_factors")
            )
            target_codes = set(ff.top_n(scores, p["top_n"]))

            # A code confirmed on the PREVIOUS trading day (queued in
            # to_buy_next, about to fill below at step 0) whose score
            # happens to drop out of target_codes on THIS SAME rebalance
            # date would otherwise get bought at step 0 and then
            # immediately sold by the de-selection loop right below --
            # a same-day wash trade charging both legs' fees/slippage for
            # a position that nets to zero. Cancel the queued fill instead
            # of letting it execute: this rebalance's own selection is the
            # more current signal, so a fill that would be reversed within
            # the same date never happened at all (2026-09-25 code review
            # finding, see git log for this line).
            for code in [c for c in to_buy_next if c not in target_codes]:
                del to_buy_next[code]

            # drop pending codes that fell out of selection -- never bought this cycle
            for code in [c for c in pending if c not in target_codes]:
                del pending[code]
        else:
            target_codes = None

        # 0. fill anything confirmed on the previous trading day (and not
        # cancelled just above), at TODAY's price -- the D/D+1 timing
        # module docstring point 2 describes.
        for code in list(to_buy_next):
            px = prices.get(code, {}).get(date)
            if not px:
                continue  # no print today -- retry tomorrow, stays queued
            target_value = to_buy_next[code]
            buy_fill = px * (1 + slip)
            buy_shares = pb._floor_to_lot(target_value / (buy_fill * (1 + leg_fee)), lot_size)
            del to_buy_next[code]
            if buy_shares <= 0:
                continue
            cost = buy_shares * buy_fill * (1 + leg_fee)
            if cost <= cash:
                cash -= cost
                holdings[code] = holdings.get(code, 0) + buy_shares
                trades.append({"date": date, "code": code, "side": "buy", "price": buy_fill,
                                "shares": buy_shares, "gated": True})

        if date in rebalance_dates:
            for code in [c for c in holdings if c not in target_codes]:
                px = prices[code].get(date)
                if not px:
                    continue
                sell_fill = px * (1 - slip)
                proceeds = holdings[code] * sell_fill * (1 - leg_fee)
                cash += proceeds
                trades.append({"date": date, "code": code, "side": "sell", "price": sell_fill, "shares": holdings[code]})
                del holdings[code]

            port_value = pb._mark_to_market(cash, holdings, prices, bar_dates, date)
            if target_codes:
                target_value_each = port_value * (p["invest_pct"] / 100) / len(target_codes)
                for code in sorted(target_codes):
                    px = prices[code].get(date)
                    current_shares = holdings.get(code, 0)
                    if current_shares > 0:
                        # continuing holding -- resize exactly like baseline,
                        # no re-gating (only genuinely NEW entries are gated)
                        if not px:
                            continue
                        diff_value = target_value_each - current_shares * px
                        if diff_value > 0:
                            buy_fill = px * (1 + slip)
                            buy_shares = pb._floor_to_lot(diff_value / (buy_fill * (1 + leg_fee)), lot_size)
                            if buy_shares <= 0:
                                continue
                            cost = buy_shares * buy_fill * (1 + leg_fee)
                            if cost <= cash:
                                cash -= cost
                                holdings[code] = current_shares + buy_shares
                                trades.append({"date": date, "code": code, "side": "buy", "price": buy_fill, "shares": buy_shares})
                        elif diff_value < 0:
                            sell_fill = px * (1 - slip)
                            sell_shares = min(pb._floor_to_lot(-diff_value / px, lot_size), current_shares)
                            if sell_shares <= 0:
                                continue
                            cash += sell_shares * sell_fill * (1 - leg_fee)
                            holdings[code] = current_shares - sell_shares
                            trades.append({"date": date, "code": code, "side": "sell", "price": sell_fill, "shares": sell_shares})
                    elif code not in pending and code not in to_buy_next:
                        # genuinely new -- gate instead of buying now
                        pending[code] = {"target_value": target_value_each}
                    elif code in pending:
                        # still waiting from an earlier cycle -- refresh sizing
                        pending[code]["target_value"] = target_value_each

        # check today's breakout signal for every still-pending code; a
        # confirmation queues a fill for THE NEXT trading day (see module
        # docstring point 2), never today.
        for code in list(pending):
            bd = breakout_data.get(code)
            if not bd:
                continue  # no signal data for this code -- stays pending forever, never bought (see docstring)
            bdates = bd["bar_dates"]
            idx = ff._bar_index_on_or_before(bdates, date)
            if idx is None or bdates[idx] != date or idx < bs.WARMUP:
                continue  # no print today for this code's signal bars, or not enough warmup yet
            tech = bs.technical_signal(bd["ctx"], bd["highs"], bd["lows"], idx)
            cutoff = bdates[idx - 1] if idx > 0 else None
            inst_window = [r for r in bd["inst_rows"] if cutoff and r["date"] <= cutoff][-chip_days:] if cutoff else []
            chip = bs.chip_signal(inst_window, None)  # margin skipped, see docstring point 1
            if bs.entry_signal(tech, chip):
                to_buy_next[code] = pending[code]["target_value"]
                del pending[code]

        equity_curve.append({"date": date, "equity": pb._mark_to_market(cash, holdings, prices, bar_dates, date)})

    return {
        "equity_curve": equity_curve,
        "trades": trades,
        "summary": pb._compute_metrics(equity_curve, p["initial_capital"]),
    }


def sweep_entry_gate_walk_forward(universe_data, breakout_data, n_folds=4, purge_days=60, mode="anchored", sim_params=None):
    """Same fold/scoring/PBO-DSR shape as report_rebalance_freq_walkforward.
    sweep_rebalance_freq_walk_forward() -- see that script for why this
    isn't a generalization of optimize.sweep_ranking_weights_walk_forward()
    either (same "don't widen production infrastructure for an unproven
    candidate" reasoning)."""
    calendar = pb._trading_calendar(universe_data)
    folds = opt.purged_calendar_walk_forward_splits(calendar, n_folds=n_folds, purge_days=purge_days, mode=mode)
    if not folds:
        return None

    names = CANDIDATES
    scores_by_name = {name: [] for name in names}
    fold_argmax = []
    trials = []
    n_trials = 0
    for fold_idx, fold in enumerate(folds):
        train_start, _ = fold["train"]
        test_start, test_end = fold["test"]
        if mode == "anchored":
            sim_start = calendar[0]
        else:
            buffered = (dt.date.fromisoformat(train_start)
                        - dt.timedelta(days=opt.ROLLING_MOMENTUM_LOOKBACK_BUFFER_DAYS)).isoformat()
            sim_start = max(calendar[0], buffered)
        sliced = opt._slice_universe_by_date(universe_data, sim_start, test_end)

        fold_scores = {}
        fold_best_name, fold_best_score = None, float("-inf")
        for name in names:
            p = dict(sim_params or {})
            if name == names[0]:
                result = pb.simulate(sliced, params=p, ranks_cache={})
            else:
                result = simulate_with_entry_gate(sliced, breakout_data, params=p, ranks_cache={})
            oos_points = [pt for pt in result["equity_curve"] if test_start <= pt["date"] <= test_end]
            score = None
            if len(oos_points) >= 2 and oos_points[0]["equity"]:
                score = (oos_points[-1]["equity"] / oos_points[0]["equity"] - 1) * 100
            n_trials += 1
            trials.append({"fold_idx": fold_idx, "name": name, "score": score})
            fold_scores[name] = score
            score_for_argmax = score if score is not None else float("-inf")
            if score_for_argmax > fold_best_score:
                fold_best_name, fold_best_score = name, score_for_argmax
        if fold_best_name is not None:
            fold_argmax.append(fold_best_name)
            for name in names:
                scores_by_name[name].append(fold_scores[name])

    usable_folds = len(fold_argmax)
    if usable_folds == 0:
        return None

    mean_scores = {}
    for name in names:
        valid = [s for s in scores_by_name[name] if s is not None]
        mean_scores[name] = sum(valid) / len(valid) if valid else None
    valid_means = {n: s for n, s in mean_scores.items() if s is not None}
    point_best_name = max(valid_means, key=valid_means.get) if valid_means else None

    argmax_counts = {}
    for name in fold_argmax:
        argmax_counts[name] = argmax_counts.get(name, 0) + 1
    winner, winner_count = max(argmax_counts.items(), key=lambda kv: kv[1]) if argmax_counts else (None, 0)
    stability = winner_count / usable_folds if usable_folds else 0.0
    plateau = {"most_common": winner, "width_frac": stability}
    chance_threshold = max(opt.PLATEAU_WIDTH_MIN_FRAC, 1 / len(names))

    pbo_value, dsr_value = opt._pbo_and_dsr(scores_by_name, usable_folds, point_best_name, n_trials)

    return {
        "candidates": names,
        "plateau": plateau,
        "point_best": {"name": point_best_name, "score": valid_means.get(point_best_name)},
        "fold_argmax": fold_argmax,
        "candidate_scores": mean_scores,
        "dsr": dsr_value,
        "pbo": pbo_value,
        "n_trials": n_trials,
        "decision": opt.evaluate_decision(plateau, pbo_value, dsr_value, plateau_width_min_frac=chance_threshold),
        "trials": trials,
        "folds": [{"test": f["test"]} for f in folds],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--purge-days", type=int, default=60)
    ap.add_argument("--mode", choices=["anchored", "rolling"], default="anchored")
    args = ap.parse_args()

    universe_data = load_cached_universe()
    print(f"{len(universe_data)} codes loaded from cache/factor_run/")
    breakout_data = load_breakout_data()
    print(f"{len(breakout_data)} codes loaded from cache/breakout_layer/ (run scripts/fetch_breakout_layer_data.py first if 0)")
    if not universe_data or not breakout_data:
        return

    result = sweep_entry_gate_walk_forward(
        universe_data, breakout_data, n_folds=args.folds, purge_days=args.purge_days, mode=args.mode)
    if result is None:
        print("calendar too short for even one purged fold")
        return

    print(f"\n{args.folds} purged folds (mode={args.mode}, purge_days={args.purge_days})\n")
    for fold_idx, f in enumerate(result["folds"]):
        print(f"fold {fold_idx}: test={f['test'][0]}..{f['test'][1]}")
        for t in result["trials"]:
            if t["fold_idx"] == fold_idx:
                score = t["score"]
                print(f"  {t['name']:<32} score={score if score is None else round(score, 2)}")

    print(f"\nusable_folds={len(result['fold_argmax'])}")
    print("\nOOS mean return% per candidate (across usable folds):")
    for name in result["candidates"]:
        print(f"  {name:<32} {result['candidate_scores'][name]}")

    argmax_counts = {}
    for name in result["fold_argmax"]:
        argmax_counts[name] = argmax_counts.get(name, 0) + 1
    print(f"\nper-fold winner counts: {argmax_counts}")
    print(f"plateau: '{result['plateau']['most_common']}' won {result['plateau']['width_frac']:.0%} of usable folds")

    print(f"\nPBO (probability of overfitting): {result['pbo']}")
    print(f"DSR (deflated Sharpe of winner '{result['point_best']['name']}', n_trials={result['n_trials']}): {result['dsr']}")
    print(f"\ndecision gate (src/optimize.py's PBO_MAX={opt.PBO_MAX}, DSR_MIN_SIGNIFICANT={opt.DSR_MIN_SIGNIFICANT}):")
    if result["decision"]["accept"]:
        print("PASS -- clears both gates")
    else:
        print("REJECT:")
        for r in result["decision"]["reasons"]:
            print(f"  - {r}")


if __name__ == "__main__":
    main()
