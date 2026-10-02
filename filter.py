from difflib import SequenceMatcher
from engineoddsretriever import resolve_live_bet_target

SPORT_ID_SOCCER = "1"
SPORT_ID_BASKETBALL = "3"
SPORT_NAMES = {SPORT_ID_SOCCER: "football", SPORT_ID_BASKETBALL: "basketball"}

MONEYLINE_KEYWORDS = {
    "football": "1x2",
    "basketball": "winner",
}

TEAM_MATCH_THRESHOLD = 0.60

# Per-category (sport, line_type, side) -> (min_ev_percent, max_nvp).
# side is "over"/"under" for totals, "+"/"-" for spreads, None for moneyline.
# A missing or None entry means that category is EXCLUDED — no bets placed.
CATEGORY_THRESHOLDS = {
    ("basketball", "total", "over"): (4.0, 2.5),
    ("basketball", "total", "under"): None,  # unders excluded
    ("basketball", "spread", "+"): (5.0, 2.5),
    ("basketball", "spread", "-"): (6.0, 2.5),
    ("basketball", "money_line", None): (7.0, 2.5),
    ("football", "money_line", None): (6.0, 2.7),
    ("football", "total", "over"): (4.0, 2.5),
    ("football", "total", "under"): None,  # unders excluded
    ("football", "spread", "+"): (5.0, 2.5),
    ("football", "spread", "-"): None,  # PENDING — no criteria given yet
}


