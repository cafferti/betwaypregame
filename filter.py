from difflib import SequenceMatcher
from engineoddsretriever import resolve_live_bet_target, MAX_LINE_GAP
import controls

SPORT_ID_SOCCER = "1"
SPORT_ID_BASKETBALL = "3"
SPORT_NAMES = {SPORT_ID_SOCCER: "football", SPORT_ID_BASKETBALL: "basketball"}

MONEYLINE_KEYWORDS = {
    "football": "1x2",
    "basketball": "winner",
}

TEAM_MATCH_THRESHOLD = 0.60

# EXPERIMENT: when a moneyline signal passes (sports listed below), bet the OPPOSITE side
# (home signal -> bet away, away signal -> bet home). Draw signals are left alone.
# The CSV EV value gets an "r" in front (e.g. "r8.34") for reversed bets.
REVERSE_MONEYLINE_SPORTS = {"football"}

# A reversed bet is only placed when the reversed side's Betway odds are BELOW the cap;
# at or above it the bet is skipped. A sport that isn't listed has no cap.
REVERSE_MAX_ODDS = {"football": 2.7}

# EXPERIMENT: basketball totals may use a Betway line up to this far from
# Pinnacle's line (all other categories keep the engine's MAX_LINE_GAP).
BASKETBALL_TOTAL_MAX_LINE_GAP = 7.0

# Football spreads: Betway only offers a few lines per match, so use the Betway line
# CLOSEST to Pinnacle's, as long as it is within this many goals.
FOOTBALL_SPREAD_MAX_LINE_GAP = 1.0
FOOTBALL_TOTAL_MAX_LINE_GAP = 1.0  # same idea for football overs / unders

# Basketball spreads: how far the Betway line may be from Pinnacle's, in the bet side's own
# points. (down, up when Pinnacle's line is positive, up when it is negative); "up" = easier.
BASKETBALL_SPREAD_WINDOW = (3.0, 6.0, 3.0)

# Pinnacle also posts corners / bookings markets. They are not goals markets, so they must
# never be matched to a Betway goals market (that is how wrong bets were placed).
BLOCK_ALT_STAT_MARKETS = True

# Only full-game alerts are bet (period 0). Football: 1 = first half, 2 = second half.
# Basketball: 1-4 = quarters. Betway's period markets are not mapped yet, so those are skipped.
ALLOWED_PERIODS = {0}
# ...except where the Betway market names are known: football spreads (handicaps) also
# cover the 1st half (period 1) and 2nd half (period 2).
ALLOWED_PERIODS_BY_MARKET = {("football", "spread"): {0, 1, 2}}

# Safety net for football spreads / totals: an EV above this is almost certainly a wrong
# line or the wrong selection (it happened: 131%), so the bet is skipped. None = off.
MAX_FOOTBALL_LINE_EV_PERCENT = 60.0

