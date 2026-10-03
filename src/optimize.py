# -*- coding: utf-8 -*-
"""Self-optimization framework: coordinate-descent search with purged
walk-forward validation, plateau detection, and Deflated Sharpe Ratio / PBO
overfitting guards. Pure functions calling into backtest.py's
replay_scores()/simulate() as the objective -- no I/O of its own. See
docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md
Phase 5, whose own framing is "防止自欺的工具，不是找最佳解的工具": every
function here exists to make it hard to fool yourself with a lucky
parameter pick, not to squeeze out the highest backtested number.

>>> Premise check, reported rather than silently glossed over <<<
The plan says this module should only be BUILT after Phase 4 (full-market
rank-IC + universe expansion), because applying an optimizer to the 3
factor weights / 9 score components before Phase 4's survivorship-bias-free
universe exists means optimizing on a biased sample. This was written while
Phase 4's full-market fetch was still running in the background -- the
framework itself doesn't depend on Phase 4's data (Phase 5's own
verification example optimizes backtest.STRATEGY_PARAMS' exit_composite on
the single-code watchlist backtest.db, which Phase 0-3 already completed),
so building/testing it then was safe. Phase 4 has since finished (2148/2148
codes, 1795 usable, rank-IC computed) -- sweep_ranking_weights_walk_forward()
below is the part that DOES need that data, and now has it: it runs the
purged walk-forward comparison against finlab_factors.RANKING_WEIGHTS
candidates derived from Phase 4's rank-IC/ICIR results (see
scripts/report_factor_ic.py's suggest_weights() output) instead of just
assuming the suggestion is an improvement.

>>> Simplification vs. the plan's literal PBO spec <<<
The plan describes PBO via CSCV with S=8 blocks. This module instead uses
ONE WALK-FORWARD FOLD's out-of-sample total_return as one "period" in the
returns_matrix metrics.pbo() consumes (n_blocks = number of usable folds,
typically 4) rather than re-bucketing into a fixed 8 calendar blocks -- the
folds are already contiguous, non-overlapping, purged time blocks, so this
reuses that structure instead of building a second, finer-grained
bucketing pass. Coarser than 8 blocks, but honest about it, not a silent
choice.
"""
import bisect
import datetime as dt
import statistics

import backtest as bt
import metrics as m
import portfolio_backtest as pb

# A trade can hold for up to STRATEGY_PARAMS["max_hold_days"] bars (120,
# post-Phase-3); a train fold ending inside that window could still have an
# open position bleeding into the test fold's own price action if not
# purged. See purged_walk_forward_splits().
PURGE_BARS_DEFAULT = 120

PLATEAU_WIDTH_MIN_FRAC = 0.2
PBO_MAX = 0.5
DSR_MIN_SIGNIFICANT = 0.95

# sweep_ranking_weights_walk_forward()'s mode="rolling" sim window needs to
# start this many CALENDAR days before a fold's train_start so that
# finlab_factors.momentum_pct()'s MOMENTUM_DAYS (60 TRADING bars) lookback is
# already satisfied at the fold's earliest rebalance dates -- otherwise those
# early rebalances see momentum_pct()/liquidity_ok() return None and
# composite_scores() silently drops otherwise-eligible codes. 120 calendar
# days is a generous margin over 60 trading days once weekends/holidays are
# counted (same order of magnitude as PURGE_BARS_DEFAULT above).
ROLLING_MOMENTUM_LOOKBACK_BUFFER_DAYS = 120


# ------------------------------------------------- walk-forward splitting --

def purged_walk_forward_splits(n_bars, n_folds=4, purge_bars=PURGE_BARS_DEFAULT, mode="anchored"):
    """[(train_range, test_range), ...] bar-index ranges over [0, n_bars).

    mode="anchored": train always starts at bar 0 and grows with each fold
    (matches backtest.STRATEGY_PARAMS' own walk-forward validation style).
    mode="rolling": train is a fixed-size trailing window immediately
    before the test fold instead.

    Either way, the `purge_bars` indices immediately before a test fold's
    start are EXCLUDED from that fold's train range: a trade opened there
    could still be open (up to max_hold_days bars later) once the test
    fold begins, so training on it would leak test-adjacent price action
    (docs/.../lighthouse.md Phase 5: "訓練/測試邊界必須 purge >=120 根，
    否則同一筆交易橫跨兩折 = 洩漏").

    Splits n_bars into n_folds+1 equal-ish contiguous blocks: block 0 seeds
    the first fold's training window, blocks 1..n_folds are each one
    fold's test period. Returns [] if there isn't enough history for even
    one honest (non-empty, purged) fold."""
    if n_folds < 1 or n_bars < (n_folds + 1) * max(purge_bars, 1) * 2:
        return []
    block = n_bars // (n_folds + 1)
    folds = []
    for k in range(1, n_folds + 1):
        test_start = k * block
        test_end = n_bars if k == n_folds else (k + 1) * block
        train_start = 0 if mode == "anchored" else max(0, test_start - block)
        train_end = max(train_start, test_start - purge_bars)
        if train_end <= train_start or test_end <= test_start:
            continue
        folds.append((range(train_start, train_end), range(test_start, test_end)))
    return folds


