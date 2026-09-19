from difflib import SequenceMatcher

SPORT_ID_SOCCER = "1"
SPORT_ID_BASKETBALL = "3"
SPORT_NAMES = {SPORT_ID_SOCCER: "football", SPORT_ID_BASKETBALL: "basketball"}

MONEYLINE_KEYWORDS = {
    "football": "1x2",
    "basketball": "winner",
}

MIN_DROP_PERCENT = 12.0
MIN_MINUTES_TO_KICKOFF = 30
MAX_MINUTES_TO_KICKOFF = 24 * 60
MIN_EV_PERCENT = 3.0
MAX_NVP = 3.0

# Single consistent threshold for all fuzzy team/event name matching —
# was previously scattered as 0.6 (event matching) vs 0.8 (outcome/team
# matching within a market), which missed real matches inconsistently.
TEAM_MATCH_THRESHOLD = 0.70

# Safety cap for nearest-line search so an absurdly distant line doesn't
# get treated as "nearest available" just because nothing closer exists.
MAX_LINE_DISTANCE = 5.0


class Stats:
    """Running counters across the whole session, printed after every
    single event evaluated — not per-run-of-the-script, cumulative for
    as long as the process stays up."""

    def __init__(self):
        self.total_fetched = 0
        self.total_passed = 0
        self.total_failed = 0
        self.not_found_on_betway = 0

    def record_fetch(self):
        self.total_fetched += 1

    def record_pass(self):
        self.total_passed += 1

    def record_fail(self, reason: str):
        self.total_failed += 1
        if reason == "no matching Betway event found":
            self.not_found_on_betway += 1

    def print_summary(self):
        print(
            f"📊 fetched: {self.total_fetched} | passed: {self.total_passed} | "
            f"failed: {self.total_failed} | not found on Betway: {self.not_found_on_betway}"
        )


stats = Stats()


def power_method_devig(prices: list, tol=1e-10, max_iter=200):
    implied_probs = [1.0 / p for p in prices]
    overround = sum(implied_probs)

    if overround <= 1.0:
        return implied_probs

    k_low, k_high = 1.0, 2.0
    while sum(p**k_high for p in implied_probs) > 1.0:
        k_high *= 2
        if k_high > 1000:
            break

    k_mid = k_high
    for _ in range(max_iter):
        k_mid = (k_low + k_high) / 2
        s = sum(p**k_mid for p in implied_probs)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            k_low = k_mid
        else:
            k_high = k_mid

    return [p**k_mid for p in implied_probs]


def get_market_prices_and_outcomes(event: dict):
    line_type = event.get("lineType")

    if line_type == "money_line":
        # FIX: compare as string on both sides — Pinnacle may send this as
        # an int or a str depending on endpoint/sport; str() covers both.
        if str(event.get("moneylineNumberOfWays")) == "3":
            return (
                [
                    float(event["priceHome"]),
                    float(event["priceDraw"]),
                    float(event["priceAway"]),
                ],
                ["home", "draw", "away"],
            )
        return [float(event["priceHome"]), float(event["priceAway"])], ["home", "away"]

    if line_type == "spread":
        return [float(event["priceHome"]), float(event["priceAway"])], ["home", "away"]

    if line_type == "total":
        return [float(event["priceOver"]), float(event["priceUnder"])], [
            "over",
            "under",
        ]

    return None, None


def compute_nvp_and_fair_prob(event: dict, outcome_label: str):
    prices, outcomes = get_market_prices_and_outcomes(event)
    if prices is None:
        return None, None

    fair_probs = power_method_devig(prices)
    try:
        idx = outcomes.index(outcome_label)
    except ValueError:
        return None, None

    fair_prob = fair_probs[idx]
    nvp = 1.0 / fair_prob
    return nvp, fair_prob


def is_alt_stat_market(event: dict) -> bool:
    home = event.get("home", "")
    away = event.get("away", "")
    league = event.get("leagueName", "")
    alt_markers = ("(Corners)", "(Bookings)")
    if any(marker in home or marker in away for marker in alt_markers):
        return True
    if "Corners" in league or "Bookings" in league:
        return True
    return False


def identify_sport(event: dict):
    return SPORT_NAMES.get(event.get("sportId"))


def minutes_to_kickoff(event: dict):
    try:
        starts_ms = int(event.get("starts", 0))
        timestamp_ms = int(event.get("timestamp", 0))
    except (ValueError, TypeError):
        return None
    return (starts_ms - timestamp_ms) / 1000 / 60


def _normalize(name: str) -> str:
    return (name or "").lower().strip()


