import time
import threading
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

PAGE_SIZE = 20
EXCLUDE_REGIONS = {"esoccer"}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1
MAX_PAGES = 15
REFRESH_INTERVAL_SECONDS = 15
MAX_BACKOFF_SECONDS = 120

_session_lock = threading.Lock()
_session = None


def _new_session():
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2)
    s.mount("https://", adapter)
    return s


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            _session = _new_session()
        return _session


def _reset_session():
    global _session
    with _session_lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
        _session = _new_session()


def _get_with_retry(url, params):
    last_exc = None
    for attempt in range(MAX_RETRIES):
        session = _get_session()
        try:
            resp = session.get(url, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.SSLError as e:
            last_exc = e
            _reset_session()
            time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
            continue
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
    print(f"[betway] ERROR: giving up on page fetch — {last_exc}")
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


def fetch_all_upcoming(max_pages=MAX_PAGES):
    all_events, all_markets, all_outcomes, all_prices = [], [], [], []

    skip = 0
    for _ in range(max_pages):
        data = fetch_page(skip)
        if data is None:
            break

        all_events.extend(data.get("events", []))
        all_markets.extend(data.get("markets", []))
        all_outcomes.extend(data.get("outcomes", []))
        all_prices.extend(data.get("prices", []))

        if data.get("isFinalPage", True):
            break
        skip += PAGE_SIZE
        time.sleep(0.3)

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


class OddsCache:
    def __init__(self, refresh_interval_seconds=REFRESH_INTERVAL_SECONDS):
        self.refresh_interval_seconds = refresh_interval_seconds
        self._rows = []
        self._last_updated = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self.fail_count = 0
        self.success_count = 0
        self._consecutive_failures = 0

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
                self._consecutive_failures = 0
                wait_time = self.refresh_interval_seconds
                print(
                    f"[betway] cache refreshed ok — {len(rows)} event/market rows "
                    f"(success #{self.success_count})"
                )
            except Exception as e:
                self.fail_count += 1
                self._consecutive_failures += 1
                wait_time = min(
                    MAX_BACKOFF_SECONDS,
                    self.refresh_interval_seconds * (2**self._consecutive_failures),
                )
                print(
                    f"[betway] ERROR: background refresh failed (#{self._consecutive_failures}) "
                    f"— {e} — backing off {wait_time:.0f}s"
                )
            self._stop_event.wait(wait_time)

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

    while not cache.is_ready():
        time.sleep(0.1)

    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        cache.stop()


if __name__ == "__main__":
    main()