# --------------------------------------------------------- search engine --

def coordinate_descent(objective, param_grid, init=None, max_rounds=10):
    """Coordinate descent over `param_grid` ({name: [candidate values]})
    against `objective(params_dict) -> float | None` (higher is better;
    None means "couldn't be evaluated", treated as -inf so it never wins).

    Each round sweeps one dimension at a time, holding the others at their
    current best, moving to the next dimension only after finishing the
    current one -- O(rounds * sum(len(candidates))) evaluations, not
    O(prod(len(candidates))) like a full grid (docs/.../lighthouse.md
    Phase 5: "每輪只動一維，O(k·v) 而非 O(v^k)"). Stops early once a full
    round makes no improvement.

    Returns (best_params, trials) where trials is
    [{"params": {...}, "score": float|None}, ...] for EVERY trial
    evaluated, winners and losers alike -- DSR's n_trials must be the real
    count including discarded trials, or "少試就是少罰" doesn't hold
    (docs/.../lighthouse.md Phase 5)."""
    names = list(param_grid)
    if init is None:
        init = {name: param_grid[name][0] for name in names}
    current = dict(init)
    trials = []

    def _score(params):
        s = objective(params)
        trials.append({"params": dict(params), "score": s})
        return s if s is not None else float("-inf")

    best_score = _score(current)
    for _ in range(max_rounds):
        improved = False
        for name in names:
            best_val, best_val_score = current[name], best_score
            for candidate in param_grid[name]:
                if candidate == current[name]:
                    continue
                trial_params = dict(current, **{name: candidate})
                s = _score(trial_params)
                if s > best_val_score:
                    best_val, best_val_score = candidate, s
            if best_val != current[name]:
                current[name] = best_val
                best_score = best_val_score
                improved = True
        if not improved:
            break
    return current, trials


def random_restart(objective, param_grid, n_restarts, rng, max_rounds=10):
    """Runs coordinate_descent() from `n_restarts` different random
    starting points and keeps the best -- guards against a single run
    converging to a local optimum (docs/.../lighthouse.md Phase 5:
    "random restart 防局部解"). `rng` is a caller-supplied random.Random
    instance so results are reproducible/testable; this module never seeds
    its own randomness.

    Returns (best_params, best_score, trials) -- trials pooled across every
    restart, same "every trial, winners and losers" contract as
    coordinate_descent()."""
    names = list(param_grid)
    all_trials = []
    best_params, best_score = None, float("-inf")
    for _ in range(max(1, n_restarts)):
        init = {name: rng.choice(param_grid[name]) for name in names}
        params, trials = coordinate_descent(objective, param_grid, init=init, max_rounds=max_rounds)
        all_trials.extend(trials)
        score = next((t["score"] for t in reversed(trials) if t["params"] == params), None)
        if score is not None and score > best_score:
            best_params, best_score = params, score
    return best_params, best_score, all_trials


# -------------------------------------------------------- plateau report --

