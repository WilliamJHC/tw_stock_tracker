# -*- coding: utf-8 -*-
"""Follow-up diagnostics for the high250 candidate (docs/factor_candidates_
high250_2026-10.md §4). Descriptive only -- NOT a decision rule, nothing is
applied, no DB writes, no Telegram. Candidates are the same three
run_strategy_search.build_high250_candidates() uses (baseline / high250 /
mom_high250_half), weights fixed up front.

Three sections (pick with flags; default runs all):

  --phase     Rebalance-phase sensitivity. The monthly engine always
              rebalances on the FIRST trading day of a month, so "month
              phase" has no meaning for k=1; the only phase is the day within
              the month. Re-runs the same purged anchored 4-fold race with
              the rebalance date set to the 1st / 6th / 11th / 16th trading
              day of each month (offsets 0/5/10/15, fixed up front).
              Temporarily replaces portfolio_backtest._rebalance_dates in
              this process only.
  --marginal  Marginal contribution of high250 over the existing momentum
              factor, on the model's own percentile-rank space (monthly
              first-trading-day dates, eligible set = codes with all of
              revenue/roe/momentum/high250): rank correlation, rank-IC of
              momentum / high250 / each residualized on the other, a
              Fama-MacBeth rank regression of next-month return rank on the
              four factor ranks, and composite-score rank-IC per candidate.
  --nomom     Control for "is the gain just from dropping momentum?": a
              no_momentum candidate (revenue/roe only, momentum weight 0)
              raced against baseline and high250 (default phase), and its
              composite rank-IC is included in --marginal's output.
  --turnover  Top-20 month-to-month retention, plus full-train engine
              simulation (annual traded notional / mean equity, cost drag,
              CAGR) at the engine's built-in costs and at 2x costs.

TRAIN segment only (optimize.train_test_split_by_calendar); the held-out last
6 months are never touched. Overlapping h=60 numbers are printed with their
t-stats for reference, but those t-stats are inflated (3x overlap, no
Newey-West).

Usage (from the project root; needs cache/factor_run/):
    python scripts/report_high250_followups.py [--phase] [--marginal] [--nomom] [--turnover]
"""
import argparse
import datetime as dt
import math
import os
import statistics as st
import sys

import numpy as np
from scipy import stats as sps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import finlab_factors as ff  # noqa: E402
import optimize as opt  # noqa: E402
import portfolio_backtest as pb  # noqa: E402
import report_factor_ic as rfi  # noqa: E402
import run_strategy_search as rss  # noqa: E402

PHASE_OFFSETS = (0, 5, 10, 15)
STATS_START = "2013-07-01"  # first date every candidate can have >= 250 bars of history
NAMES = ("baseline", "high250", "mom_high250_half")
IC_NAMES = NAMES + ("no_momentum",)
FACTOR_KEYS = ("revenue", "roe", "momentum", "high250")  # column order of the rank matrix


def _t(values):
    """Mean / standard error, nan if fewer than 2 points or zero variance."""
    if len(values) < 2:
        return float("nan")
    sd = st.stdev(values)
    return st.mean(values) / (sd / math.sqrt(len(values))) if sd > 0 else float("nan")


def _month_firsts(universe_data, start="2013-01-01"):
    firsts = {}
    for d in sorted({b for u in universe_data.values() for b in u["bar_dates"]}):
        firsts.setdefault(d[:7], d)
    return [firsts[m] for m in sorted(firsts) if firsts[m] >= start]


def _fwd(universe_data, code, date, target, tol=10):
    d = universe_data[code]
    i = ff._bar_index_on_or_before(d["bar_dates"], date)
    j = ff._bar_index_on_or_before(d["bar_dates"], target)
    if i is None or j is None or j <= i:
        return None
    if (dt.date.fromisoformat(target) - dt.date.fromisoformat(d["bar_dates"][j])).days > tol:
        return None
    p0, p1 = ff._close(d["bars"][i]), ff._close(d["bars"][j])
    return p1 / p0 - 1 if p0 else None


def _fwd_array(universe_data, codes, date, target):
    """Forward return per code as a float array, nan where unavailable (a
    genuine 0.0 return stays 0.0)."""
    out = []
    for c in codes:
        v = _fwd(universe_data, c, date, target)
        out.append(np.nan if v is None else v)
    return np.array(out, dtype=float)


