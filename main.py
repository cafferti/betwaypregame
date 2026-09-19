import asyncio

from engineoddsretriever import OddsCache, BasketballOddsCache
from pinnacleretriever import OddsStreamer
from filter import evaluate_pinnacle_event


async def consume_and_evaluate(
    streamer: OddsStreamer,
    football_cache: OddsCache,
    basketball_cache: BasketballOddsCache,
):
    while True:
        event = await streamer.out_queue.get()
        betway_rows = football_cache.get_rows() + basketball_cache.get_rows()
        try:
            evaluate_pinnacle_event(event, betway_rows)
        except Exception as e:
            print(f"[orchestrator] ERROR: evaluate_pinnacle_event failed — {e}")


async def main():
    football_cache = OddsCache()
    football_cache.start()

    basketball_cache = BasketballOddsCache()
    basketball_cache.start()

    print("[orchestrator] Waiting for first Betway fetches (football + basketball)...")
    while not (football_cache.is_ready() and basketball_cache.is_ready()):
        await asyncio.sleep(0.1)
    print("[orchestrator] Both caches ready. Starting Pinnacle stream...")

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
        consume_and_evaluate(streamer, football_cache, basketball_cache),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[orchestrator] Stopped by user.")