def detect_plateau(values, scores, tolerance_frac=0.1):
    """`values`: candidate values for ONE parameter dimension (any order).
    `scores`: parallel objective values (None entries dropped).

    Finds the WIDEST contiguous run of values (after sorting by value)
    whose score is within `tolerance_frac` of the full [min, max] score
    span, measured from the max -- i.e. score >= max - tolerance_frac *
    (max - min). Returns {"range": (lo, hi), "center": float,
    "width_frac": width / (max(values) - min(values))}, or None if fewer
    than 2 valid (value, score) pairs.

    docs/.../lighthouse.md Phase 5: "回報高原而非單點" -- the single best
    value chases noise near the top of a flat region; the plateau's
    CENTER is the more robust pick (see backtest.STRATEGY_PARAMS'
    exit_composite=-40, chosen exactly this way in Phase 3 over the
    noisier full-sample argmax=-30)."""
    pairs = sorted(((v, s) for v, s in zip(values, scores) if s is not None), key=lambda vs: vs[0])
    if len(pairs) < 2:
        return None
    vs_only = [v for v, _ in pairs]
    ss_only = [s for _, s in pairs]
    best_score, lo_score = max(ss_only), min(ss_only)
    span = best_score - lo_score
    threshold = best_score - tolerance_frac * span if span > 0 else best_score

    best_run, run_start = None, None
    for i, s in enumerate(ss_only + [None]):  # sentinel None closes a trailing run
        in_run = s is not None and s >= threshold
        if in_run and run_start is None:
            run_start = i
        elif not in_run and run_start is not None:
            run = (run_start, i - 1)
            if best_run is None or (vs_only[run[1]] - vs_only[run[0]]) > (vs_only[best_run[1]] - vs_only[best_run[0]]):
                best_run = run
            run_start = None
    if best_run is None:
        return None
    lo, hi = vs_only[best_run[0]], vs_only[best_run[1]]
    full_span = vs_only[-1] - vs_only[0]
    width_frac = (hi - lo) / full_span if full_span else 0.0
    return {"range": (lo, hi), "center": (lo + hi) / 2, "width_frac": width_frac}


def resolve_ranking_weights_action(decision, winner, has_previous):
    """Pure decision logic for scripts/run_ranking_optimize.py's scheduled
    run (docs/https-finlab-finance-blog-python-get-tai-memoized-lighthouse.md
    Phase 6d, docs/adr/0002-auto-apply-ranking-weights.md). Each scheduled
    cycle runs sweep_ranking_weights_walk_forward() over up to 3 named
    candidates -- "active" (whatever's live right now), "previous" (only
    present the cycle right after a switch -- what "active" replaced,
    Phase 6d's rollback target), "ic_suggested" (this cycle's fresh
    rank-IC-derived proposal, absent if no factor clears suggest_weights()'s
    bar) -- and this function turns that result's decision/winner into ONE
    of three actions.

    IMPORTANT: `winner` must be that result's plateau["most_common"] (the
    per-fold-argmax MAJORITY winner), NOT point_best["name"] (the highest-
    MEAN-OOS-return winner) -- the two can differ (one big-margin fold win
    can out-mean a candidate that lost every other fold by a little), and
    `decision` was computed by evaluate_decision(plateau, ...), which only
    validates plateau's own stability. Passing point_best's name here would
    let a candidate whose stability was never actually checked ride on a
    decision computed for a different one. See
    scripts/run_ranking_optimize.py's main() for the call site.

    "keep": nothing changes. Either no candidate cleared evaluate_decision()'s
    DSR/PBO/plateau gates (decision["accept"] is False -- absence of
    evidence isn't evidence FOR switching either), or "active" itself won
    (still the best-performing setting this cycle).

    "rollback_to_previous": "previous" won AND cleared the gates -- Phase
    6d's ADR: the setting "active" replaced has now demonstrably
    outperformed it on a fresh out-of-sample cycle, so revert.
    has_previous guards this from ever firing on a cycle where "previous"
    isn't even a real candidate (plateau["most_common"]'s name space is
    just the `candidates` dict's keys -- there is no scenario where
    winner=="previous" without has_previous being True in practice, but the
    caller not being trusted to only pass real winner names here keeps this
    function's contract self-contained).

    "adopt_suggested": "ic_suggested" won AND cleared the gates -- this
    cycle's fresh rank-IC proposal beat whatever's currently active."""
    if not decision["accept"]:
        return "keep"
    if winner == "previous" and has_previous:
        return "rollback_to_previous"
    if winner == "ic_suggested":
        return "adopt_suggested"
    return "keep"


def evaluate_decision(plateau, pbo_value, dsr_value, plateau_width_min_frac=PLATEAU_WIDTH_MIN_FRAC):
    """Applies docs/.../lighthouse.md Phase 5's three accept/reject rules.
    Returns {"accept": bool, "reasons": [str, ...]} -- reasons lists every
    rule that fired (rejection can have more than one cause). A rule whose
    input is None (couldn't be computed) does not fire -- absence of
    evidence isn't treated as evidence of overfitting.

    plateau_width_min_frac: overridable because sweep_ranking_weights_walk_
    forward() reuses "width_frac" for a DIFFERENT quantity -- the fraction
    of folds the winning NAMED candidate actually won (not a numeric
    plateau's width) -- whose chance baseline is 1/len(candidates), not a
    fixed fraction of a numeric search range; that caller passes a
    chance-adjusted threshold instead of the default."""
    reasons = []
    if plateau is None or plateau["width_frac"] < plateau_width_min_frac:
        reasons.append(f"plateau width < {plateau_width_min_frac:.0%} of search range -- looks like an overfit spike")
    if pbo_value is not None and pbo_value > PBO_MAX:
        reasons.append(f"PBO {pbo_value:.2f} > {PBO_MAX} -- overfitting probability too high")
    if dsr_value is not None and dsr_value < DSR_MIN_SIGNIFICANT:
        reasons.append(f"DSR {dsr_value:.2f} < {DSR_MIN_SIGNIFICANT} -- not significant after trial-count penalty")
    return {"accept": not reasons, "reasons": reasons}