def _with_no_momentum(candidates):
    """Same dict plus "no_momentum": baseline weights with momentum dropped
    (composite_scores() just skips the missing key)."""
    out = dict(candidates)
    out["no_momentum"] = {k: v for k, v in candidates["baseline"].items() if k != "momentum"}
    return out


def _setup():
    universe_data = rfi.load_cached_universe()
    print(f"{len(universe_data)} codes loaded from {rfi.RUN_CACHE_DIR}")
    if not universe_data:
        raise SystemExit("nothing cached -- run scripts/run_factor_backtest.py --universe all first")
    calendar = pb._trading_calendar(universe_data)
    split = opt.train_test_split_by_calendar(calendar)
    if split is None:
        raise SystemExit("not enough calendar for a train/test split")
    train_start, train_end = split["train"]
    print(f"train={train_start}..{train_end}  (held-out test {split['test'][0]}..{split['test'][1]} untouched)")
    candidates = rss.build_high250_candidates(dict(ff.RANKING_WEIGHTS))
    extra = ff.used_extra_factors(candidates.values())
    return universe_data, train_start, train_end, candidates, extra


# ------------------------------------------------------------------ phase --

def run_phase(universe_data, train_start, train_end, candidates, extra):
    train_universe = opt._slice_universe_by_date(universe_data, train_start, train_end)
    orig = pb._rebalance_dates

    def make_nth(off):
        def nth(calendar, every_months=1, offset_months=0, freq="monthly"):
            by_month = {}
            for d in calendar:
                by_month.setdefault(d[:7], []).append(d)
            # A month with <= off trading days in this slice (a fold's last,
            # partial month) is skipped rather than falling back to its last
            # bar, which would add a spurious extra rebalance. off=0 is
            # identical to the engine's default first-trading-day rule.
            return [days[off] for _, days in sorted(by_month.items()) if len(days) > off]
        return nth

    print("\n== 換倉日相位（月內第 N 個交易日）：各 fold 樣本外報酬 % ==")
    summary = []
    try:
        for off in PHASE_OFFSETS:
            pb._rebalance_dates = make_nth(off)
            result = opt.sweep_ranking_weights_walk_forward(train_universe, candidates, extra_factors=extra)
            if result is None:
                print(f"offset {off}: not enough history")
                continue
            by_fold = {}
            for t in result["trials"]:
                by_fold.setdefault(t["fold_idx"], {})[t["name"]] = t["score"]
            print(f"-- 月內第 {off + 1} 個交易日 --")
            for f in sorted(by_fold):
                print("  fold %d: " % f + "  ".join(f"{n}={by_fold[f][n]:+7.1f}" for n in NAMES))
            wins = sum(by_fold[f]["high250"] > by_fold[f]["baseline"] for f in by_fold)
            print("  mean: " + "  ".join(f"{n}={result['candidate_scores'][n]:+7.1f}" for n in NAMES)
                  + f"  | high250 勝 baseline {wins}/{len(by_fold)} fold"
                  + f"  fold_argmax={result['fold_argmax']} pbo={result['pbo']:.2f} dsr={result['dsr']:.3f}"
                  + f" (dsr_of={result.get('dsr_of')})")
            summary.append((off, result["candidate_scores"], wins, len(by_fold), result["dsr"]))
    finally:
        pb._rebalance_dates = orig
    print("\n相位彙整：high250 平均 − baseline 平均（百分點）／勝 fold 數／DSR")
    for off, sc, wins, n, dsr in summary:
        print(f"  第 {off + 1:2d} 個交易日: {sc['high250'] - sc['baseline']:+6.1f}  {wins}/{n}  dsr={dsr:.3f}")


def run_nomom(universe_data, train_start, train_end, candidates, extra):
    train_universe = opt._slice_universe_by_date(universe_data, train_start, train_end)
    race = {n: candidates[n] for n in ("baseline", "high250")}
    race["no_momentum"] = _with_no_momentum(candidates)["no_momentum"]
    names = tuple(race)
    result = opt.sweep_ranking_weights_walk_forward(train_universe, race, extra_factors=extra)
    if result is None:
        print("not enough history")
        return
    by_fold = {}
    for t in result["trials"]:
        by_fold.setdefault(t["fold_idx"], {})[t["name"]] = t["score"]
    print("\n== 對照：直接拿掉動能（不加 high250）, 預設相位 ==")
    for f in sorted(by_fold):
        print("  fold %d: " % f + "  ".join(f"{n}={by_fold[f][n]:+7.1f}" for n in names))
    print("  mean: " + "  ".join(f"{n}={result['candidate_scores'][n]:+7.1f}" for n in names)
          + f"\n  fold_argmax={result['fold_argmax']} pbo={result['pbo']:.2f} dsr={result['dsr']:.3f}"
          + f" (dsr_of={result.get('dsr_of')}) n_trials={result['n_trials']}")


