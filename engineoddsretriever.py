import time
import threading
import concurrent.futures
import requests
from difflib import SequenceMatcher

import netstatus

BASE_URL = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/BetBook/Upcoming/"
EVENT_MARKETS_URL = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/MarketGroupings/MarketGroupNamesAndMarketsForEvent"

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
MAX_PAGES = 200  # safety ceiling only — isFinalPage normally stops well before this
REFRESH_INTERVAL_SECONDS = 15
MAX_BACKOFF_SECONDS = 120

BASKETBALL_PAGE_SIZE = 200
BASKETBALL_MAX_PAGES_GUESS = (
    50  # safety ceiling only — isFinalPage normally stops well before this
)
BASKETBALL_LISTING_WORKERS = 8
BASKETBALL_PER_EVENT_WORKERS = 5
BASKETBALL_REFRESH_INTERVAL_SECONDS = 20

BASKETBALL_TARGET_MARKET_DISPLAY_NAMES = {
    "Winner (Incl. OT)",
    "Handicap (Incl. Overtime)",
    "Total (Incl. Overtime)",
}

TEAM_MATCH_THRESHOLD_LIVE = 0.60

# A line counts as "exactly the same" when it is within this distance.
LINE_MATCH_TOLERANCE = 0.01

# Betway returns a match's markets in pages (the site asks for 40 at a time).
EVENT_MARKETS_PAGE_SIZE = 40
EVENT_MARKETS_MAX_PAGES = 8

# Markets whose name/title contains any of these are NOT the main full-game
# handicap/total (half-time, quarter, team totals, corners, cards...).
EXCLUDED_MARKET_WORDS = (
    "half",
    "quarter",
    "period",
    "1st",
    "2nd",
    "3rd",
    "4th",
    "team",
    "home",
    "away",
    "corner",
    "booking",
    "card",
)

# Separate session pools so one job can never starve another of connections.
_sessions = {}
_sessions_lock = threading.Lock()


def _new_session(pool_size):
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_size, pool_maxsize=pool_size
    )
    s.mount("https://", adapter)
    return s


def _get_session(pool_key, pool_size):
    with _sessions_lock:
        if pool_key not in _sessions:
            _sessions[pool_key] = _new_session(pool_size)
        return _sessions[pool_key]


def _reset_session(pool_key, pool_size):
    with _sessions_lock:
        existing = _sessions.get(pool_key)
        if existing is not None:
            try:
                existing.close()
            except Exception:
                pass
        _sessions[pool_key] = _new_session(pool_size)


def _get_with_retry(url, params, pool_key="default", pool_size=10):
    """
    GET with retries. If the failure is "no internet", it does NOT retry or print:
    it pauses (the bold WAITING FOR NETWORK line is shown once) and carries on with
    the same request when the connection is back.
    """
    last_exc = None
    attempt = 0
    net_pauses = 0
    while attempt < MAX_RETRIES:
        netstatus.wait_online()  # paused while the internet is down
        session = _get_session(pool_key, pool_size)
        try:
            resp = session.get(url, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.SSLError as e:
            last_exc = e
            if netstatus.is_network_error(e) and net_pauses < 3:
                net_pauses += 1
                netstatus.waiting("betway")
                continue
            _reset_session(pool_key, pool_size)
            time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
            attempt += 1
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            last_exc = e
            if status is not None and 500 <= status < 600:
                time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
                attempt += 1
                continue
            raise
        except requests.exceptions.RequestException as e:
            last_exc = e
            if netstatus.is_network_error(e) and net_pauses < 3:
                net_pauses += 1
                netstatus.waiting("betway")
                continue
            time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
            attempt += 1
    if last_exc is not None and netstatus.is_network_error(last_exc):
        return None  # still failing right after the network came back: stay quiet
    print(f"[betway] ERROR: giving up — {last_exc}")
    return None


def _normalize_name(name: str) -> str:
    return (name or "").lower().strip()


def _team_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize_name(a), _normalize_name(b)).ratio()