# --------------------------------------------- backtest-engine integration --

def _pbo_and_dsr(scores_by_key, usable_folds, chosen_key, n_trials):
    """Shared overfitting-guard computation for both sweep_*_walk_forward()
    variants below: PBO across every candidate's per-fold OOS returns, and
    the Deflated Sharpe Ratio of `chosen_key`, which is whatever candidate
    the CALLER passes. Both callers pass the candidate the plateau rule
    selects, so the DSR describes the candidate that would actually be
    adopted: sweep_param_walk_forward() passes the candidate value nearest
    the plateau center (falling back to the point-best value when no
    plateau is found), and sweep_ranking_weights_walk_forward() passes the
    plateau's most-common per-fold winner (plateau["most_common"]) -- NOT
    point_best (highest MEAN out-of-sample return), which can be a different
    candidate. (Before 2026-10 the ranking-weights sweep passed point_best,
    so its DSR could describe a candidate other than the adopted one; see
    docs/plan_dsr_target_plateau_winner_2026-10.md.)

    scores_by_key: {key: [per-fold score%, ...]}, one entry per usable
    fold; a None score (candidate had no trades that fold) is treated as
    0% for both metrics -- matches this project's existing convention, see
    each caller. Returns (pbo_value, dsr_value), both None if
    usable_folds < 2; dsr_value is also None if chosen_key is None or its
    Sharpe couldn't be computed."""
    if usable_folds < 2:
        return None, None
    returns_matrix = {k: [(s if s is not None else 0.0) / 100 for s in scores]
                       for k, scores in scores_by_key.items()}
    pbo_value = m.pbo(returns_matrix, n_blocks=usable_folds)
    dsr_value = None
    if chosen_key is not None:
        chosen_returns = returns_matrix[chosen_key]
        sharpe = m.sharpe_ratio(chosen_returns, periods_per_year=usable_folds)
        trial_sharpes = [s for s in (
            m.sharpe_ratio(returns, periods_per_year=usable_folds) for returns in returns_matrix.values()
        ) if s is not None]
        trial_std = statistics.pstdev(trial_sharpes) if len(trial_sharpes) > 1 else 1.0
        if sharpe is not None:
            dsr_value = m.deflated_sharpe_ratio(sharpe, n_trials=n_trials, n_obs=usable_folds,
                                                trial_sharpe_std=trial_std or 1.0)
    return pbo_value, dsr_value