# ---------------------------------------------------------------- marginal --

def _month_panel(universe_data, train_end, extra):
    """[{date, codes, R (n x 4 rank matrix: revenue/roe/momentum/high250),
    comp {name: scores array}, f1, f3 (arrays, nan where unavailable)}]."""
    reb = _month_firsts(universe_data)
    candidates = _with_no_momentum(rss.build_high250_candidates(dict(ff.RANKING_WEIGHTS)))
    panel = []
    for k, date in enumerate(reb):
        if k + 1 >= len(reb) or reb[k + 1] > train_end:
            break
        ranks = ff.factor_ranks(universe_data, date, extra_factors=extra)
        if len(ranks) < 50:
            continue
        codes = sorted(ranks)
        R = np.array([[ranks[c]["revenue"], ranks[c]["roe"], ranks[c]["momentum"], ranks[c]["high250"]] for c in codes])
        comp = {n: R @ np.array([w.get(k, 0.0) for k in FACTOR_KEYS]) for n, w in candidates.items()}
        f1 = _fwd_array(universe_data, codes, date, reb[k + 1])
        t3 = reb[k + 3] if k + 3 < len(reb) and reb[k + 3] <= train_end else None
        f3 = _fwd_array(universe_data, codes, date, t3) if t3 else np.full(len(codes), np.nan)
        panel.append({"date": date, "codes": codes, "R": R, "comp": comp, "f1": f1, "f3": f3})
    return panel


def _spearman(x, y):
    m = ~np.isnan(y)
    if m.sum() < 30:
        return float("nan")
    return float(sps.spearmanr(x[m], y[m])[0])


def _resid(y, x):
    X = np.column_stack([np.ones(len(x)), x])
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    return y - X @ beta


def run_marginal(universe_data, train_end, extra):
    panel = _month_panel(universe_data, train_end, extra)
    print(f"\n== 與現行動能的邊際貢獻（{len(panel)} 個月，{panel[0]['date']} ~ {panel[-1]['date']}）==")
    for h in ("f1", "f3"):
        label = "下個月" if h == "f1" else "三個月（重疊，t 偏高）"
        corr, ic_mom, ic_high, ic_hres, ic_mres = [], [], [], [], []
        comp_ic = {n: [] for n in IC_NAMES}
        fm = []
        for m in panel:
            y = m[h]
            ok = ~np.isnan(y)
            if ok.sum() < 30:
                continue
            mom, high = m["R"][:, 2], m["R"][:, 3]
            corr.append(float(sps.spearmanr(mom, high)[0]))
            ic_mom.append(_spearman(mom, y))
            ic_high.append(_spearman(high, y))
            ic_hres.append(_spearman(_resid(high, mom), y))
            ic_mres.append(_spearman(_resid(mom, high), y))
            for n in IC_NAMES:
                comp_ic[n].append(_spearman(m["comp"][n], y))
            yr = sps.rankdata(y[ok]) / ok.sum()
            X = np.column_stack([np.ones(ok.sum()), m["R"][ok]])
            fm.append(np.linalg.lstsq(X, yr, rcond=None)[0])
        print(f"\n[{label}] n={len(corr)} 個月")
        print(f"  動能排名與 high250 排名的橫截面 Spearman 相關：平均 {st.mean(corr):.3f}")

        def line(name, v):
            v = [x for x in v if not math.isnan(x)]
            print(f"  rank-IC {name:28} mean {st.mean(v):+.4f}  t={_t(v):+.2f}  ICIR={st.mean(v) / st.stdev(v):+.3f}")
        line("動能", ic_mom)
        line("high250", ic_high)
        line("high250 去掉動能後的殘差", ic_hres)
        line("動能 去掉 high250 後的殘差", ic_mres)
        for n in IC_NAMES:
            line(f"綜合分數 {n}", comp_ic[n])
        for a_name, b_name in (("high250", "baseline"), ("mom_high250_half", "baseline"),
                               ("no_momentum", "baseline"), ("high250", "no_momentum")):
            d = [a - b for a, b in zip(comp_ic[a_name], comp_ic[b_name]) if not (math.isnan(a) or math.isnan(b))]
            print(f"  綜合 IC 差（{a_name} − {b_name}）: mean {st.mean(d):+.4f}  t={_t(d):+.2f}")
        fm = np.array(fm)
        print("  Fama-MacBeth（報酬排名 ~ 營收+ROE+動能+high250 排名）係數：")
        for i, nm in enumerate(("截距", "營收", "ROE", "動能", "high250")):
            print(f"    {nm:8} mean {fm[:, i].mean():+.4f}  t={_t(list(fm[:, i])):+.2f}")


