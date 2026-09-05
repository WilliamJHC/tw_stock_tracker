# -*- coding: utf-8 -*-
"""Human-run application step for scripts/propose_regime_weights.py's
pending candidate (docs/adr/0004-regime-adaptive-strategy.md -- same manual-
review posture as scripts/apply_strategy_candidate.py/docs/adr/0003-manual-
review-for-strategy-logic-search.md; this is a brand-new logic type, not
RANKING_WEIGHTS' own periodic re-tuning, so it doesn't get ADR 0002's
auto-apply either).

>>> Run this via the "apply_regime_weights_candidate" GitHub Actions workflow
>>> (.github/workflows/apply_regime_weights_candidate.yml, workflow_dispatch
>>> -- trigger it from the repo's Actions tab, or `gh workflow run
>>> apply_regime_weights_candidate.yml -f run_id=... -f reject=true`), NOT by
>>> running this file directly on your own machine. db.connect() below
>>> (src/db.py) opens a local file when no path is given -- on a
>>> contributor's laptop that's an unrelated sqlite file, not the real
>>> backtest.db every scheduled pipeline reads/writes (see
>>> scripts/apply_strategy_candidate.py's own docstring for the full
>>> explanation -- same mechanism, same risk, this script just targets a
>>> different meta key).

Usage (inside the workflow above; direct CLI shown for local testing against
your own throwaway backtest.db only):
    python scripts/apply_regime_weights_candidate.py [--run-id RUN_ID] [--reject]

--run-id: optional; if given, must match meta.pending_regime_weights_candidate's
own run_id or nothing is done (guards against applying a stale candidate that's
already been superseded by a newer scripts/propose_regime_weights.py run).
Defaults to whatever is currently pending.

--reject: discards meta.pending_regime_weights_candidate without touching
meta.active_ranking_weights_by_regime.

Applying (no --reject): writes the candidate's weights_by_regime AS A WHOLE
into meta.active_ranking_weights_by_regime, replacing whatever was there --
NOT merged key-by-key with the previous value. A regime the candidate
skipped for insufficient sample size (scripts/propose_regime_weights.py) is
simply absent from weights_by_regime, so scripts/run_daily_recommendations.py
falls back to the flat meta.active_ranking_weights for that regime, same as
before this feature existed. Every included regime's weights are validated
against finlab_factors.REQUIRED_FACTOR_KEYS before anything is written --
one bad regime refuses the whole candidate rather than applying the other,
valid ones (a partially-applied candidate would be a silent surprise for
whoever reviewed the full candidate and approved it as one unit).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import db  # noqa: E402
import finlab_factors as ff  # noqa: E402

ACTIVE_META_KEY = "active_ranking_weights_by_regime"
PENDING_META_KEY = "pending_regime_weights_candidate"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", type=str, default=None,
                     help="核對是否與目前待審候選一致；預設讀取目前的 pending 候選")
    ap.add_argument("--reject", action="store_true", help="捨棄待審候選，不套用")
    args = ap.parse_args()

    conn = db.connect()
    db.init_db(conn)
    db.migrate(conn)

    pending = db.get_meta_json(conn, PENDING_META_KEY)
    if pending is None:
        print("沒有待審的盤勢權重候選（meta.pending_regime_weights_candidate 未設定）")
        conn.close()
        return

    if args.run_id and args.run_id != pending["run_id"]:
        print(f"--run-id={args.run_id} 與目前待審候選 run_id={pending['run_id']} 不一致，未執行任何動作")
        conn.close()
        return

    if args.reject:
        db.set_meta(conn, PENDING_META_KEY, "")
        print(f"已捨棄待審候選 run_id={pending['run_id']}")
        conn.close()
        return

    weights_by_regime = pending["weights_by_regime"]
    if not weights_by_regime:
        print("候選沒有任何通過樣本數門檻的盤勢，沒有東西可套用")
        conn.close()
        return

    for regime, weights in weights_by_regime.items():
        missing = ff.REQUIRED_FACTOR_KEYS - set(weights)
        if missing:
            print(f"候選 {regime} 的 weights 缺少必要因子 {sorted(missing)}，拒絕套用（不動任何 meta）")
            conn.close()
            return

    db.set_meta(conn, ACTIVE_META_KEY, json.dumps(weights_by_regime, ensure_ascii=False))
    db.set_meta(conn, PENDING_META_KEY, "")
    print(f"已套用候選 run_id={pending['run_id']}，涵蓋盤勢：{sorted(weights_by_regime)}")
    if pending.get("skipped_regimes"):
        print(f"未涵蓋（樣本不足，該盤勢沿用全域 active_ranking_weights）：{sorted(pending['skipped_regimes'])}")
    conn.close()


if __name__ == "__main__":
    main()
