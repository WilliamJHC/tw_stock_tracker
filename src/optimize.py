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
universe exists means optimizing on a biased sample. Phase 4's full-market
fetch was still running in the background when this was written. The
framework itself doesn't depend on Phase 4's data, though -- Phase 5's own
verification example optimizes backtest.STRATEGY_PARAMS' exit_composite on
the single-code watchlist backtest.db, which Phase 0-3 already completed --
so building/testing it now is safe. Actually RUNNING it against
finlab_factors.RANKING_WEIGHTS or analysis.SCORE_WEIGHTS should wait for
Phase 4's full-market data (scripts/run_optimize.py's docstring repeats
this caveat at the point someone would actually do that).

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
import statistics

import backtest as bt
import metrics as m

# A trade can hold for up to STRATEGY_PARAMS["max_hold_days"] bars (120,
# post-Phase-3); a train fold ending inside that window could still have an
# open position bleeding into the test fold's own price action if not
# purged. See purged_walk_forward_splits().
PURGE_BARS_DEFAULT = 120

PLATEAU_WIDTH_MIN_FRAC = 0.2
PBO_MAX = 0.5
DSR_MIN_SIGNIFICANT = 0.95


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


def evaluate_decision(plateau, pbo_value, dsr_value):
    """Applies docs/.../lighthouse.md Phase 5's three accept/reject rules.
    Returns {"accept": bool, "reasons": [str, ...]} -- reasons lists every
    rule that fired (rejection can have more than one cause). A rule whose
    input is None (couldn't be computed) does not fire -- absence of
    evidence isn't treated as evidence of overfitting."""
    reasons = []
    if plateau is None or plateau["width_frac"] < PLATEAU_WIDTH_MIN_FRAC:
        reasons.append(f"plateau width < {PLATEAU_WIDTH_MIN_FRAC:.0%} of search range -- looks like an overfit spike")
    if pbo_value is not None and pbo_value > PBO_MAX:
        reasons.append(f"PBO {pbo_value:.2f} > {PBO_MAX} -- overfitting probability too high")
    if dsr_value is not None and dsr_value < DSR_MIN_SIGNIFICANT:
        reasons.append(f"DSR {dsr_value:.2f} < {DSR_MIN_SIGNIFICANT} -- not significant after trial-count penalty")
    return {"accept": not reasons, "reasons": reasons}


# --------------------------------------------- backtest-engine integration --

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
        fold_best_value, fold_best_score = None, float("-inf")
        for value in candidate_values:
            params = dict(base_params, **{param_name: value})
            result = bt.simulate(test_score_rows, bars, params=params)
            total_return = result["summary"]["total_return"]
            n_trials += 1
            trials.append({"fold_idx": fold_idx, "value": value, "score": total_return})
            scores_by_value[value].append(total_return)
            score_for_fold_argmax = total_return if total_return is not None else float("-inf")
            if score_for_fold_argmax > fold_best_score:
                fold_best_value, fold_best_score = value, score_for_fold_argmax
        if fold_best_value is not None:
            fold_argmax.append(fold_best_value)

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

    pbo_value = dsr_value = None
    if usable_folds >= 2:
        returns_matrix = {v: [(s if s is not None else 0.0) / 100 for s in scores_by_value[v]] for v in candidate_values}
        pbo_value = m.pbo(returns_matrix, n_blocks=usable_folds)
        if chosen_value is not None:
            chosen_returns = returns_matrix[chosen_value]
            sharpe = m.sharpe_ratio(chosen_returns, periods_per_year=usable_folds)
            trial_sharpes = [s for s in (
                m.sharpe_ratio(returns_matrix[v], periods_per_year=usable_folds) for v in candidate_values
            ) if s is not None]
            trial_std = statistics.pstdev(trial_sharpes) if len(trial_sharpes) > 1 else 1.0
            if sharpe is not None:
                dsr_value = m.deflated_sharpe_ratio(sharpe, n_trials=n_trials, n_obs=usable_folds,
                                                    trial_sharpe_std=trial_std or 1.0)

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