def _team_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def find_matching_betway_event(
    pinnacle_event: dict,
    betway_rows: list,
    kickoff_tolerance_minutes=15,
    min_score=TEAM_MATCH_THRESHOLD,
):
    try:
        p_kickoff = int(pinnacle_event.get("starts", 0)) / 1000
    except (ValueError, TypeError):
        return None

    p_home = pinnacle_event.get("home", "")
    p_away = pinnacle_event.get("away", "")

    best_row = None
    best_score = 0.0

    for row in betway_rows:
        b_kickoff = row.get("kickoff_epoch")
        if b_kickoff is None:
            continue
        if abs(b_kickoff - p_kickoff) > kickoff_tolerance_minutes * 60:
            continue

        home_sim = _team_similarity(p_home, row.get("home", ""))
        away_sim = _team_similarity(p_away, row.get("away", ""))
        score = (home_sim + away_sim) / 2

        if score > best_score:
            best_score = score
            best_row = row

    if best_score < min_score:
        return None
    return best_row


def _find_nearest_line_outcome(
    market_outcomes, target_name_filter, points, max_distance=MAX_LINE_DISTANCE
):
    """
    Pure nearest-neighbor line search: scans EVERY available line for
    outcomes matching target_name_filter (e.g. "over", or None for
    spread sides) and returns whichever is numerically closest to
    `points` — works regardless of decimal granularity (0.25, 0.1,
    whole numbers, etc). Returns (price, actual_line, is_exact) or
    (None, None, None) if nothing is within max_distance.
    """
    best_price = None
    best_line = None
    best_diff = None

    for o in market_outcomes:
        if target_name_filter is not None and o["name"].lower() != target_name_filter:
            continue
        try:
            line_val = float(o["line"])
        except (ValueError, TypeError):
            continue

        diff = abs(line_val - points)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_line = line_val
            best_price = o["price"]

    if best_price is None:
        return None, None, None
    if best_diff > max_distance:
        return None, None, None

    is_exact = best_diff <= 0.01
    return best_price, best_line, is_exact


def find_betway_price(betway_row: dict, pinnacle_event: dict, sport: str):
    line_type = pinnacle_event.get("lineType")
    outcome_side = (pinnacle_event.get("outcome") or "").lower()

    try:
        points = float(pinnacle_event.get("points") or 0)
    except (ValueError, TypeError):
        points = 0.0

    team_name = (
        pinnacle_event["home"] if outcome_side == "home" else pinnacle_event["away"]
    )

    for market in betway_row.get("markets", []):
        display_name = (market.get("displayName") or "").lower()

        if line_type == "money_line":
            keyword = MONEYLINE_KEYWORDS.get(sport)
            if not keyword or keyword not in display_name.replace(" ", ""):
                continue
            for o in market["outcomes"]:
                if _team_similarity(o["name"], team_name) >= TEAM_MATCH_THRESHOLD:
                    return o["price"], None, True  # no line concept for moneyline

        elif line_type == "spread" and "handicap" in display_name:
            matching_outcomes = [
                o
                for o in market["outcomes"]
                if _team_similarity(o["name"], team_name) >= TEAM_MATCH_THRESHOLD
            ]
            price, actual_line, is_exact = _find_nearest_line_outcome(
                matching_outcomes, target_name_filter=None, points=points
            )
            if price is not None:
                return price, actual_line, is_exact

        elif line_type == "total" and "total" in display_name:
            price, actual_line, is_exact = _find_nearest_line_outcome(
                market["outcomes"], target_name_filter=outcome_side, points=points
            )
            if price is not None:
                return price, actual_line, is_exact

    return None, None, None


def _describe_event(event: dict) -> str:
    return (
        f"{event.get('home')} vs {event.get('away')} [{event.get('leagueName')}] "
        f"{event.get('lineType')} pts={event.get('points')} outcome={event.get('outcome')}"
    )


