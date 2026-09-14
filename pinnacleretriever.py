import asyncio
import aiohttp
import time
from collections import deque

BASE_URL = (
    "https://sword.pinnacleoddsdropper.com/alerts/user_2ib1Weq6qiKPFR19iNoTqmSoFoD"
)

POLL_INTERVAL_SEC = 8
MAX_BACKOFF_SEC = 30
SEEN_KEYS_MAX = 5000
REQUEST_TIMEOUT_SEC = 15


def passes_filter(event):
    return True


def dedupe_key(event):
    # Price included: same market re-firing at the SAME price is a true
    # duplicate (skip it), but the same market at a NEW price is a genuinely
    # different opportunity and must be re-evaluated. Rounded to 2dp so
    # trivial sub-cent jitter doesn't cause unnecessary re-evaluation spam.
    change_to = event.get("changeTo")
    try:
        change_to = round(float(change_to), 2)
    except (TypeError, ValueError):
        pass

    return (
        event.get("eventId"),
        event.get("lineType"),
        event.get("points"),
        event.get("outcome"),
        event.get("periodNumber"),
        change_to,
    )


class OddsStreamer:
    def __init__(self, out_queue: asyncio.Queue):
        self.cursor = None
        self.total_seen = 0
        self.total_passed = 0
        self.out_queue = out_queue
        self.seen_keys = deque(maxlen=SEEN_KEYS_MAX)
        self.seen_set = set()

    def _remember(self, key):
        if len(self.seen_keys) == self.seen_keys.maxlen:
            oldest = self.seen_keys[0]
            self.seen_set.discard(oldest)
        self.seen_keys.append(key)
        self.seen_set.add(key)

    async def poll_once(self, session: aiohttp.ClientSession):
        params = {"dropNotificationsCursor": self.cursor} if self.cursor else {}
        async with session.get(
            BASE_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC),
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)

        events = payload.get("data", [])
        if not events:
            print(
                f"[pinnacle] poll ok — 0 new alerts (seen={self.total_seen}, queued={self.total_passed})"
            )
            return

        self.total_seen += len(events)
        new_this_poll = 0

        for e in events:
            if not passes_filter(e):
                continue

            key = dedupe_key(e)
            if key in self.seen_set:
                continue
            self._remember(key)

            self.total_passed += 1
            new_this_poll += 1
            desc = (
                f"{e.get('home')} vs {e.get('away')} [{e.get('leagueName')}] "
                f"{e.get('lineType')} pts={e.get('points')} outcome={e.get('outcome')} "
                f"price={e.get('changeTo')}"
            )
            print(f"🔔 [pinnacle] new alert #{self.total_passed}: {desc}")
            await self.out_queue.put(e)

        print(
            f"[pinnacle] poll ok — {len(events)} received, {new_this_poll} new/unique "
            f"(total seen={self.total_seen}, total queued={self.total_passed})"
        )

        latest_ts = max(int(e.get("timestamp", 0)) for e in events)
        latest_id = events[-1].get("id", "")
        suffix = latest_id.split("-")[-1] if "-" in latest_id else "0"
        self.cursor = f"{latest_ts}-{suffix}"

    async def run(self):
        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300, family=0)
        async with aiohttp.ClientSession(connector=connector) as session:
            consecutive_failures = 0
            print("[pinnacle] streamer started")

            while True:
                start = time.monotonic()
                try:
                    await self.poll_once(session)
                    consecutive_failures = 0
                except Exception as ex:
                    consecutive_failures += 1
                    backoff = min(MAX_BACKOFF_SEC, 3 * consecutive_failures)
                    print(
                        f"[pinnacle] ERROR [{type(ex).__name__}]: {ex or 'no message'} "
                        f"— failure #{consecutive_failures}, backing off {backoff}s"
                    )
                    await asyncio.sleep(backoff)
                    continue

                elapsed = time.monotonic() - start
                await asyncio.sleep(max(0, POLL_INTERVAL_SEC - elapsed))


def quiet_asyncio_background_errors(loop, context):
    exc = context.get("exception")
    if exc is not None and isinstance(exc, OSError):
        return
    loop.default_exception_handler(context)


async def main():
    out_queue = asyncio.Queue()
    streamer = OddsStreamer(out_queue)

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(quiet_asyncio_background_errors)

    await streamer.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
