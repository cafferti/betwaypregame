from difflib import SequenceMatcher

RULES = {
    ("football", "moneyline"): {"min_ev": 6.0, "max_nvp": 2.7},
    ("football", "totals_over"): {"min_ev": 4.0, "max_nvp": 2.5},
    ("football", "spread_positive"): {"min_ev": 5.0, "max_nvp": 2.5},
}

SPORT_ID_SOCCER = "1"

SPORT_ID_NAMES = {
    SPORT_ID_SOCCER: "soccer",
}

# Single consistent threshold used everywhere we fuzzy-match team/event names.
# Lowered from the old scattered 0.6 / 0.8 values so fewer real matches get
# missed due to punctuation, abbreviations, or naming differences between
# Pinnacle and Betway (e.g. "St." vs "Saint", "II" vs "B", etc.)
TEAM_MATCH_THRESHOLD = 0.70

# Safety cap so we don't match wildly unrelated lines (e.g. Pinnacle's 3.75
# accidentally matching a stray 50.5 line from a different market bleeding
# in). Generous enough to always find a real nearby line in practice.
MAX_LINE_DISTANCE = 5.0

_already_evaluated = set()


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
    return "(Corners)" in home or "(Corners)" in away or "Corners" in league


def classify_pinnacle_event(event: dict):
    sport_id = event.get("sportId")

    if sport_id != SPORT_ID_SOCCER:
        sport_name = SPORT_ID_NAMES.get(sport_id, f"sportId={sport_id}")
        return None, f"other sport ({sport_name}) — not handled yet"

    line_type = event.get("lineType")

    if line_type == "money_line":
        return ("football", "moneyline"), None

    if line_type == "spread":
        try:
            points = float(event.get("points") or 0)
        except (ValueError, TypeError):
            return None, "invalid points value"
        if points > 0:
            return ("football", "spread_positive"), None
        return (
            None,
            "negative-point spread — excluded by design (only underdog/+points evaluated)",
        )

    if line_type == "total":
        outcome = (event.get("outcome") or "").lower()
        if outcome != "over":
            return None, "'under' outcome — excluded by design (only 'over' evaluated)"
        return ("football", "totals_over"), None

    return None, f"unhandled lineType '{line_type}'"


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
    Pure nearest-neighbor line search: looks at EVERY available line on
    Betway for outcomes matching target_name_filter (e.g. "over"), and
    returns whichever one is numerically closest to `points` — regardless
    of decimal granularity (works for 0.25 lines, 0.1 lines, whole numbers,
    anything). No fixed step size, so nothing gets skipped over.

    Returns (price, actual_line, is_exact) or (None, None, None) if no
    candidate exists within max_distance.
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


def find_betway_price(betway_row: dict, pinnacle_event: dict, classification: tuple):
    _, market_kind = classification
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

        if market_kind == "moneyline" and "1x2" in display_name.replace(" ", ""):
            for o in market["outcomes"]:
                if _team_similarity(o["name"], team_name) >= TEAM_MATCH_THRESHOLD:
                    return o["price"], None, True  # no line concept for moneyline

        elif market_kind == "spread_positive" and "handicap" in display_name:
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

        elif market_kind == "totals_over" and "total" in display_name:
            price, actual_line, is_exact = _find_nearest_line_outcome(
                market["outcomes"], target_name_filter="over", points=points
            )
            if price is not None:
                return price, actual_line, is_exact

        elif market_kind == "totals_under" and "total" in display_name:
            # Only reached if "under" is later enabled in classify_pinnacle_event
            price, actual_line, is_exact = _find_nearest_line_outcome(
                market["outcomes"], target_name_filter="under", points=points
            )
            if price is not None:
                return price, actual_line, is_exact

    return None, None, None


def _describe_event(event: dict) -> str:
    return (
        f"{event.get('home')} vs {event.get('away')} [{event.get('leagueName')}] "
        f"{event.get('lineType')} pts={event.get('points')} outcome={event.get('outcome')} "
        f"price={event.get('changeTo')}"
    )


def _dedupe_key(event: dict):
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


def evaluate_pinnacle_event(pinnacle_event: dict, betway_rows: list):
    key = _dedupe_key(pinnacle_event)
    if key in _already_evaluated:
        return None
    _already_evaluated.add(key)

    desc = _describe_event(pinnacle_event)
    print(f"[filter] evaluating: {desc}")

    classification, skip_reason = classify_pinnacle_event(pinnacle_event)
    if classification is None:
        print(f"⏭️  SKIP [{skip_reason}] {desc}")
        return None

    rule = RULES[classification]
    outcome_label = (pinnacle_event.get("outcome") or "").lower()

    nvp, fair_prob = compute_nvp_and_fair_prob(pinnacle_event, outcome_label)
    if nvp is None:
        print(f"❌ FAIL [could not compute NVP] {desc}")
        return None
    print(f"[filter] NVP computed: {nvp:.3f} (need ≤ {rule['max_nvp']}) — {desc}")

    if nvp > rule["max_nvp"]:
        print(f"❌ FAIL [NVP {nvp:.3f} > max {rule['max_nvp']}] {desc}")
        return None

    print(
        f"[filter] searching Betway ({len(betway_rows)} rows cached) for match — {desc}"
    )
    betway_row = find_matching_betway_event(pinnacle_event, betway_rows)
    if betway_row is None:
        print(
            f"❌ FAIL [no matching Betway event found (threshold {TEAM_MATCH_THRESHOLD})] {desc}"
        )
        return None
    print(
        f"[filter] ✅ matched Betway event: {betway_row.get('home')} vs {betway_row.get('away')} "
        f"(eventId={betway_row.get('eventId')})"
    )

    betway_price, matched_line, is_exact = find_betway_price(
        betway_row, pinnacle_event, classification
    )
    if betway_price is None:
        print(f"❌ FAIL [matched event but not this market/outcome on Betway] {desc}")
        return None

    line_note = (
        ""
        if is_exact
        else f" ⚠️ APPROX LINE (Pinnacle pts={pinnacle_event.get('points')} → Betway nearest line={matched_line})"
    )
    print(f"[filter] found Betway price: {betway_price}{line_note} — computing EV...")

    ev_percent = (betway_price * fair_prob - 1.0) * 100.0
    if ev_percent < rule["min_ev"]:
        print(
            f"❌ FAIL [EV {ev_percent:.2f}% < min {rule['min_ev']}%] {desc} "
            f"| Betway {betway_price} vs NVP {nvp:.3f}"
        )
        return None

    result = {
        "pinnacle_event_id": pinnacle_event.get("eventId"),
        "betway_event_id": betway_row.get("eventId"),
        "home": pinnacle_event.get("home"),
        "away": pinnacle_event.get("away"),
        "league": pinnacle_event.get("leagueName"),
        "market_kind": classification[1],
        "outcome": outcome_label,
        "points": pinnacle_event.get("points"),
        "nvp": round(nvp, 3),
        "betway_price": betway_price,
        "betway_matched_line": matched_line,
        "line_matched_exactly": is_exact,
        "ev_percent": round(ev_percent, 2),
        "is_alt_stat_market": is_alt_stat_market(pinnacle_event),
    }

    exact_tag = "" if is_exact else " ⚠️ APPROX LINE"
    print(
        f"✅ PASS{exact_tag} [{result['market_kind']}] {result['home']} vs {result['away']} "
        f"[{result['league']}] {result['outcome']} {result['points']}→{matched_line} "
        f"| Betway {result['betway_price']} vs NVP {result['nvp']} "
        f"| EV {result['ev_percent']}%"
    )

    return result