def evaluate_pinnacle_event(pinnacle_event: dict, betway_rows: list):
    """
    Generic filter: drop% -> time-to-kickoff -> NVP -> Betway match -> EV.
    Always prints exactly one PASS or FAIL line, followed by a running
    stats summary for the whole session.
    """
    stats.record_fetch()
    desc = _describe_event(pinnacle_event)

    sport = identify_sport(pinnacle_event)
    if sport is None:
        print(f"❌ FAIL [unsupported sport] {desc}")
        stats.record_fail("unsupported sport")
        stats.print_summary()
        return None

    try:
        drop_pct = float(pinnacle_event.get("percentageChange", 0))
    except (ValueError, TypeError):
        drop_pct = 0.0
    if drop_pct < MIN_DROP_PERCENT:
        print(f"❌ FAIL [drop {drop_pct:.1f}% < min {MIN_DROP_PERCENT}%] {desc}")
        stats.record_fail("drop too low")
        stats.print_summary()
        return None

    mins_to_kickoff = minutes_to_kickoff(pinnacle_event)
    if mins_to_kickoff is None:
        print(f"❌ FAIL [could not compute time-to-kickoff] {desc}")
        stats.record_fail("no kickoff time")
        stats.print_summary()
        return None
    if mins_to_kickoff < MIN_MINUTES_TO_KICKOFF:
        print(
            f"❌ FAIL [only {mins_to_kickoff:.0f}min to kickoff, min {MIN_MINUTES_TO_KICKOFF}] {desc}"
        )
        stats.record_fail("too close to kickoff")
        stats.print_summary()
        return None
    if mins_to_kickoff > MAX_MINUTES_TO_KICKOFF:
        print(
            f"❌ FAIL [{mins_to_kickoff:.0f}min to kickoff, max {MAX_MINUTES_TO_KICKOFF}] {desc}"
        )
        stats.record_fail("too far from kickoff")
        stats.print_summary()
        return None

    outcome_label = (pinnacle_event.get("outcome") or "").lower()
    nvp, fair_prob = compute_nvp_and_fair_prob(pinnacle_event, outcome_label)
    if nvp is None:
        print(f"❌ FAIL [could not compute NVP] {desc}")
        stats.record_fail("no NVP")
        stats.print_summary()
        return None

    if nvp > MAX_NVP:
        print(f"❌ FAIL [NVP {nvp:.3f} > max {MAX_NVP}] {desc}")
        stats.record_fail("NVP too high")
        stats.print_summary()
        return None

    betway_row = find_matching_betway_event(pinnacle_event, betway_rows)
    if betway_row is None:
        print(
            f"❌ FAIL [no matching Betway event found (threshold {TEAM_MATCH_THRESHOLD})] {desc}"
        )
        stats.record_fail("no matching Betway event found")
        stats.print_summary()
        return None

    betway_price, matched_line, is_exact = find_betway_price(
        betway_row, pinnacle_event, sport
    )
    if betway_price is None:
        print(f"❌ FAIL [matched event but not this market/outcome on Betway] {desc}")
        stats.record_fail("market/outcome not found on Betway")
        stats.print_summary()
        return None

    line_note = (
        ""
        if is_exact
        else f" ⚠️ APPROX LINE (Pinnacle pts={pinnacle_event.get('points')} → Betway nearest={matched_line})"
    )
    if line_note:
        print(f"[filter] {line_note.strip()}")

    ev_percent = (betway_price * fair_prob - 1.0) * 100.0
    if ev_percent < MIN_EV_PERCENT:
        print(
            f"❌ FAIL [EV {ev_percent:.2f}% < min {MIN_EV_PERCENT}%] {desc} "
            f"| Betway {betway_price} vs NVP {nvp:.3f}"
        )
        stats.record_fail("EV too low")
        stats.print_summary()
        return None

    result = {
        "pinnacle_event_id": pinnacle_event.get("eventId"),
        "betway_event_id": betway_row.get("eventId"),
        "home": pinnacle_event.get("home"),
        "away": pinnacle_event.get("away"),
        "league": pinnacle_event.get("leagueName"),
        "sport": sport,
        "line_type": pinnacle_event.get("lineType"),
        "outcome": outcome_label,
        "points": pinnacle_event.get("points"),
        "drop_percent": round(drop_pct, 2),
        "minutes_to_kickoff": round(mins_to_kickoff, 1),
        "nvp": round(nvp, 3),
        "betway_price": betway_price,
        "betway_matched_line": matched_line,
        "line_matched_exactly": is_exact,
        "ev_percent": round(ev_percent, 2),
        "is_alt_stat_market": is_alt_stat_market(pinnacle_event),
    }

    exact_tag = "" if is_exact else " ⚠️ APPROX LINE"
    print(
        f"✅ PASS{exact_tag} [{result['sport']}/{result['line_type']}] {result['home']} vs {result['away']} "
        f"[{result['league']}] {result['outcome']} {result['points']} "
        f"| drop {result['drop_percent']}% | {result['minutes_to_kickoff']:.0f}min to kickoff "
        f"| Betway {result['betway_price']} vs NVP {result['nvp']} "
        f"| EV {result['ev_percent']}%"
    )
    stats.record_pass()
    stats.print_summary()

    return result
