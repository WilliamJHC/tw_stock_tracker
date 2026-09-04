# -*- coding: utf-8 -*-
"""Human-run application step for scripts/run_strategy_search.py's
pending candidate (docs/adr/0003-manual-review-for-strategy-logic-search.md)
-- the strategy-logic search pipeline deliberately never applies its own
winner; a person reads the Telegram summary/printed report and runs this
script to either apply it or throw it away.

Usage:
    python scripts/apply_strategy_candidate.py [--run-id RUN_ID] [--reject]

--run-id: optional; if given, must match meta.pending_strategy_candidate's
own run_id or nothing is done (guards against applying a stale candidate
you think you're looking at but that's actually already been superseded by
a newer scheduled run). Defaults to whatever is currently pending.

--reject: discards meta.pending_strategy_candidate without touching
meta.active_ranking_weights/previous_ranking_weights/active_weights_source.

Applying (no --reject): moves the current active_ranking_weights to
previous_ranking_weights (the same rollback-target key
scripts/run_ranking_optimize.py's own auto-optimize pipeline uses -- there
is only ever one "active" for the whole app, whichever pipeline last
touched it), writes the candidate's weights in as the new
active_ranking_weights, and sets active_weights_source="strategy_search".
That last write is the coordination mechanism ADR 0003 documents: it makes
src/optimize.py's previous_candidate_eligible() return False for
scripts/run_ranking_optimize.py's very next scheduled cycle, so that
pipeline doesn't see this newly-approved candidate's predecessor sitting in
"previous" and roll it back on its own. run_ranking_optimize.py resets the
source back to "ranking_optimize" itself the next time IT changes active
(adopt or rollback) -- this protection is one cycle, not permanent.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import db  # noqa: E402
import finlab_factors as ff  # noqa: E402

ACTIVE_META_KEY = "active_ranking_weights"
PREVIOUS_META_KEY = "previous_ranking_weights"
SOURCE_META_KEY = "active_weights_source"
PENDING_META_KEY = "pending_strategy_candidate"


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
        print("沒有待審的策略搜尋候選（meta.pending_strategy_candidate 未設定）")
        conn.close()
        return

    if args.run_id and args.run_id != pending["run_id"]:
        print(f"--run-id={args.run_id} 與目前待審候選 run_id={pending['run_id']} 不一致，未執行任何動作")
        conn.close()
        return

    if args.reject:
        db.set_meta(conn, PENDING_META_KEY, "")
        print(f"已捨棄待審候選 run_id={pending['run_id']} candidate_name={pending['candidate_name']}")
        conn.close()
        return

    active = db.get_meta_json(conn, ACTIVE_META_KEY, default=dict(ff.RANKING_WEIGHTS))
    db.set_meta(conn, PREVIOUS_META_KEY, json.dumps(active))
    db.set_meta(conn, ACTIVE_META_KEY, json.dumps(pending["weights"]))
    db.set_meta(conn, SOURCE_META_KEY, "strategy_search")
    db.set_meta(conn, PENDING_META_KEY, "")
    print(f"已套用候選 run_id={pending['run_id']} candidate_name={pending['candidate_name']} weights={pending['weights']}")
    print("active_weights_source=strategy_search -- scripts/run_ranking_optimize.py 下一輪會跳過"
          "把 previous 納入候選（見 src/optimize.py 的 previous_candidate_eligible()）")
    conn.close()


if __name__ == "__main__":
    main()