# ---------------------------------------------------------------- turnover --

def _engine_stats(train_universe, params, start):
    result = pb.simulate(train_universe, params=params)
    curve = [pt for pt in result["equity_curve"] if pt["date"] >= start]
    trades = [t for t in result["trades"] if t["date"] >= start]
    if len(curve) < 2:
        return {}
    p = dict(pb.PORTFOLIO_PARAMS, **params)
    leg_cost = p["fee_pct"] / 2 / 100 + p["slippage_pct"] / 100
    years = (dt.date.fromisoformat(curve[-1]["date"]) - dt.date.fromisoformat(curve[0]["date"])).days / 365.25
    mean_eq = sum(pt["equity"] for pt in curve) / len(curve)
    notional = sum(t["price"] * t["shares"] for t in trades)
    first, last = curve[0]["equity"], curve[-1]["equity"]
    return {
        "trades_per_year": len(trades) / years,
        "traded_notional_x_equity_per_year": notional / mean_eq / years,
        "cost_drag_pct_per_year": notional * leg_cost / mean_eq / years * 100,
        "cagr_pct": ((last / first) ** (1 / years) - 1) * 100 if first > 0 and last > 0 else None,
    }


def run_turnover(universe_data, train_start, train_end, candidates, extra):
    print("\n== 換手率 ==")
    reb = [d for d in _month_firsts(universe_data) if d <= train_end]
    tops = {n: [] for n in NAMES}
    for date in reb:
        ranks = ff.factor_ranks(universe_data, date, extra_factors=extra)
        for n in NAMES:
            sc = ff.composite_scores(None, date, weights=candidates[n], ranks=ranks) if len(ranks) >= 50 else {}
            tops[n].append(set(ff.top_n(sc, 20)) if sc else None)
    print("前 20 名月對月留存率（上月持股本月仍在前 20 的比例）：")
    for n in NAMES:
        r = [len(a & b) / 20 for a, b in zip(tops[n], tops[n][1:]) if a and b]
        print(f"  {n:18} 平均 {st.mean(r) * 100:5.1f}%  （{len(r)} 個月）")

    train_universe = opt._slice_universe_by_date(universe_data, train_start, train_end)
    print(f"\n全訓練段引擎模擬（{STATS_START} 起，前期無 250 根歷史為空倉故略過）：")
    for label, extra_cost in (("引擎內建成本", {}), ("成本 ×2", {"fee_pct": pb.PORTFOLIO_PARAMS["fee_pct"] * 2,
                                                          "slippage_pct": pb.PORTFOLIO_PARAMS["slippage_pct"] * 2})):
        print(f"-- {label} --")
        for n in NAMES:
            params = {"ranking_weights": candidates[n], "extra_factors": extra, **extra_cost}
            s = _engine_stats(train_universe, params, STATS_START)
            print(f"  {n:18} 年成交筆數 {s['trades_per_year']:6.0f}  年成交額/平均淨值 {s['traded_notional_x_equity_per_year']:5.2f}x"
                  f"  成本拖累 {s['cost_drag_pct_per_year']:5.2f}%/年  CAGR {s['cagr_pct']:6.1f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", action="store_true")
    ap.add_argument("--marginal", action="store_true")
    ap.add_argument("--nomom", action="store_true")
    ap.add_argument("--turnover", action="store_true")
    args = ap.parse_args()
    run_all = not (args.phase or args.marginal or args.nomom or args.turnover)

    universe_data, train_start, train_end, candidates, extra = _setup()
    if args.phase or run_all:
        run_phase(universe_data, train_start, train_end, candidates, extra)
    if args.marginal or run_all:
        run_marginal(universe_data, train_end, extra)
    if args.nomom or run_all:
        run_nomom(universe_data, train_start, train_end, candidates, extra)
    if args.turnover or run_all:
        run_turnover(universe_data, train_start, train_end, candidates, extra)


if __name__ == "__main__":
    main()