def _fetch_event_markets_all_pages(event_id, pool_key, pool_size):
    """
    Fetches ALL of a match's markets, page by page, and merges them.
    (A single request only returns the first 40 markets, so lines could be
    missing.) Returns None if the very first page fails.
    """
    merged = {"events": [], "marketsInGroup": [], "outcomes": [], "prices": []}
    seen_market_ids = set()

    for page in range(EVENT_MARKETS_MAX_PAGES):
        params = {
            "eventId": event_id,
            "marketGroupId": " ",
            "countryCode": "NG",
            "cultureCode": "en-US",
            "skip": page * EVENT_MARKETS_PAGE_SIZE,
            "take": EVENT_MARKETS_PAGE_SIZE,
            "isBuildABetOnly": "false",
            "searchQuery": "",
        }
        data = _get_with_retry(
            EVENT_MARKETS_URL, params, pool_key=pool_key, pool_size=pool_size
        )
        if data is None:
            return None if page == 0 else merged

        markets = data.get("marketsInGroup") or []
        new_markets = [m for m in markets if m.get("marketId") not in seen_market_ids]
        if not new_markets:
            break  # empty page, or the server repeated the same page

        for m in new_markets:
            seen_market_ids.add(m.get("marketId"))
        merged["marketsInGroup"].extend(new_markets)
        merged["events"].extend(data.get("events") or [])
        merged["outcomes"].extend(data.get("outcomes") or [])
        merged["prices"].extend(data.get("prices") or [])

        if len(markets) < EVENT_MARKETS_PAGE_SIZE:
            break

    return merged


# ============================================================
# FOOTBALL
# ============================================================


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
    return _get_with_retry(BASE_URL, url_params, pool_key="football", pool_size=6)