def sweep_param_walk_forward(bars, inst, margin, shareholding, sbl, rs_by_date,
                              param_name, candidate_values, base_params=None,
                              n_folds=4, mode="anchored", purge_bars=PURGE_BARS_DEFAULT):
    """Walk-forward sweep of ONE backtest.STRATEGY_PARAMS dimension across
    `candidate_values` -- the shape of Phase 5's own worked example
    (exit_composite). For each purged fold, every candidate is scored by
    backtest.simulate()'s total_return on that fold's OUT-OF-SAMPLE bars
    only (a single-dimension sweep has nothing to fit on train first --
    coordinate_descent()/random_restart() above are what a multi-dimension
    search would use train-fold objectives for).

    bars/inst/margin/shareholding/sbl/rs_by_date: same shape
    backtest.replay_scores() takes -- this function calls it exactly ONCE
    (over the full bar range) and reuses the result across every fold and
    candidate, since scoring doesn't depend on exit_composite/atr_mult/
    max_hold_days (only simulate() does).

    Returns None if there isn't enough history for even one purged fold
    (see purged_walk_forward_splits()) or no fold has any score rows.
    Otherwise the Phase 5 report shape: {"param_name", "plateau",
    "point_best", "fold_argmax", "dsr", "pbo", "n_trials", "vs_buy_hold",
    "decision", "trials"} -- "trials" is
    [{"fold_idx", "value", "score"}, ...] for every (fold, candidate) pair,
    meant for persisting via db.upsert_optimization_trials()."""
    base_params = dict(base_params or {})
    dates = [b["date"] for b in bars]
    folds = purged_walk_forward_splits(len(bars), n_folds=n_folds, purge_bars=purge_bars, mode=mode)
    if not folds:
        return None

    rows_by_date = {r["date"]: r for r in bt.replay_scores(bars, inst, margin, shareholding, sbl, rs_by_date=rs_by_date)}

    scores_by_value = {v: [] for v in candidate_values}
    fold_argmax = []
    trials = []
    n_trials = 0
    for fold_idx, (_, test_range) in enumerate(folds):
        test_score_rows = [rows_by_date[d] for d in (dates[i] for i in test_range) if d in rows_by_date]
        if not test_score_rows:
            continue
        # Bound simulate()'s "still open at data end" close to this fold's
        # own test range, not the whole (possibly multi-year) `bars` array --
        # otherwise a position left open at the fold boundary gets force-
        # closed at the LAST bar of the entire dataset, leaking years of
        # future price action into this fold's total_return. Mirrors
        # sweep_ranking_weights_walk_forward()'s _slice_universe_by_date()
        # call below, which bounds simulate() the same way for that path.
        fold_bars = bars[:test_range[-1] + 1]
        # Same fold_bars for every candidate value below -- build the index
        # once per fold instead of once per (fold, candidate) call.
        fold_bar_index = bt.build_bar_index(fold_bars)
        fold_scores = {}
        fold_best_value, fold_best_score = None, float("-inf")
        for value in candidate_values:
            params = dict(base_params, **{param_name: value})
            result = bt.simulate(test_score_rows, fold_bars, params=params, bar_index=fold_bar_index)
            total_return = result["summary"]["total_return"]
            n_trials += 1
            trials.append({"fold_idx": fold_idx, "value": value, "score": total_return})
            fold_scores[value] = total_return
            score_for_fold_argmax = total_return if total_return is not None else float("-inf")
            if score_for_fold_argmax > fold_best_score:
                fold_best_value, fold_best_score = value, score_for_fold_argmax
        # Only folded into scores_by_value/returns_matrix when the fold is
        # "usable" (fold_argmax gets an entry) -- otherwise a fold where every
        # candidate scored None would still push a 0.0 into every value's
        # series, making returns_matrix longer than usable_folds and silently
        # misaligning m.pbo()'s n_blocks with the actual row length below.
        if fold_best_value is not None:
            fold_argmax.append(fold_best_value)
            for value in candidate_values:
                scores_by_value[value].append(fold_scores[value])

    usable_folds = len(fold_argmax)
    if usable_folds == 0:
        return None

    mean_scores = {}
    for v in candidate_values:
        valid = [s for s in scores_by_value[v] if s is not None]
        mean_scores[v] = sum(valid) / len(valid) if valid else None

    plateau = detect_plateau(candidate_values, [mean_scores[v] for v in candidate_values])
    valid_means = {v: s for v, s in mean_scores.items() if s is not None}
    point_best_value = max(valid_means, key=valid_means.get) if valid_means else None
    point_best = {"value": point_best_value, "score": valid_means.get(point_best_value)}
    if plateau and point_best_value is not None and not (plateau["range"][0] <= point_best_value <= plateau["range"][1]):
        point_best["warning"] = "point best falls outside the detected plateau -- likely noise, prefer the plateau center"

    chosen_value = None
    if plateau is not None:
        chosen_value = min(candidate_values, key=lambda v: abs(v - plateau["center"]))
    elif point_best_value is not None:
        chosen_value = point_best_value

    pbo_value, dsr_value = _pbo_and_dsr(scores_by_value, usable_folds, chosen_value, n_trials)

    oos_start_idx, oos_end_idx = folds[0][1][0], folds[-1][1][-1]
    c0, c1 = bars[oos_start_idx]["c"], bars[oos_end_idx]["c"]
    buy_hold_return = (c1 / c0 - 1) * 100 if c0 else None

    return {
        "param_name": param_name,
        "plateau": plateau,
        "point_best": point_best,
        "fold_argmax": fold_argmax,
        "dsr": dsr_value,
        "pbo": pbo_value,
        "n_trials": n_trials,
        "vs_buy_hold": {
            "chosen_value": chosen_value,
            "chosen_oos_mean_return_pct": mean_scores.get(chosen_value),
            "buy_hold_return_pct": buy_hold_return,
        },
        "decision": evaluate_decision(plateau, pbo_value, dsr_value),
        "trials": trials,
    }


