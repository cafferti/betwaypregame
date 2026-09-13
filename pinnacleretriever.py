import asyncio
import aiohttp
import time
from datetime import datetime
from collections import deque

BASE_URL = (
    "https://sword.pinnacleoddsdropper.com/alerts/user_2ib1Weq6qiKPFR19iNoTqmSoFoD"
)

# ── FILTER THRESHOLDS ────────────────────────────────────────────────
MIN_DROP_PERCENT = 13
MIN_LIQUIDITY = 200
MIN_MINUTES_BUFFER = 30
POLL_INTERVAL_SEC = 8
SEEN_KEYS_MAX = 5000
MAX_BACKOFF_SEC = 30


def passes_filter(event):
    try:
        drop_percent = float(event.get("percentageChange", 0))
        liquidity = float(event.get("lowerBoundLimit", 0))
        starts_ms = int(event.get("starts", 0))
        timestamp_ms = int(event.get("timestamp", 0))
    except (ValueError, TypeError):
        return False

    if drop_percent < MIN_DROP_PERCENT:
        return False
    if liquidity < MIN_LIQUIDITY:
        return False
    if (starts_ms - timestamp_ms) / 1000 / 60 < MIN_MINUTES_BUFFER:
        return False
    return True


def dedupe_key(event):
    return (
        event.get("eventId"),
        event.get("lineType"),
        event.get("points"),
        event.get("outcome"),
        event.get("periodNumber"),
    )


def print_opportunity(e):
    starts_ms = int(e.get("starts", 0))
    kickoff_str = datetime.fromtimestamp(starts_ms / 1000).strftime("%Y-%m-%d %H:%M")
    now_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

    print(f"\n🔔 [{now_str}] NEW QUALIFYING DROP")
    print(f"  {e['home']} vs {e['away']}  [{e['leagueName']}]")
    print(
        f"  Market      : {e['lineType']} | points={e.get('points')} | outcome={e['outcome']}"
    )
    print(
        f"  Odds move   : {e['changeFrom']} -> {e['changeTo']}  ({float(e['percentageChange']):.1f}% drop)"
    )
    print(f"  Liquidity   : {e['lowerBoundLimit']}-{e['upperBoundLimit']}")
    print(f"  Kickoff     : {kickoff_str}")
    print("-" * 70)


class OddsStreamer:
    def __init__(self, out_queue: asyncio.Queue):
        self.cursor = None
        self.seen_keys = deque(maxlen=SEEN_KEYS_MAX)
        self.seen_set = set()
        self.total_seen = 0
        self.total_passed = 0
        self.out_queue = out_queue

    def _remember(self, key):
        if len(self.seen_keys) == self.seen_keys.maxlen:
            oldest = self.seen_keys[0]
            self.seen_set.discard(oldest)
        self.seen_keys.append(key)
        self.seen_set.add(key)

    async def poll_once(self, session: aiohttp.ClientSession):
        params = {"dropNotificationsCursor": self.cursor} if self.cursor else {}
        async with session.get(
            BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)

        events = payload.get("data", [])
        if not events:
            return

        self.total_seen += len(events)

        for e in events:
            if not passes_filter(e):
                continue
            key = dedupe_key(e)
            if key in self.seen_set:
                continue
            self._remember(key)
            self.total_passed += 1
            print_opportunity(e)
            await self.out_queue.put(e)

        latest_ts = max(int(e.get("timestamp", 0)) for e in events)
        latest_id = events[-1].get("id", "")
        suffix = latest_id.split("-")[-1] if "-" in latest_id else "0"
        self.cursor = f"{latest_ts}-{suffix}"

    async def run(self):
        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300, family=0)
        async with aiohttp.ClientSession(connector=connector) as session:
            print("🟢 Async streaming started.\n")
            consecutive_failures = 0

            while True:
                start = time.monotonic()
                try:
                    await self.poll_once(session)
                    if consecutive_failures > 0:
                        print(
                            f"\n✅ Network recovered after {consecutive_failures} failed attempt(s). Resuming.\n"
                        )
                    consecutive_failures = 0
                except Exception as ex:
                    consecutive_failures += 1
                    backoff = min(MAX_BACKOFF_SEC, 3 * consecutive_failures)
                    print(
                        f"\n⚠️ Fetch failed [{type(ex).__name__}]: {ex or 'no message'} "
                        f"— failure #{consecutive_failures}, backing off {backoff}s "
                        f"(seen={self.total_seen}, qualifying={self.total_passed} — unchanged during outage)"
                    )
                    await asyncio.sleep(backoff)
                    continue

                elapsed = time.monotonic() - start
                status_line = (
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"poll={elapsed * 1000:.0f}ms | seen={self.total_seen} | "
                    f"qualifying={self.total_passed}"
                )
                print(f"\r{status_line}".ljust(90), end="", flush=True)

                await asyncio.sleep(max(0, POLL_INTERVAL_SEC - elapsed))


def quiet_asyncio_background_errors(loop, context):
    """
    Suppresses noisy internal aiohttp/asyncio background-task tracebacks
    (e.g. DNS resolver 'shielded future' errors) that happen outside our
    own try/except. These are already handled by our retry loop above —
    this just stops them from double-printing an unhandled-exception dump.
    """
    exc = context.get("exception")
    msg = context.get("message", "")
    if exc is not None and isinstance(exc, OSError):
        return  # swallow DNS/socket-level background noise
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
        print("\n🛑 Stopped by user.")
