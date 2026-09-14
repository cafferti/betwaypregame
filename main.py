"""
orchestrator.py — connects the three pieces:
  1. Betway OddsCache (background thread, refreshes every 15s)
  2. Pinnacle OddsStreamer (asyncio, polls every 8s, pushes to a queue)
  3. evaluate_pinnacle_event (pulls from the queue, checks against
     Betway's current cache, prints only if it qualifies)
"""

import asyncio

from engineoddsretriever import OddsCache
from pinnacleretriever import OddsStreamer
from filter import evaluate_pinnacle_event


async def consume_and_evaluate(streamer: OddsStreamer, betway_cache: OddsCache):
    while True:
        event = await streamer.out_queue.get()
        betway_rows = betway_cache.get_rows()
        try:
            evaluate_pinnacle_event(event, betway_rows)
        except Exception as e:
            print(f"[orchestrator] ERROR: evaluate_pinnacle_event failed — {e}")


async def main():
    betway_cache = OddsCache()
    betway_cache.start()

    print("[orchestrator] Waiting for first Betway fetch...")
    while not betway_cache.is_ready():
        await asyncio.sleep(0.1)
    print("[orchestrator] Betway cache ready. Starting Pinnacle stream...")

    out_queue = asyncio.Queue()
    streamer = OddsStreamer(out_queue)

    loop = asyncio.get_running_loop()

    def quiet_asyncio_background_errors(loop, context):
        exc = context.get("exception")
        if exc is not None and isinstance(exc, OSError):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(quiet_asyncio_background_errors)

    await asyncio.gather(
        streamer.run(),
        consume_and_evaluate(streamer, betway_cache),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[orchestrator] Stopped by user.")