class Stats:
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

    def summary_line(self):
        return (
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
    """Kept for display only — not used as a filter anymore."""
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


def _get_category_thresholds(sport: str, line_type: str, outcome_label: str, points):
    if line_type == "money_line":
        key = (sport, "money_line", None)
    elif line_type == "total":
        side = outcome_label if outcome_label in ("over", "under") else None
        key = (sport, "total", side)
    elif line_type == "spread":
        try:
            pts = float(points) if points not in (None, "") else 0.0
        except (ValueError, TypeError):
            pts = 0.0
        side = "+" if pts >= 0 else "-"
        key = (sport, "spread", side)
    else:
        return None

    return CATEGORY_THRESHOLDS.get(key)


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


def find_betway_price_moneyline(betway_row: dict, pinnacle_event: dict, sport: str):
    """
    Cached-data moneyline lookup. "draw" is matched explicitly against
    the outcome's own name, not inferred as "not home" — matching the
    away team's name for a draw would misprice or mis-bet.
    """
    outcome_side = (pinnacle_event.get("outcome") or "").lower()

    if outcome_side == "draw":
        target_name = "draw"
        is_draw = True
    else:
        target_name = (
            pinnacle_event["home"] if outcome_side == "home" else pinnacle_event["away"]
        )
        is_draw = False

    for market in betway_row.get("markets", []):
        display_name = (market.get("displayName") or "").lower()
        keyword = MONEYLINE_KEYWORDS.get(sport)
        if not keyword or keyword not in display_name.replace(" ", ""):
            continue
        for o in market["outcomes"]:
            outcome_name = (o.get("name") or "").strip().lower()
            if is_draw:
                if outcome_name == "draw":
                    return o["price"], o
            else:
                if _team_similarity(o["name"], target_name) >= TEAM_MATCH_THRESHOLD:
                    return o["price"], o

    return None, None


def _describe(pinnacle_event: dict) -> str:
    return (
        f"{pinnacle_event.get('home')} vs {pinnacle_event.get('away')} "
        f"[{pinnacle_event.get('leagueName')}] {pinnacle_event.get('lineType')} "
        f"{pinnacle_event.get('outcome')} {pinnacle_event.get('points')}"
    )


def _fmt(val, suffix=""):
    return "N/A" if val is None else f"{val:.2f}{suffix}"


def _log(passed: bool, reason: str, pinnacle_event: dict, ev_percent, nvp):
    icon = "✅ PASS" if passed else "❌ FAIL"
    print(
        f"{icon} [{reason}] {_describe(pinnacle_event)} "
        f"| EV: {_fmt(ev_percent, '%')} | NVP: {_fmt(nvp)}"
    )


def evaluate_pinnacle_event(pinnacle_event: dict, betway_rows: list):
    """
    Every single alert gets a printed PASS/FAIL line with the reason,
    plus EV and NVP wherever they could be computed (N/A otherwise).
    Only the category EV/NVP table (CATEGORY_THRESHOLDS) decides
    pass/fail — no drop%/kickoff-window pre-filtering anymore.
    """
    stats.record_fetch()

    sport = identify_sport(pinnacle_event)
    if sport is None:
        stats.record_fail("unsupported sport")
        _log(False, "unsupported sport", pinnacle_event, None, None)
        return None

    outcome_label = (pinnacle_event.get("outcome") or "").lower()
    nvp, fair_prob = compute_nvp_and_fair_prob(pinnacle_event, outcome_label)
    if nvp is None:
        stats.record_fail("no NVP")
        _log(False, "could not compute NVP", pinnacle_event, None, None)
        return None

    line_type = pinnacle_event.get("lineType")

    thresholds = _get_category_thresholds(
        sport, line_type, outcome_label, pinnacle_event.get("points")
    )
    if thresholds is None:
        stats.record_fail("category excluded or unsupported")
        _log(
            False,
            "category excluded (unders / pending / unsupported)",
            pinnacle_event,
            None,
            nvp,
        )
        return None
    min_ev_percent, max_nvp = thresholds

    if nvp > max_nvp:
        stats.record_fail("NVP too high for category")
        _log(False, f"NVP {nvp:.3f} > max {max_nvp}", pinnacle_event, None, nvp)
        return None

    betway_row = find_matching_betway_event(pinnacle_event, betway_rows)
    if betway_row is None:
        stats.record_fail("no matching Betway event found")
        _log(False, "no matching Betway event found", pinnacle_event, None, nvp)
        return None

    if line_type == "money_line":
        betway_price, matched_outcome = find_betway_price_moneyline(
            betway_row, pinnacle_event, sport
        )
        if betway_price is None:
            stats.record_fail("market/outcome not found on Betway")
            _log(False, "market/outcome not found on Betway", pinnacle_event, None, nvp)
            return None

        matched_line = None
        is_exact = True
        placement_fields = {
            "betway_market_id": matched_outcome.get("marketId"),
            "outcomeId": matched_outcome.get("outcomeId"),
            "eventVersion": matched_outcome.get("eventVersion"),
            "marketVersion": matched_outcome.get("marketVersion"),
            "outcomeVersion": matched_outcome.get("outcomeVersion"),
            "priceVersion": matched_outcome.get("priceVersion"),
            "serverEmopSource": matched_outcome.get("serverEmopSource"),
            "publicHubPublishedTime": matched_outcome.get("publicHubPublishedTime"),
            "priceNum": matched_outcome.get("priceNum"),
            "priceDen": matched_outcome.get("priceDen"),
            "betway_handicap_scaled": 0,
        }

    elif line_type in ("spread", "total"):
        try:
            target_line = float(pinnacle_event.get("points") or 0)
        except (ValueError, TypeError):
            target_line = 0.0

        team_name = (
            pinnacle_event["home"]
            if outcome_label == "home"
            else pinnacle_event["away"]
        )

        live = resolve_live_bet_target(
            event_id=betway_row.get("eventId"),
            line_type=line_type,
            target_line=target_line,
            outcome_side=outcome_label,
            team_name=team_name,
        )
        if live is None:
            stats.record_fail("market/outcome not found on Betway (live)")
            _log(False, "no live line match on Betway", pinnacle_event, None, nvp)
            return None

        betway_price = live["priceDec"]
        matched_line = live.get("rawHandicap")
        is_exact = matched_line is not None and abs(matched_line - target_line) <= 0.01
        placement_fields = {
            "betway_market_id": live.get("marketId"),
            "outcomeId": live.get("outcomeId"),
            "eventVersion": live.get("eventVersion"),
            "marketVersion": live.get("marketVersion"),
            "outcomeVersion": live.get("outcomeVersion"),
            "priceVersion": live.get("priceVersion"),
            "serverEmopSource": live.get("serverEmopSource"),
            "publicHubPublishedTime": live.get("publicHubPublishedTime"),
            "priceNum": live.get("priceNum"),
            "priceDen": live.get("priceDen"),
            "betway_handicap_scaled": live.get("handicap"),
        }

    else:
        stats.record_fail("unsupported line type")
        _log(False, "unsupported line type", pinnacle_event, None, nvp)
        return None

    ev_percent = (betway_price * fair_prob - 1.0) * 100.0

    if ev_percent < min_ev_percent:
        stats.record_fail("EV too low for category")
        _log(
            False,
            f"EV {ev_percent:.2f}% < min {min_ev_percent}%",
            pinnacle_event,
            ev_percent,
            nvp,
        )
        return None

    result = {
        "pinnacle_event_id": pinnacle_event.get("eventId"),
        "betway_event_id": betway_row.get("eventId"),
        "home": pinnacle_event.get("home"),
        "away": pinnacle_event.get("away"),
        "league": pinnacle_event.get("leagueName"),
        "sport": sport,
        "line_type": line_type,
        "outcome": outcome_label,
        "points": pinnacle_event.get("points"),
        "minutes_to_kickoff": round(minutes_to_kickoff(pinnacle_event) or 0, 1),
        "nvp": round(nvp, 3),
        "betway_price": betway_price,
        "betway_matched_line": matched_line,
        "line_matched_exactly": is_exact,
        "ev_percent": round(ev_percent, 2),
        "is_alt_stat_market": is_alt_stat_market(pinnacle_event),
        "betway_line": matched_line,
        **placement_fields,
    }

    stats.record_pass()
    _log(True, "passed category thresholds", pinnacle_event, ev_percent, nvp)
    return result