# ---------------------------------- portfolio-engine (RANKING_WEIGHTS) sweep --

def _slice_universe_by_date(universe_data, start_date, end_date):
    """universe_data restricted to bars within [start_date, end_date]
    (inclusive). revenue_series/roe_series are left untouched -- their own
    usable_date-gated finlab_factors._as_of() lookups already enforce
    point-in-time correctness independent of how the bars window is cut."""
    sliced = {}
    for code, d in universe_data.items():
        bars = [b for b in d["bars"] if start_date <= b["date"] <= end_date]
        if not bars:
            continue
        sliced[code] = {
            "bars": bars, "bar_dates": [b["date"] for b in bars],
            "revenue_series": d["revenue_series"], "roe_series": d["roe_series"],
        }
        # Candidate-factor series (value_pbr) are usable_date-gated by
        # _as_of() the same way, so pass them through untouched. Only when
        # present, so universes without one slice to the same shape as before.
        if "value_series" in d:
            sliced[code]["value_series"] = d["value_series"]
    return sliced


def purged_calendar_walk_forward_splits(calendar, n_folds=4, purge_days=60, mode="anchored"):
    """Like purged_walk_forward_splits() but over a trading CALENDAR (date
    strings) instead of bar indices -- portfolio_backtest.py's engine has
    no single bars array to index into (every code has its own). purge_days
    is calendar days, not bars; defaults to finlab_factors.FINANCIALS_LAG_DAYS
    (60), the longest point-in-time latency this engine's factors carry, so
    a fold boundary can't land inside a quarter's still-unpublished
    disclosure window.

    Returns [{"train": (start_date, end_date), "test": (start_date, end_date)},
    ...], or [] if there isn't enough calendar for even one purged fold."""
    n = len(calendar)
    if n_folds < 1 or n < (n_folds + 1) * 2:
        return []
    block = n // (n_folds + 1)
    folds = []
    for k in range(1, n_folds + 1):
        test_start_idx = k * block
        test_end_idx = n - 1 if k == n_folds else (k + 1) * block - 1
        if test_start_idx > test_end_idx:
            continue
        train_start_idx = 0 if mode == "anchored" else max(0, test_start_idx - block)
        test_start_dt = dt.date.fromisoformat(calendar[test_start_idx])
        purge_idx = test_start_idx - 1
        while purge_idx >= train_start_idx and (test_start_dt - dt.date.fromisoformat(calendar[purge_idx])).days < purge_days:
            purge_idx -= 1
        if purge_idx < train_start_idx:
            continue
        folds.append({
            "train": (calendar[train_start_idx], calendar[purge_idx]),
            "test": (calendar[test_start_idx], calendar[test_end_idx]),
        })
    return folds


def _days_in_month(year, month):
    """Number of calendar days in `year`-`month` -- helper for
    train_test_split_by_calendar()'s month-subtraction below (stdlib
    datetime has no built-in month delta; this avoids pulling in a new
    dependency like dateutil for a once-per-run calculation)."""
    next_month = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    return (next_month - dt.date(year, month, 1)).days


def train_test_split_by_calendar(calendar, test_months=6, purge_days=60):
    """calendar: ascending trading-date strings (portfolio_backtest.py's
    _trading_calendar() output). Carves off the LAST `test_months` calendar
    months as a held-out test window that never enters
    sweep_ranking_weights_walk_forward()'s search -- an extra guard on top
    of that function's own purged walk-forward folds, per the strategy-
    logic-search plan's train/test split (searching only ever touches
    train; test is spent exactly once, at the very end, to sanity-check
    the train-selected candidate against genuinely unseen data).

    The remaining earlier history becomes train, minus a `purge_days`-day
    gap immediately before the test window starts -- same purge rationale
    as purged_calendar_walk_forward_splits() above (a trade opened just
    before the boundary could still be open once the test window begins,
    so training on it would leak test-adjacent price action).

    Returns {"train": (start_date, end_date), "test": (start_date, end_date)}
    (date strings, inclusive), or None if there isn't enough calendar left
    for a non-empty, purged train segment once the test window is carved
    out (either the test window already swallows the whole calendar, or
    the purge gap eats what would have been train)."""
    if not calendar:
        return None
    end_dt = dt.date.fromisoformat(calendar[-1])
    # Subtract test_months calendar months, clamping day-of-month down when
    # the target month is shorter (e.g. Aug 31 - 6 months -> Feb 28/29).
    total_months = end_dt.year * 12 + (end_dt.month - 1) - test_months
    year, month = divmod(total_months, 12)
    month += 1
    day = min(end_dt.day, _days_in_month(year, month))
    test_start_dt = dt.date(year, month, day)

    test_start_idx = bisect.bisect_left(calendar, test_start_dt.isoformat())
    if test_start_idx >= len(calendar):
        return None

    purge_idx = test_start_idx - 1
    while purge_idx >= 0 and (test_start_dt - dt.date.fromisoformat(calendar[purge_idx])).days < purge_days:
        purge_idx -= 1
    if purge_idx < 0:
        return None

    return {
        "train": (calendar[0], calendar[purge_idx]),
        "test": (calendar[test_start_idx], calendar[-1]),
    }