# Per-category (sport, line_type, side) -> (min_ev_percent, max_nvp).
# side is "over"/"under" for totals, "+"/"-" for spreads, None for moneyline.
# A missing or None entry means that category is EXCLUDED — no bets placed.
CATEGORY_THRESHOLDS = {
    ("basketball", "total", "over"): (4.0, 2.5),
    ("basketball", "total", "under"): (
        4.0,
        2.5,
    ),  # unders now enabled (same criteria as overs)
    ("basketball", "spread", "+"): (5.0, 2.5),
    ("basketball", "spread", "-"): (6.0, 2.5),
    ("basketball", "money_line", None): (7.0, 2.5),
    ("football", "money_line", None): (6.0, 2.7),
    ("football", "total", "over"): (4.0, 2.5),
    ("football", "total", "under"): (
        4.0,
        2.5,
    ),  # unders now enabled (same criteria as overs)
    ("football", "spread", "+"): (5.0, 2.5),
    ("football", "spread", "-"): (
        5.0,
        2.5,
    ),  # now enabled (same criteria as "+" spreads)
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


def get_pinnacle_price(event: dict, outcome_label: str):
    """Raw (vig-included) Pinnacle price for the outcome being bet — for logging only."""
    key_map = {
        "home": "priceHome",
        "away": "priceAway",
        "draw": "priceDraw",
        "over": "priceOver",
        "under": "priceUnder",
    }
    key = key_map.get(outcome_label)
    if key is None or event.get(key) is None:
        return None
    try:
        return float(event[key])
    except (ValueError, TypeError):
        return None


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
    Cached-data moneyline lookup with an EXPLICIT choice for each outcome:

        outcome == "home"  -> the selection whose name matches the HOME team
        outcome == "draw"  -> the selection literally named "Draw" (3-way markets only)
        outcome == "away"  -> the selection whose name matches the AWAY team
        anything else      -> no bet

    A team selection must match its own side better than the other side, so a
    home bet can never be placed on the away selection (or the other way round).
    """
    side = (pinnacle_event.get("outcome") or "").strip().lower()
    if side not in ("home", "draw", "away"):
        return None, None

    keyword = MONEYLINE_KEYWORDS.get(sport)
    if not keyword:
        return None, None

    home_name = pinnacle_event.get("home", "")
    away_name = pinnacle_event.get("away", "")

    for market in betway_row.get("markets", []):
        display_name = (market.get("displayName") or "").lower()
        if keyword not in display_name.replace(" ", ""):
            continue

        for o in market["outcomes"]:
            outcome_name = (o.get("name") or "").strip().lower()

            if side == "draw":
                if outcome_name in ("draw", "x"):
                    return o["price"], o
                continue

            home_sim = _team_similarity(outcome_name, home_name)
            away_sim = _team_similarity(outcome_name, away_name)

            if side == "home":
                if home_sim >= TEAM_MATCH_THRESHOLD and home_sim > away_sim:
                    return o["price"], o
            else:  # side == "away"
                if away_sim >= TEAM_MATCH_THRESHOLD and away_sim > home_sim:
                    return o["price"], o

    return None, None


def _moneyline_placement_fields(matched_outcome: dict) -> dict:
    return {
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
    pass/fail.
    """
    stats.record_fetch()

    sport = identify_sport(pinnacle_event)
    if sport is None:
        stats.record_fail("unsupported sport")
        _log(False, "unsupported sport", pinnacle_event, None, None)
        return None

    if BLOCK_ALT_STAT_MARKETS and is_alt_stat_market(pinnacle_event):
        stats.record_fail("corners/bookings market")
        _log(
            False,
            "corners/bookings market — not a goals market, skipped",
            pinnacle_event,
            None,
            None,
        )
        return None

    try:
        period = int(pinnacle_event.get("periodNumber") or 0)
    except (TypeError, ValueError):
        period = 0
    allowed = ALLOWED_PERIODS_BY_MARKET.get(
        (sport, pinnacle_event.get("lineType")), ALLOWED_PERIODS
    )
    if period not in allowed:
        stats.record_fail("not a full-game period")
        _log(
            False,
            f"period {period} market (not full game) — skipped",
            pinnacle_event,
            None,
            None,
        )
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

    # Dashboard switch: this bet category can be turned off without stopping the bot.
    category = controls.category_of(sport, line_type, outcome_label)
    if not controls.is_enabled(category):
        stats.record_fail("category switched off")
        _log(
            False,
            f"{category} is switched OFF in the dashboard",
            pinnacle_event,
            None,
            nvp,
        )
        return None

    if nvp > max_nvp:
        stats.record_fail("NVP too high for category")
        _log(False, f"NVP {nvp:.3f} > max {max_nvp}", pinnacle_event, None, nvp)
        return None

    betway_row = find_matching_betway_event(pinnacle_event, betway_rows)
    if betway_row is None:
        stats.record_fail("no matching Betway event found")
        _log(False, "no matching Betway event found", pinnacle_event, None, nvp)
        return None

    ev_factor = 1.0  # 3-way handicaps lose where Pinnacle would refund (see push_prob)
    three_way, push_prob = False, 0.0

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
        placement_fields = _moneyline_placement_fields(matched_outcome)

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

        # Basketball totals get a wider acceptable line gap; everything else
        # keeps the engine default.
        nearest = False
        window = None
        if sport == "basketball" and line_type == "spread":
            window = BASKETBALL_SPREAD_WINDOW
            nearest = (
                True  # closest line inside the window (ties go to the easier line)
            )
        elif sport == "basketball" and line_type == "total":
            line_gap = BASKETBALL_TOTAL_MAX_LINE_GAP
        elif sport == "football" and line_type == "spread":
            line_gap = FOOTBALL_SPREAD_MAX_LINE_GAP
            nearest = True  # closest line within the gap
        elif sport == "football" and line_type == "total":
            line_gap = FOOTBALL_TOTAL_MAX_LINE_GAP
            nearest = True
        else:
            line_gap = MAX_LINE_GAP

        other_team_name = (
            pinnacle_event["away"]
            if outcome_label == "home"
            else pinnacle_event["home"]
        )

        # The engine returns (live, reason); accept a single value too.
        result_live = resolve_live_bet_target(
            event_id=betway_row.get("eventId"),
            line_type=line_type,
            target_line=target_line,
            outcome_side=outcome_label,
            team_name=team_name,
            max_line_gap=line_gap,
            prefer_nearest=nearest,
            other_team_name=other_team_name,
            line_rules="football" if sport == "football" else None,
            allow_three_way=(sport == "football" and line_type == "spread"),
            line_window=window,
            period=period,
        )
        if isinstance(result_live, tuple):
            live, reason = result_live
        else:
            live, reason = result_live, ""

        if live is None:
            stats.record_fail("market/outcome not found on Betway (live)")
            _log(
                False,
                reason or "no live line match on Betway",
                pinnacle_event,
                None,
                nvp,
            )
            return None

        betway_price = live["priceDec"]
        three_way = bool(live.get("threeWay"))
        push_prob = live.get("pushProb") or 0.0
        ev_factor = 1.0 - push_prob
        # the line for the team/side being bet — comparable with Pinnacle's point
        matched_line = live.get("betSideLine", live.get("rawHandicap"))
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

    ev_percent = (betway_price * fair_prob * ev_factor - 1.0) * 100.0

    if (
        MAX_FOOTBALL_LINE_EV_PERCENT is not None
        and sport == "football"
        and line_type in ("spread", "total")
        and ev_percent > MAX_FOOTBALL_LINE_EV_PERCENT
    ):
        stats.record_fail("EV implausibly high (line mismatch?)")
        _log(
            False,
            f"EV {ev_percent:.1f}% is above {MAX_FOOTBALL_LINE_EV_PERCENT:g}% — "
            f"probably a wrong line/selection, skipped",
            pinnacle_event,
            ev_percent,
            nvp,
        )
        return None

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

    # ------------------------------------------------------------
    # EXPERIMENT: reverse football moneyline bets.
    # The signal (EV / NVP gates above) was judged on the ORIGINAL side.
    # The bet itself goes on the opposite side. Price, NVP, Pinnacle price
    # and placement fields below all belong to the side actually bet.
    # The signal's EV is kept but flagged with an "r" prefix in the CSV.
    # ------------------------------------------------------------
    signal_ev_percent = ev_percent
    bet_outcome_label = outcome_label
    is_reversed = False

    if (
        sport in REVERSE_MONEYLINE_SPORTS
        and line_type == "money_line"
        and outcome_label in ("home", "away")
    ):
        flipped_label = "away" if outcome_label == "home" else "home"
        flipped_event = dict(pinnacle_event, outcome=flipped_label)

        rev_price, rev_outcome = find_betway_price_moneyline(
            betway_row, flipped_event, sport
        )
        if rev_price is None:
            stats.record_fail("reverse outcome not found on Betway")
            _log(
                False,
                f"reverse ({flipped_label}) outcome not found on Betway",
                pinnacle_event,
                signal_ev_percent,
                nvp,
            )
            return None

        cap = REVERSE_MAX_ODDS.get(sport)
        if cap is not None and rev_price >= cap:
            stats.record_fail("reverse odds at/above cap")
            _log(
                False,
                f"reverse ({flipped_label}) odds {rev_price} are not under {cap} — skipped",
                pinnacle_event,
                signal_ev_percent,
                nvp,
            )
            return None

        rev_nvp, _rev_fair = compute_nvp_and_fair_prob(pinnacle_event, flipped_label)

        betway_price = rev_price
        placement_fields = _moneyline_placement_fields(rev_outcome)
        bet_outcome_label = flipped_label
        is_reversed = True
        if rev_nvp is not None:
            nvp = rev_nvp

    ev_for_csv = f"r{signal_ev_percent:.2f}" if is_reversed else round(ev_percent, 2)

    result = {
        "pinnacle_event_id": pinnacle_event.get("eventId"),
        "betway_event_id": betway_row.get("eventId"),
        "home": pinnacle_event.get("home"),
        "away": pinnacle_event.get("away"),
        "league": (pinnacle_event.get("leagueName") or "")
        + {1: " [1st half]", 2: " [2nd half]"}.get(period, ""),
        "sport": sport,
        "line_type": line_type,
        "outcome": bet_outcome_label,
        "points": pinnacle_event.get("points"),
        "pinnacle_price": get_pinnacle_price(pinnacle_event, bet_outcome_label),
        "minutes_to_kickoff": round(minutes_to_kickoff(pinnacle_event) or 0, 1),
        "nvp": round(nvp, 3),
        "betway_price": betway_price,
        "betway_matched_line": matched_line,
        "line_matched_exactly": is_exact,
        "ev_percent": ev_for_csv,
        "reversed": is_reversed,
        "is_alt_stat_market": is_alt_stat_market(pinnacle_event),
        "betway_line": matched_line,
        **placement_fields,
    }

    stats.record_pass()
    if line_type == "money_line":
        line_note = (
            f" | REVERSED {outcome_label}->{bet_outcome_label} @ {betway_price}"
            if is_reversed
            else ""
        )
    else:
        line_note = f" | Betway line {matched_line} @ {betway_price}" + (
            "" if is_exact else " (not exact)"
        )
        if three_way:
            line_note += f" [3-way handicap, draw chance {push_prob * 100:.0f}%]"
    _log(
        True,
        "passed category thresholds" + line_note,
        pinnacle_event,
        signal_ev_percent,
        nvp,
    )
    return result
