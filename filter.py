from difflib import SequenceMatcher

# ============================================================
# RULES — anything not listed here fails the criteria by default.
# ============================================================

RULES = {
    ("football", "moneyline"): {"min_ev": 6.0, "max_nvp": 2.7},
    ("football", "totals_over"): {"min_ev": 4.0, "max_nvp": 2.5},
    ("football", "spread_positive"): {"min_ev": 5.0, "max_nvp": 2.5},
}

SPORT_ID_SOCCER = "1"  # confirmed directly from real sword data


# ============================================================
# POWER METHOD DEVIG
# ============================================================


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
    """
    Pulls the full current market prices directly off the Pinnacle event
    itself. All three market shapes below are confirmed against real
    sword.pinnacleoddsdropper.com data — no guessed fields remain.
    """
    line_type = event.get("lineType")

    if line_type == "money_line":
        if event.get("moneylineNumberOfWays") == "3":
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


# ============================================================
# CLASSIFICATION
# ============================================================


def is_alt_stat_market(event: dict) -> bool:
    """
    Detects corners/other alt-stat totals (e.g. "Teplice (Corners)"),
    which come through as normal lineType="total" events but aren't
    match-goals totals. Currently NOT excluded automatically — see the
    open question about whether these should count under your Totals
    rule. Exposed here so you can wire in exclusion once decided.
    """
    home = event.get("home", "")
    away = event.get("away", "")
    league = event.get("leagueName", "")
    return "(Corners)" in home or "(Corners)" in away or "Corners" in league


def classify_pinnacle_event(event: dict):
    if event.get("sportId") != SPORT_ID_SOCCER:
        return None  # basketball etc. — not handled yet

    line_type = event.get("lineType")

    if line_type == "money_line":
        return ("football", "moneyline")

    if line_type == "spread":
        try:
            points = float(event.get("points") or 0)
        except (ValueError, TypeError):
            return None
        if points > 0:
            return ("football", "spread_positive")
        return None  # negative/zero spread — not on the list, fails

    if line_type == "total":
        outcome = (event.get("outcome") or "").lower()
        if outcome != "over":
            return None  # unders excluded explicitly
        # NOTE: alt-stat totals (corners, etc.) currently pass through
        # this same rule — is_alt_stat_market() exists but isn't applied
        # yet, pending your answer on whether that's correct.
        return ("football", "totals_over")

    return None


# ============================================================
# FUZZY MATCHING — Pinnacle event -> Betway row
# ============================================================


def _normalize(name: str) -> str:
    return (name or "").lower().strip()


def _team_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def find_matching_betway_event(
    pinnacle_event: dict, betway_rows: list, kickoff_tolerance_minutes=15, min_score=0.6
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


# ============================================================
# FINDING THE ACTUAL BETWAY PRICE FOR THIS SPECIFIC BET
# ============================================================


def find_betway_price(
    betway_row: dict, pinnacle_event: dict, classification: tuple, tolerance=0.01
):
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
                if _team_similarity(o["name"], team_name) > 0.8:
                    return o["price"]

        elif market_kind == "spread_positive" and "handicap" in display_name:
            for o in market["outcomes"]:
                if _team_similarity(o["name"], team_name) < 0.8:
                    continue
                try:
                    line_val = float(o["line"])
                except (ValueError, TypeError):
                    continue
                if abs(line_val - points) <= tolerance:
                    return o["price"]

        elif market_kind == "totals_over" and "total" in display_name:
            for o in market["outcomes"]:
                if o["name"].lower() != "over":
                    continue
                try:
                    line_val = float(o["line"])
                except (ValueError, TypeError):
                    continue
                if abs(line_val - points) <= tolerance:
                    return o["price"]

    return None


# ============================================================
# PUTTING IT TOGETHER
# ============================================================


def evaluate_pinnacle_event(pinnacle_event: dict, betway_rows: list):
    classification = classify_pinnacle_event(pinnacle_event)
    if classification is None:
        return None

    rule = RULES[classification]
    outcome_label = (pinnacle_event.get("outcome") or "").lower()

    nvp, fair_prob = compute_nvp_and_fair_prob(pinnacle_event, outcome_label)
    if nvp is None:
        return None

    if nvp > rule["max_nvp"]:
        return None

    betway_row = find_matching_betway_event(pinnacle_event, betway_rows)
    if betway_row is None:
        return None

    betway_price = find_betway_price(betway_row, pinnacle_event, classification)
    if betway_price is None:
        return None

    ev_percent = (betway_price * fair_prob - 1.0) * 100.0
    if ev_percent < rule["min_ev"]:
        return None

    return {
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
        "ev_percent": round(ev_percent, 2),
        "is_alt_stat_market": is_alt_stat_market(pinnacle_event),
    }