def fetch_all_upcoming(max_pages=MAX_PAGES):
    """
    Raises RuntimeError if any page request genuinely failed (timeout,
    5xx, etc. after retries) — a failed request must not be silently
    treated as "0 events available".
    """
    all_events, all_markets, all_outcomes, all_prices = [], [], [], []

    skip = 0
    pages_fetched = 0
    hit_final_page = False
    for _ in range(max_pages):
        data = fetch_page(skip)
        if data is None:
            raise RuntimeError(
                f"football page fetch failed at skip={skip} after retries "
                f"({pages_fetched} page(s) fetched successfully before this)"
            )

        all_events.extend(data.get("events", []))
        all_markets.extend(data.get("markets", []))
        all_outcomes.extend(data.get("outcomes", []))
        all_prices.extend(data.get("prices", []))
        pages_fetched += 1

        if data.get("isFinalPage", True):
            hit_final_page = True
            break
        skip += PAGE_SIZE
        time.sleep(0.3)

    if not hit_final_page:
        print(
            f"[football] WARNING: stopped after hitting MAX_PAGES={max_pages} "
            f"({pages_fetched} pages) WITHOUT reaching isFinalPage — "
            f"some events may be missing"
        )

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
                        "outcomeId": outcome.get("outcomeId"),
                        "marketId": market.get("marketId"),
                        "eventId": event_id,
                        "priceNum": price.get("numerator"),
                        "priceDen": price.get("denominator"),
                        "eventVersion": event.get("version"),
                        "marketVersion": market.get("version"),
                        "outcomeVersion": outcome.get("version"),
                        "priceVersion": price.get("version"),
                        "serverEmopSource": price.get("emopSource"),
                        "publicHubPublishedTime": price.get("publicHubPublishedTime"),
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
                    f"[football] engineoddsretriever.py executed successfully — {len(rows)} rows"
                )
            except Exception as e:
                self.fail_count += 1
                self._consecutive_failures += 1
                if netstatus.is_network_error(e) or netstatus.is_down("betway"):
                    netstatus.waiting("betway")
                    wait_time = 5  # keep retrying quickly so recovery is fast
                else:
                    wait_time = min(
                        MAX_BACKOFF_SECONDS,
                        self.refresh_interval_seconds * (2**self._consecutive_failures),
                    )
                    print(
                        f"[football] ERROR: refresh failed (#{self._consecutive_failures}) — {e} — backing off {wait_time:.0f}s"
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


# ============================================================
# BASKETBALL
# ============================================================


def fetch_basketball_listing_page(skip):
    params = {
        "countryCode": "NG",
        "sportId": "basketball",
        "Skip": skip,
        "Take": BASKETBALL_PAGE_SIZE,
        "cultureCode": "en-US",
        "isEsport": "false",
        "boostedOnly": "false",
    }
    url_params = list(params.items()) + [
        ("marketTypes", "Winner (Incl. Overtime)"),
    ]
    return _get_with_retry(
        BASE_URL, url_params, pool_key="basketball_listing", pool_size=10
    )


def fetch_all_basketball_events(
    max_pages=BASKETBALL_MAX_PAGES_GUESS, workers=BASKETBALL_LISTING_WORKERS
):
    """
    Raises RuntimeError if any page in the batch failed after retries —
    matches football's behavior so OddsCache's error/backoff path
    actually triggers instead of silently undercounting.
    """
    skips = [i * BASKETBALL_PAGE_SIZE for i in range(max_pages)]
    results_by_skip = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_skip = {
            executor.submit(fetch_basketball_listing_page, skip): skip for skip in skips
        }
        for future in concurrent.futures.as_completed(future_to_skip):
            skip = future_to_skip[future]
            try:
                results_by_skip[skip] = future.result()
            except Exception as e:
                print(f"[basketball] ERROR: listing page skip={skip} failed — {e}")
                results_by_skip[skip] = None

    all_events = []
    hit_final_page = False
    failed_skips = []
    for skip in skips:
        data = results_by_skip.get(skip)
        if data is None:
            failed_skips.append(skip)
            continue
        all_events.extend(data.get("events", []))
        if data.get("isFinalPage", True):
            hit_final_page = True
            break

    if failed_skips:
        raise RuntimeError(
            f"basketball listing fetch had {len(failed_skips)} failed page(s) "
            f"at skips={failed_skips} after retries"
        )

    if not hit_final_page:
        print(
            f"[basketball] WARNING: stopped after hitting max_pages={max_pages} "
            f"WITHOUT reaching isFinalPage — some events may be missing"
        )

    return all_events


def fetch_basketball_event_markets(event_id):
    data = _fetch_event_markets_all_pages(event_id, "basketball_events", 8)
    if data is None:
        return []

    event_version = None
    for e in data.get("events", []):
        if e.get("eventId") == event_id:
            event_version = e.get("version")
            break

    target_markets = {
        m["marketId"]: m
        for m in data.get("marketsInGroup", [])
        if m.get("displayName") in BASKETBALL_TARGET_MARKET_DISPLAY_NAMES
    }
    if not target_markets:
        return []

    prices_by_outcome = {p["outcomeId"]: p for p in data.get("prices", [])}

    outcomes_by_market = {}
    for o in data.get("outcomes", []):
        if not o.get("shouldDisplay", True):
            continue
        if o.get("marketId") in target_markets:
            outcomes_by_market.setdefault(o["marketId"], []).append(o)

    market_blocks = []
    for market_id, market in target_markets.items():
        outcome_rows = []
        for o in outcomes_by_market.get(market_id, []):
            price = prices_by_outcome.get(o["outcomeId"])
            price_val = price.get("priceDecimal") if price else None
            if price_val in (None, 0):
                continue

            handicap_val = o.get("handicap")
            line_val = handicap_val if handicap_val not in (None, "", 0) else ""

            outcome_rows.append(
                {
                    "name": (o.get("name") or "").strip(),
                    "line": line_val,
                    "price": price_val,
                    "outcomeId": o.get("outcomeId"),
                    "marketId": market_id,
                    "eventId": event_id,
                    "priceNum": price.get("numerator"),
                    "priceDen": price.get("denominator"),
                    "eventVersion": event_version
                    if event_version is not None
                    else market.get("version"),
                    "marketVersion": market.get("version"),
                    "outcomeVersion": o.get("version"),
                    "priceVersion": price.get("version"),
                    "serverEmopSource": price.get("emopSource"),
                    "publicHubPublishedTime": price.get("publicHubPublishedTime"),
                }
            )

        if not outcome_rows:
            continue

        market_blocks.append(
            {
                "marketId": market_id,
                "displayName": market.get("displayName"),
                "outcomes": outcome_rows,
            }
        )

    return market_blocks


def build_basketball_odds_table(events, max_workers=BASKETBALL_PER_EVENT_WORKERS):
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_event = {
            executor.submit(fetch_basketball_event_markets, e["eventId"]): e
            for e in events
        }
        for future in concurrent.futures.as_completed(future_to_event):
            event = future_to_event[future]
            try:
                market_blocks = future.result()
            except Exception as e:
                print(
                    f"[basketball] ERROR: event {event.get('eventId')} markets failed — {e}"
                )
                continue

            if not market_blocks:
                continue

            results.append(
                {
                    "eventId": event.get("eventId"),
                    "home": event.get("homeTeam"),
                    "away": event.get("awayTeam"),
                    "league": event.get("league"),
                    "region": event.get("region"),
                    "kickoff_epoch": event.get("expectedStartEpoch"),
                    "markets": market_blocks,
                }
            )

    return results


class BasketballOddsCache:
    def __init__(self, refresh_interval_seconds=BASKETBALL_REFRESH_INTERVAL_SECONDS):
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
                events = fetch_all_basketball_events()
                rows = build_basketball_odds_table(events)
                with self._lock:
                    self._rows = rows
                    self._last_updated = time.time()
                    self.success_count += 1
                self._consecutive_failures = 0
                wait_time = self.refresh_interval_seconds
                print(
                    f"[basketball] engineoddsretriever.py executed successfully — {len(rows)} rows"
                )
            except Exception as e:
                self.fail_count += 1
                self._consecutive_failures += 1
                if netstatus.is_network_error(e) or netstatus.is_down("betway"):
                    netstatus.waiting("betway")
                    wait_time = 5  # keep retrying quickly so recovery is fast
                else:
                    wait_time = min(
                        MAX_BACKOFF_SECONDS,
                        self.refresh_interval_seconds * (2**self._consecutive_failures),
                    )
                    print(
                        f"[basketball] ERROR: refresh failed (#{self._consecutive_failures}) — {e} — backing off {wait_time:.0f}s"
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


# ============================================================
# LIVE PER-BET MARKET RESOLUTION (spread / total only)
# ============================================================


def _fmt_lines(lines, limit=8):
    uniq = sorted(set(lines))
    shown = ", ".join(f"{x:+g}" for x in uniq[:limit])
    return shown + (" ..." if len(uniq) > limit else "")


# DEFAULT: how far the Betway line may be from Pinnacle's line before the bet is
# skipped. None = no limit. 0.5 = only lines within 0.5 of Pinnacle's line are
# considered. Callers can override this per call via resolve_live_bet_target's
# max_line_gap argument (filter.py passes 7 for basketball totals).
MAX_LINE_GAP = 0.5

# When no exact line exists, the line MOST LIKELY TO WIN inside the gap is used
# (spread: higher bet-side line; over: lower line; under: higher line).
# True  = if only a HARDER line is within the gap, still use it.
# False = never bet a line harder than Pinnacle's (EV can then only be understated).
ALLOW_HARDER_LINES = True


_PERIOD_PREFIX = {1: "1st half", 2: "2nd half"}


def _market_in_family(m, keyword, period=0):
    """
    Is this market a full-game (period 0) or 1st/2nd-half (period 1/2) market of the
    wanted kind? Betway names them like:
        "Handicap" / "Handicap (0 : 0.5)"               full game
        "1st Half - Handicap" / "1st Half - Handicap 0:1"
        "2nd Half - Handicap" / "2nd Half - Handicap - 2 Way 0.5"
    """
    name = (m.get("name") or "").strip().lower()
    display = (m.get("displayName") or "").strip().lower()
    if period in _PERIOD_PREFIX:
        prefix = _PERIOD_PREFIX[period]
        if not (name.startswith(prefix) or display.startswith(prefix)):
            return False
        if keyword not in name and keyword not in display:
            return False
        words = [w for w in EXCLUDED_MARKET_WORDS if w not in ("half", "1st", "2nd")]
        return not any(w in name or w in display for w in words)
    if not (name.startswith(keyword) or display.startswith(keyword)):
        return False
    return not any(w in name or w in display for w in EXCLUDED_MARKET_WORDS)


def _build_candidates(data, keyword, period, line_type):
    """
    (market, line) pairs. For handicaps the line is read from the market's OWN selections
    first: Betway keeps the main line (e.g. +0.5) on the parent market itself, whose own
    "handicap" field says 0. Child markets that repeat a line win over the parent.
    """
    outcomes = data.get("outcomes", []) or []
    has_ref = any("marketId" in o for o in outcomes)
    by_market = {}
    if has_ref:
        for o in outcomes:
            if o.get("shouldDisplay", True):
                by_market.setdefault(str(o.get("marketId", "")), []).append(o)

    raw = []
    for m in data.get("marketsInGroup", []):
        if m.get("isSuspended"):
            continue
        if m.get("isActive") is False or m.get("shouldDisplay") is False:
            continue
        if not _market_in_family(m, keyword, period):
            continue
        mid = str(m.get("marketId"))
        added = False
        if line_type == "spread" and has_ref:
            groups = {}
            for o in by_market.get(mid, []):
                nm = (o.get("name") or "").strip().lower()
                if nm in ("yes", "no"):
                    continue
                try:
                    hv = round(float(o.get("handicap")), 3)
                except (TypeError, ValueError):
                    continue
                groups.setdefault(hv, []).append(o)
            for hv, outs in groups.items():
                names = [(o.get("name") or "").strip().lower() for o in outs]
                draws = sum(1 for n in names if n in ("draw", "x"))
                teams = len({n for n in names if n not in ("draw", "x")})
                if teams == 2 and draws <= 1 and len(outs) == 2 + draws:
                    raw.append((m, hv))
                    added = True
        if not added and not m.get("isSquashedParent"):
            try:
                raw.append((m, float(m.get("handicap"))))
            except (TypeError, ValueError):
                pass

    child_keys = {
        (str(m.get("marketId")).split("hcp=")[0], round(h, 3))
        for m, h in raw
        if "hcp=" in str(m.get("marketId"))
    }
    out, seen = [], set()
    for m, h in raw:
        mid = str(m.get("marketId"))
        if "hcp=" not in mid and (mid.split("hcp=")[0], round(h, 3)) in child_keys:
            continue  # a child market already carries this line
        if (mid, round(h, 3)) in seen:
            continue
        seen.add((mid, round(h, 3)))
        out.append((m, h))
    return out


def _outcomes_for_market(market, h, all_outcomes, has_market_ref):
    """
    The selections that belong to ONE line of a handicap/total market.
    First try the selections that point at this exact market id. Some matches hang
    every line's selections under the PARENT market instead; then take the family's
    selections whose own handicap equals this line.
    """
    market_id = str(market.get("marketId"))

    def visible(o):
        return o.get("shouldDisplay", True)

    if has_market_ref:
        outs = [
            o
            for o in all_outcomes
            if str(o.get("marketId", "")) == market_id and visible(o)
        ]
    else:
        outs = [
            o
            for o in all_outcomes
            if str(o.get("outcomeId", "")).startswith(market_id) and visible(o)
        ]
    if (
        len(outs) > 3
    ):  # a parent market that holds several lines: keep this line's selections
        narrowed = []
        for o in outs:
            try:
                if abs(float(o.get("handicap")) - h) <= LINE_MATCH_TOLERANCE:
                    narrowed.append(o)
            except (TypeError, ValueError):
                pass
        if narrowed:
            outs = narrowed
    if len(outs) in (
        2,
        3,
    ):  # 2-way handicap/total, or a 3-way handicap (two teams + Draw)
        return outs

    base = market_id.split("hcp=")[0]
    family, seen = [], set()
    for o in all_outcomes:
        oid = str(o.get("outcomeId", ""))
        if oid in seen or not visible(o):
            continue
        mid = str(o.get("marketId", ""))
        if not (
            mid == market_id
            or (base and mid.startswith(base))
            or oid.startswith(market_id)
        ):
            continue
        try:
            oh = float(o.get("handicap"))
        except (TypeError, ValueError):
            continue
        if abs(oh - h) <= LINE_MATCH_TOLERANCE:
            seen.add(oid)
            family.append(o)
    return family


def _push_probability(outs, prices_by_outcome):
    """
    In a 3-way handicap ("0:1" style) the Draw selection is what pays when a whole-number
    handicap would have been a push. Its probability, from Betway's own three prices
    (margin removed proportionally), tells us how much of the time the team selection
    loses where Pinnacle's 2-way line would only refund.
    """
    inv = {}
    for o in outs:
        pr = prices_by_outcome.get(o.get("outcomeId"))
        v = pr.get("priceDecimal") if pr else None
        if not v:
            return 0.0
        inv[(o.get("name") or "").strip().lower()] = 1.0 / float(v)
    total = sum(inv.values())
    draw = inv.get("draw", inv.get("x"))
    return (draw / total) if (draw and total) else 0.0


def _pick_spread_selection(
    outs, outcome_side, team_name, other_team_name, bw_home, bw_away
):
    """
    Which of the two selections is the team being bet?
      1) by Betway's own home/away names for this match (most reliable)
      2) by Pinnacle's two team names (best pairing, no hard threshold)
      3) the old rule: best name match to the bet team, at least TEAM_MATCH_THRESHOLD_LIVE
    """
    if len(outs) != 2:
        return None
    o0, o1 = outs

    def pair(n_first, n_second):
        # score for [o0 = first name, o1 = second name]  vs  swapped
        a = _team_similarity(o0.get("name", ""), n_first) + _team_similarity(
            o1.get("name", ""), n_second
        )
        b = _team_similarity(o1.get("name", ""), n_first) + _team_similarity(
            o0.get("name", ""), n_second
        )
        return a, b

    if bw_home and bw_away:
        a, b = pair(bw_home, bw_away)  # a: o0 is home
        if abs(a - b) > 0.05:
            home_o, away_o = (o0, o1) if a > b else (o1, o0)
            return home_o if outcome_side == "home" else away_o

    if team_name and other_team_name:
        a, b = pair(team_name, other_team_name)  # a: o0 is the bet team
        if abs(a - b) > 0.05:
            return o0 if a > b else o1

    best, best_score = None, 0.0
    for o in outs:
        score = _team_similarity(o.get("name", ""), team_name)
        if score > best_score:
            best_score, best = score, o
    return best if best_score >= TEAM_MATCH_THRESHOLD_LIVE else None


def _football_preferences(line_type, target, outcome_side):
    """
    Ordered list of Betway lines (in the BET SIDE's own numbers) to try for a football
    spread / total, best first. Anything not listed falls back to the nearest line.

      whole numbers, spreads : the half-line closer to zero first, then the other half-line
                               (-2 -> -1.5 then -2.5 | +2 -> +1.5 then +2.5)
      spread 0               : +0.5 then -0.5
      whole numbers, totals  : Over N -> N-0.5 then N+0.5 | Under N -> N+0.5 then N-0.5
      quarter lines (x.25 / x.75): either neighbouring line; if both exist the easier one
    """
    prefs = [target]  # an exact match always comes first
    frac = round(abs(target) % 1, 2)
    easier_is_higher = (line_type == "spread") or (outcome_side == "under")

    if frac == 0.0:
        if line_type == "spread":
            if abs(target) < 1e-9:
                prefs += [0.5, -0.5]
            else:
                sign = 1 if target > 0 else -1
                prefs += [sign * (abs(target) - 0.5), sign * (abs(target) + 0.5)]
        else:
            if outcome_side == "over":
                prefs += [target - 0.5, target + 0.5]
            else:
                prefs += [target + 0.5, target - 0.5]
    elif frac in (0.25, 0.75):
        a, b = target - 0.25, target + 0.25
        prefs += [b, a] if easier_is_higher else [a, b]
    return prefs


def resolve_live_bet_target(
    event_id,
    line_type,
    target_line,
    outcome_side,
    team_name,
    max_line_gap=MAX_LINE_GAP,
    prefer_nearest=False,
    other_team_name=None,
    line_rules=None,
    allow_three_way=False,
    line_window=None,
    period=0,
):
    """
    Finds the Betway market + selection for a spread or total bet, using the
    NEAREST line Betway offers to Pinnacle's line (exact match is used when it exists).

    max_line_gap: how far the Betway line may be from Pinnacle's line. Defaults
    to the module-level MAX_LINE_GAP; None = no limit.
    prefer_nearest: pick the Betway line CLOSEST to Pinnacle's (an exact match first;
    on a tie the line that is easier to win). Otherwise the easiest line in range is used.
    other_team_name: the opposing team's name (helps pick the right selection).
    line_rules: "football" = use _football_preferences() to choose between neighbouring
    lines (whole numbers, quarter lines, 0); anything else keeps the nearest/easiest rule.
    line_window: (down, up_if_positive, up_if_negative) - how far from Pinnacle's line the
    Betway line may be, measured in the BET SIDE's own numbers. "Up" = easier (more points
    for the bettor), "down" = harder. Replaces max_line_gap when given.
    period: 0 = full game, 1 / 2 = first / second half (football handicaps).
    allow_three_way: also use Betway's 3-way handicap markets ("0:1", "1:0", ... with a Draw
    selection). Whole-number handicaps only exist there. The bet is on the team selection;
    the returned dict then carries threeWay=True and pushProb (chance of the Draw outcome).

    SIGN RULE (spreads): Betway's market handicap is always the HOME team's
    handicap, e.g. "Handicap (0 : 0.5)" has handicap -0.5 = home -0.5 / away +0.5,
    and BOTH of its selections carry that same -0.5. Pinnacle's point belongs to
    the team being bet. So:
        bet on home, Pinnacle point p  -> look for Betway market handicap  p
        bet on away, Pinnacle point p  -> look for Betway market handicap -p

    Still enforced (so the bet is a sane one):
      - only the main full-game "Handicap"/"Total" markets
      - 3-way handicaps (market ids containing ":") are ignored
      - spread markets must have exactly 2 selections, no "draw"

    Returns (live_dict, "") on success, or (None, reason) when it can't.
    """
    data = _fetch_event_markets_all_pages(event_id, "resolve", 8)
    if data is None:
        return None, "Betway market lookup failed (network)"

    event_version = None
    bw_home = bw_away = None
    for e in data.get("events", []):
        if e.get("eventId") == event_id:
            event_version = e.get("version")
            bw_home = e.get("homeTeam") or e.get("home")
            bw_away = e.get("awayTeam") or e.get("away")
            break

    keyword = "handicap" if line_type == "spread" else "total"

    candidates = _build_candidates(data, keyword, period, line_type)

    if not candidates:
        where = {1: "1st-half ", 2: "2nd-half "}.get(period, "")
        return None, f"Betway has no open {where}{keyword} markets for this match"

    two_way = [(m, h) for m, h in candidates if ":" not in str(m.get("marketId"))]
    usable = candidates if allow_three_way else two_way
    if not usable:
        return None, f"Betway only has 3-way {keyword} markets for this match"

    # Convert the bet side's line into Betway's home-perspective handicap
    if line_type == "spread" and outcome_side == "away":
        market_target = -target_line
    else:
        market_target = target_line

    def _bet_side_line(h):
        return -h if (line_type == "spread" and outcome_side == "away") else h

    def _ease(h):
        """Bigger = more likely to win for the bettor."""
        if line_type == "spread":
            return _bet_side_line(h)
        return -h if outcome_side == "over" else h

    target_ease = _ease(market_target)

    pool = usable
    window_text = None
    if line_window is not None:
        down, up_pos, up_neg = line_window
        up = up_pos if target_line >= 0 else up_neg
        lo, hi = target_line - down, target_line + up
        window_text = f"{lo:+g} to {hi:+g}"
        pool = [
            mh
            for mh in pool
            if lo - LINE_MATCH_TOLERANCE
            <= _bet_side_line(mh[1])
            <= hi + LINE_MATCH_TOLERANCE
        ]
    elif max_line_gap is not None:
        pool = [
            mh
            for mh in pool
            if abs(mh[1] - market_target) <= max_line_gap + LINE_MATCH_TOLERANCE
        ]
    if not ALLOW_HARDER_LINES:
        pool = [mh for mh in pool if _ease(mh[1]) >= target_ease - LINE_MATCH_TOLERANCE]

    if not pool:
        offered = [_bet_side_line(h) for _, h in usable]
        return (
            None,
            f"no Betway line "
            + (
                f"from {window_text}"
                if window_text
                else f"within {max_line_gap if max_line_gap is not None else 'any'}"
            )
            + f" of {target_line:g} (it offers {_fmt_lines(offered)})",
        )

    # 1) an exact line always wins; 2) otherwise either the NEAREST line (prefer_nearest,
    #    ties go to the easier line) or the line most likely to win (then the nearest one)
    prefs = (
        _football_preferences(line_type, target_line, outcome_side)
        if line_rules == "football"
        else None
    )

    def _rank(mh):
        gap = abs(mh[1] - market_target)
        exact = gap <= LINE_MATCH_TOLERANCE
        if prefs is not None:
            bl = _bet_side_line(mh[1])
            for i, pl in enumerate(prefs):
                if abs(bl - pl) <= LINE_MATCH_TOLERANCE:
                    return (0, i, 0.0)
            return (
                1,
                round(gap, 4),
                -_ease(mh[1]),
            )  # not a preferred line: nearest, easier first
        if prefer_nearest:
            return (0 if exact else 1, round(gap, 4), -_ease(mh[1]))
        return (0 if exact else 1, 0 if exact else -_ease(mh[1]), gap)

    ordered = sorted(pool, key=_rank)

    prices_by_outcome = {p["outcomeId"]: p for p in data.get("prices", [])}
    all_outcomes = data.get("outcomes", [])
    has_market_ref = any("marketId" in o for o in all_outcomes)

    notes = []
    for market, h in ordered:
        market_id = market.get("marketId")
        outs = _outcomes_for_market(market, h, all_outcomes, has_market_ref)

        matched = None
        if line_type == "spread":
            names = [(o.get("name") or "").strip().lower() for o in outs]
            draws = [n for n in names if n in ("draw", "x")]
            if len(outs) == 3 and len(draws) == 1 and allow_three_way:
                team_outs = [
                    o
                    for o in outs
                    if (o.get("name") or "").strip().lower() not in ("draw", "x")
                ]
                three_way, push_prob = True, _push_probability(outs, prices_by_outcome)
            elif len(outs) == 2 and not draws:
                team_outs, three_way, push_prob = outs, False, 0.0
            else:
                notes.append(
                    f"line {_bet_side_line(h):+g}: {len(outs)} selections found"
                )
                continue
            matched = _pick_spread_selection(
                team_outs, outcome_side, team_name, other_team_name, bw_home, bw_away
            )
            if matched is None:
                notes.append(
                    f"line {_bet_side_line(h):+g}: could not tell which selection is {team_name}"
                )
        else:
            three_way, push_prob = False, 0.0
            for o in outs:
                if (o.get("name") or "").strip().lower().startswith(outcome_side):
                    matched = o
                    break

        if matched is None:
            continue

        price = prices_by_outcome.get(matched.get("outcomeId"))
        if not price:
            continue
        price_val = price.get("priceDecimal")
        if price_val in (None, 0):
            continue

        live = {
            "eventId": event_id,
            "marketId": market_id,
            "outcomeId": matched.get("outcomeId"),
            "outcomeName": matched.get("name"),
            "marketName": market.get("displayName"),
            "priceNum": price.get("numerator"),
            "priceDen": price.get("denominator"),
            "priceDec": price_val,
            "handicap": int(round(h * 100)),
            "rawHandicap": h,  # Betway's own (home-perspective) value
            "betSideLine": _bet_side_line(h),  # the line for the team being bet
            "threeWay": three_way,
            "pushProb": push_prob,
            "eventVersion": event_version
            if event_version is not None
            else market.get("version"),
            "marketVersion": market.get("version"),
            "outcomeVersion": matched.get("version"),
            "priceVersion": price.get("version"),
            "serverEmopSource": price.get("emopSource"),
            "publicHubPublishedTime": price.get("publicHubPublishedTime"),
        }
        return live, ""

    detail = ("; ".join(notes[:3])) if notes else "no price on the selection"
    return (
        None,
        f"Betway's {keyword} markets near {target_line:g} have no usable 2-way selection or price ({detail})",
    )


def main():
    football_cache = OddsCache()
    football_cache.start()

    basketball_cache = BasketballOddsCache()
    basketball_cache.start()

    while not (football_cache.is_ready() and basketball_cache.is_ready()):
        time.sleep(0.1)

    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        football_cache.stop()
        basketball_cache.stop()


if __name__ == "__main__":
    main()