def previous_candidate_eligible(active_weights_source, has_previous):
    """Pure decision logic for scripts/run_ranking_optimize.py's
    build_candidates(): should this cycle's sweep include "previous" (the
    RANKING_WEIGHTS rollback target) as a candidate at all?

    Coordination problem this guards against: the strategy-logic-search
    pipeline (scripts/apply_strategy_candidate.py) and this RANKING_WEIGHTS
    auto-optimize pipeline share the same meta.active_ranking_weights/
    previous_ranking_weights keys. If a human just approved a strategy-
    search candidate (meta.active_weights_source == "strategy_search"),
    the very next scheduled run_ranking_optimize.py cycle would otherwise
    see a real "previous" entry (whatever the human's candidate replaced)
    and could resolve_ranking_weights_action() its way into
    "rollback_to_previous" -- silently reverting a human decision this
    pipeline was never supposed to touch. Returning False here keeps
    "previous" out of that cycle's candidate pool entirely (ic_suggested is
    NOT affected -- it's still compared against "active" as normal), which
    protects exactly one cycle: scripts/run_ranking_optimize.py's own
    apply_action() unconditionally resets active_weights_source back to
    "ranking_optimize" at the end of THIS cycle regardless of what it
    concludes (adopt/rollback/keep alike), so this is not a permanent
    rollback-disable and doesn't linger across however many "keep" cycles
    happen to follow.

    has_previous=False (no previous candidate exists this cycle regardless
    of source) always returns False -- nothing to guard."""
    return has_previous and active_weights_source != "strategy_search"


