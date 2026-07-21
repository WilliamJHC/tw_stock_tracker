"""Phase 0 spike: Binance WS smoke test.

Subscribes to aggTrade + depth for 2-3 pairs for 30 minutes, logs
connect/disconnect/reconnect events with timestamps and message counts.
Not part of the shipped codebase -- throwaway spike artifact per dev_plan_intraday.md Phase 0.
"""
import asyncio
import datetime
import json

import websockets

SYMBOLS = ["btcusdt", "ethusdt", "solusdt"]
STREAMS = "/".join(f"{s}@aggTrade" for s in SYMBOLS) + "/" + "/".join(f"{s}@depth20@1000ms" for s in SYMBOLS)
URL = f"wss://stream.binance.com:9443/stream?streams={STREAMS}"
DURATION_SEC = 30 * 60
LOG_PATH = "docs/spike_ws_log.txt"


def log(msg):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def main():
    start = asyncio.get_event_loop().time()
    reconnects = 0
    msg_count = 0
    backoff = 1
    log(f"spike start, target duration={DURATION_SEC}s, url={URL}")
    while asyncio.get_event_loop().time() - start < DURATION_SEC:
        try:
            async with websockets.connect(URL, ping_interval=20, ping_timeout=10) as ws:
                log(f"connected (reconnect #{reconnects})")
                backoff = 1
                connected_at = asyncio.get_event_loop().time()
                while asyncio.get_event_loop().time() - start < DURATION_SEC:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    msg_count += 1
                    if msg_count % 200 == 0:
                        data = json.loads(raw)
                        log(f"heartbeat: {msg_count} msgs so far, last stream={data.get('stream')}")
        except Exception as e:
            reconnects += 1
            log(f"disconnected/error: {e!r}; backing off {backoff}s (reconnect attempt #{reconnects})")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    log(f"spike done. total_msgs={msg_count} reconnects={reconnects}")


if __name__ == "__main__":
    open(LOG_PATH, "w", encoding="utf-8").close()
    asyncio.run(main())
