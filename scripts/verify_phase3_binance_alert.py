"""Phase 3 acceptance verification (not part of the shipped codebase).

Wires a live realtime.binance_source.BinanceSource into monitor.py's
record_trade/record_depth + run_once() + alerts.send_telegram(), runs for
30 minutes against real Binance data, and logs every alert actually sent.

This glue does not exist in monitor.py itself yet (documented there as a
known simplification -- main() only drives the synchronous MIS/TW path).
This script is a throwaway verification harness, not a permanent feature.

Kept as the record of the live acceptance run (2026-07-21, 78 alerts
delivered -- see docs/spike_phase3_verify_log.txt), and as the way to re-run
it against real Binance + real Telegram. The wiring logic itself is now
covered offline by tests/test_push_wiring.py, so routine regression checking
does not need this script.
"""
import asyncio
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import monitor
from alerts import send_telegram
from realtime.binance_source import BinanceSource

CODES = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]  # must match Trade.code case (BinanceSource uppercases symbols)
DURATION_SEC = 30 * 60
EVAL_INTERVAL_SEC = 15
LOG_PATH = "docs/spike_phase3_verify_log.txt"


def log(msg):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def main():
    state = monitor.new_state()
    source = BinanceSource()
    source.subscribe(CODES)
    source.on_trade(lambda t: monitor.record_trade(state, t))
    source.on_depth(lambda d: monitor.record_depth(state, d))

    run_task = asyncio.create_task(source.run())

    total_sent = []
    start = asyncio.get_event_loop().time()
    log(f"verify start, target duration={DURATION_SEC}s, codes={CODES}")
    try:
        while asyncio.get_event_loop().time() - start < DURATION_SEC:
            await asyncio.sleep(EVAL_INTERVAL_SEC)
            now = datetime.datetime.now()
            sent = monitor.run_once(CODES, "crypto", state, source=None, send_fn=send_telegram, now=now)
            if sent:
                for item in sent:
                    log(f"ALERT SENT: {item['alert']['message']} | telegram_result={item['send_result']}")
                total_sent.extend(sent)
            n_ticks = sum(len(h.get("trade_ticks", [])) for h in state["history"].values())
            log(f"heartbeat: {n_ticks} trade ticks accumulated across {len(state['history'])} codes, "
                f"{len(total_sent)} alerts sent so far")
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
    log(f"verify done. total_alerts_sent={len(total_sent)}")
    for item in total_sent:
        log(f"  - {item['alert']['code']} {item['alert']['event_type']}: {item['alert']['message']}")


if __name__ == "__main__":
    open(LOG_PATH, "w", encoding="utf-8").close()
    asyncio.run(main())