def sweep_ranking_weights_walk_forward(universe_data, candidates, n_folds=4, purge_days=60,
                                        mode="anchored", sim_params=None, extra_factors=None):
    """Purged walk-forward comparison of NAMED finlab_factors.RANKING_WEIGHTS
    candidates -- e.g. the current fixed weights vs Phase 4's rank-IC-
    derived suggestions at a few horizons (docs/https-finlab-finance-blog-
    python-get-tai-memoized-lighthouse.md Phase 5, applied to Phase 4's
    results). This compares a handful of specific proposals, not a
    coordinate-descent grid search over the 3-dim weight space: Phase 4's
    IC/ICIR significance tests already did the "is there a real signal"
    check per factor, so re-searching continuous weights here would mostly
    relearn that same in-sample noise a second time.

    candidates: {name: {"revenue","roe","momentum", ...} weight dict} -- may
    include extra keys (e.g. "ma_cross"/"vol_price_div") beyond the
    original three when `extra_factors` is also given (see below);
    finlab_factors.composite_scores()'s generalized weighted sum just skips
    any weight key that isn't in a given ranks dict, so a candidate that
    doesn't use an extra factor can simply omit it from its own weight dict.

    extra_factors: forwarded to every fold's portfolio_backtest.simulate()
    call as params["extra_factors"] -- lets this sweep score candidate
    factors beyond the fixed revenue/roe/momentum trio (the strategy-logic
    search's ma_cross/vol_price_div) through this same purged walk-forward
    machinery instead of a parallel search engine. None (the default)
    reproduces the pre-existing behavior exactly.

    Each fold reruns portfolio_backtest.simulate() from the trading
    calendar's start (mode="anchored") or the fold's own train_start
    (mode="rolling") through the fold's test_end -- monthly rebalance
    state has to carry forward, there's no way to "resume" simulate() mid-
    history -- then scores each candidate by the OOS return realized ONLY
    within the fold's test window (the equity curve segment between
    test_start and test_end), not the whole run.

    Returns None if there isn't enough calendar for even one purged fold.
    Otherwise: {"candidates", "plateau", "point_best", "fold_argmax",
    "candidate_scores", "dsr", "dsr_of", "pbo", "n_trials", "vs_buy_hold",
    "decision", "trials"}. "dsr_of" names the candidate "dsr" was computed
    for (== plateau["most_common"]). "plateau" here means "how many folds did the overall winner
    actually win" (a numeric plateau range doesn't apply to named
    candidates) -- its accept/reject threshold is chance-adjusted to
    max(PLATEAU_WIDTH_MIN_FRAC, 1/len(candidates)), since e.g. a 2-candidate
    race has a 50% win-by-chance floor that a fixed 20% bar would let
    through. "candidate_scores" is the raw {name: mean OOS return%} for
    every candidate, in case the winner isn't obviously the plateau."""
    calendar = pb._trading_calendar(universe_data)
    folds = purged_calendar_walk_forward_splits(calendar, n_folds=n_folds, purge_days=purge_days, mode=mode)
    if not folds:
        return None

    names = list(candidates)
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
                        - dt.timedelta(days=ROLLING_MOMENTUM_LOOKBACK_BUFFER_DAYS)).isoformat()
            sim_start = max(calendar[0], buffered)
        sliced = _slice_universe_by_date(universe_data, sim_start, test_end)
        # One factor_ranks() memoization cache per FOLD, shared across this
        # fold's `names` loop below -- every named candidate scores against
        # the same `sliced` universe/extra_factors this fold, and
        # finlab_factors.factor_ranks() doesn't depend on which candidate's
        # weights are being tried, so without this cache the identical
        # liquidity-filter + percentile-rank pass gets redone once per
        # candidate at every rebalance date (see portfolio_backtest.
        # simulate()'s ranks_cache docstring). Scoped to THIS fold, not
        # shared across folds -- mode="rolling" slices `sliced` differently
        # per fold (different sim_start), so a value cached under one
        # fold's `sliced` could reflect a different lookback window than
        # another fold's for the same date string; a fresh {} per fold
        # avoids ever reusing a value computed against the wrong slice.
        ranks_cache = {}
        fold_scores = {}
        fold_best_name, fold_best_score = None, float("-inf")
        for name in names:
            p = dict(sim_params or {})
            p["ranking_weights"] = candidates[name]
            p["extra_factors"] = extra_factors
            result = pb.simulate(sliced, params=p, ranks_cache=ranks_cache)
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
        # See sweep_param_walk_forward()'s matching comment: only fold into
        # scores_by_name/returns_matrix on a usable fold, so its length stays
        # aligned with usable_folds (n_blocks passed to m.pbo() below).
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
    chance_threshold = max(PLATEAU_WIDTH_MIN_FRAC, 1 / len(names)) if names else PLATEAU_WIDTH_MIN_FRAC

    # The DSR is computed for the plateau winner -- the same candidate
    # run_ranking_optimize.py / run_strategy_search.py would adopt -- so the
    # gate's three rules all describe one candidate. point_best_name can
    # differ; it only feeds "point_best" and "vs_buy_hold" below.
    pbo_value, dsr_value = _pbo_and_dsr(scores_by_name, usable_folds, winner, n_trials)

    oos_start, oos_end = folds[0]["test"][0], folds[-1]["test"][1]
    buy_hold_returns = []
    for d in universe_data.values():
        prices = {b["date"]: b["c"] for b in d["bars"]}
        dates_in_range = sorted(dd for dd in prices if oos_start <= dd <= oos_end)
        if len(dates_in_range) >= 2 and prices[dates_in_range[0]]:
            buy_hold_returns.append((prices[dates_in_range[-1]] / prices[dates_in_range[0]] - 1) * 100)
    universe_buy_hold = sum(buy_hold_returns) / len(buy_hold_returns) if buy_hold_returns else None

    return {
        "candidates": names,
        "plateau": plateau,
        "point_best": {"name": point_best_name, "score": valid_means.get(point_best_name)},
        "fold_argmax": fold_argmax,
        "candidate_scores": mean_scores,
        "dsr": dsr_value,
        "dsr_of": winner,
        "pbo": pbo_value,
        "n_trials": n_trials,
        "vs_buy_hold": {
            "chosen_name": point_best_name,
            "chosen_oos_mean_return_pct": mean_scores.get(point_best_name),
            "universe_mean_buy_hold_return_pct": universe_buy_hold,
        },
        "decision": evaluate_decision(plateau, pbo_value, dsr_value, plateau_width_min_frac=chance_threshold),
        "trials": trials,
    }
