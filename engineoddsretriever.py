import time
import threading
import concurrent.futures
import requests
from datetime import datetime, timezone

BASE_URL = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/BetBook/Upcoming/"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.betway.com.ng/",
    "Origin": "https://www.betway.com.ng",
}

PAGE_SIZE = 200
EXCLUDE_REGIONS = {"esoccer"}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1
MAX_PAGES_GUESS = 4
FETCH_WORKERS = MAX_PAGES_GUESS
REFRESH_INTERVAL_SECONDS = 5

_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=FETCH_WORKERS, pool_maxsize=FETCH_WORKERS
)
_session.mount("https://", _adapter)


def _get_with_retry(url, params):
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = _session.get(url, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            last_exc = e
            if status is not None and 500 <= status < 600:
                time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
                continue
            raise
        except requests.exceptions.RequestException as e:
            last_exc = e
            time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
    print(f"   ❌ Giving up: {last_exc}")
    return None


def fetch_page(skip):
    params = {
        "countryCode": "NG",
        "sportId": "soccer",
        "Skip": skip,
        "Take": PAGE_SIZE,
        "cultureCode": "en-US",
        "isEsport": "false",
        "boostedOnly": "false",
    }
    url_params = list(params.items()) + [
        ("marketTypes", "[Win/Draw/Win]"),
        ("marketTypes", "[Double Chance]"),
        ("marketTypes", "[Total Goals]"),
        ("marketTypes", "[Handicap] [2-Way]"),
        ("marketTypes", "[Handicap] [3-Way]"),
    ]
    return _get_with_retry(BASE_URL, url_params)


def fetch_all_upcoming(max_pages=MAX_PAGES_GUESS, workers=FETCH_WORKERS):
    skips = [i * PAGE_SIZE for i in range(max_pages)]
    results_by_skip = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_skip = {executor.submit(fetch_page, skip): skip for skip in skips}
        for future in concurrent.futures.as_completed(future_to_skip):
            skip = future_to_skip[future]
            try:
                results_by_skip[skip] = future.result()
            except Exception as e:
                print(f"   ⚠️  page skip={skip} failed: {e}")
                results_by_skip[skip] = None

    all_events, all_markets, all_outcomes, all_prices = [], [], [], []
    for skip in skips:
        data = results_by_skip.get(skip)
        if data is None:
            continue
        all_events.extend(data.get("events", []))
        all_markets.extend(data.get("markets", []))
        all_outcomes.extend(data.get("outcomes", []))
        all_prices.extend(data.get("prices", []))
        if data.get("isFinalPage", True):
            break

    return {
        "events": all_events,
        "markets": all_markets,
        "outcomes": all_outcomes,
        "prices": all_prices,
    }


def _clean_line(value):
    if isinstance(value, str):
        return value.strip(" ()")
    return value


def _extract_line_value(market: dict, outcome: dict):
    outcome_handicap = outcome.get("handicap")
    outcome_sbv = outcome.get("sbv")
    market_handicap = market.get("handicap")

    if outcome_handicap not in (None, "", 0):
        return outcome_handicap, "outcome.handicap"
    if outcome_sbv not in (None, "", 0):
        return outcome_sbv, "sbv"
    if market_handicap not in (None, "", 0):
        return market_handicap, "market.handicap"
    return "", None


def build_odds_table(data):
    events = {
        e["eventId"]: e
        for e in data.get("events", [])
        if e.get("regionId") not in EXCLUDE_REGIONS
    }

    outcomes_by_market = {}
    seen_outcome_ids = set()
    for o in data.get("outcomes", []):
        oid = o.get("outcomeId")
        if oid is not None and oid in seen_outcome_ids:
            continue
        if oid is not None:
            seen_outcome_ids.add(oid)
        outcomes_by_market.setdefault(o["marketId"], []).append(o)

    prices_by_outcome = {p["outcomeId"]: p for p in data.get("prices", [])}

    markets_by_event = {}
    for m in data.get("markets", []):
        if m["eventId"] in events:
            markets_by_event.setdefault(m["eventId"], []).append(m)

    results = []
    for event_id, event in events.items():
        market_blocks = []
        for market in markets_by_event.get(event_id, []):
            outcome_rows = []
            for outcome in outcomes_by_market.get(market["marketId"], []):
                price = prices_by_outcome.get(outcome["outcomeId"])
                price_val = price.get("priceDecimal") if price else None
                if price_val in (None, 0):
                    continue

                line_val, _ = _extract_line_value(market, outcome)

                outcome_rows.append(
                    {
                        "name": outcome.get("name"),
                        "line": _clean_line(line_val),
                        "price": price_val,
                    }
                )

            if not outcome_rows:
                continue

            market_blocks.append(
                {
                    "marketId": market.get("marketId"),
                    "displayName": market.get("displayName"),
                    "outcomes": outcome_rows,
                }
            )

        if not market_blocks:
            continue

        results.append(
            {
                "eventId": event_id,
                "home": event.get("homeTeam"),
                "away": event.get("awayTeam"),
                "league": event.get("league"),
                "region": event.get("region"),
                "kickoff_epoch": event.get("expectedStartEpoch"),
                "markets": market_blocks,
            }
        )
    return results


def print_odds_table(rows):
    print(f"📡 {len(rows)} events with odds\n")
    print("=" * 70)
    for r in rows:
        kickoff = datetime.fromtimestamp(r["kickoff_epoch"], tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        print(f"⚽ {r['home']} vs {r['away']}  [{r['league']} - {r['region']}]")
        print(f"   Kickoff: {kickoff}  | eventId: {r['eventId']}")

        by_name = {}
        for m in r["markets"]:
            by_name.setdefault(m["displayName"], []).append(m)

        for market_name, instances in by_name.items():
            print(f"\n   📊 {market_name}")
            for inst in instances:
                print(f"      -- Market ID {inst['marketId']} --")
                for o in inst["outcomes"]:
                    label = (
                        f"{o['name']} {o['line']}"
                        if o["line"] not in ("", None)
                        else o["name"]
                    )
                    print(f"         {label:30}: {o['price']}")
        print("\n" + "-" * 70)


class OddsCache:
    """
    Runs fetch_all_upcoming() + build_odds_table() on a loop in the
    background. Consuming code calls get_rows(), which returns instantly
    from memory — no network wait on the calling side.
    """

    def __init__(self, refresh_interval_seconds=REFRESH_INTERVAL_SECONDS):
        self.refresh_interval_seconds = refresh_interval_seconds
        self._rows = []
        self._last_updated = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self.fail_count = 0
        self.success_count = 0

    def start(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                data = fetch_all_upcoming()
                rows = build_odds_table(data)
                with self._lock:
                    self._rows = rows
                    self._last_updated = time.time()
                    self.success_count += 1
            except Exception as e:
                self.fail_count += 1
                print(f"   ⚠️  Background refresh failed: {e}")
            self._stop_event.wait(self.refresh_interval_seconds)

    def get_rows(self):
        with self._lock:
            return self._rows

    def age_seconds(self):
        with self._lock:
            if self._last_updated is None:
                return None
            return time.time() - self._last_updated

    def is_ready(self):
        return self.age_seconds() is not None


def main():
    cache = OddsCache()
    cache.start()

    print("Waiting for first fetch to complete...")
    while not cache.is_ready():
        time.sleep(0.1)

    print_odds_table(cache.get_rows())
    print("\n✅ Cache is live. Running indefinitely — press Ctrl+C to stop.\n")

    try:
        while True:
            rows = cache.get_rows()
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"{len(rows)} events cached | age: {cache.age_seconds():.1f}s | "
                f"successes: {cache.success_count} | failures: {cache.fail_count}"
            )
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n🛑 Stopping...")
        cache.stop()


if __name__ == "__main__":
    main()
