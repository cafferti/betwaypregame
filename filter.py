from difflib import SequenceMatcher
from engineoddsretriever import resolve_live_bet_target, resolve_live_moneyline, MAX_LINE_GAP
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

# Reverse only when the OTHER side is the favourite on BETWAY (its Betway price is lower than
# the signalled side's Betway price); otherwise the ORIGINAL side is bet.
REVERSE_ONLY_WHEN_FAVOURITE = True

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

# Football spread rules, judged on the line actually bet and on Betway's 1X2 prices:
#  * a NEGATIVE line (the team starts a goal or more behind) only on the favourite;
#  * a POSITIVE line on the favourite: no restriction;
#  * a POSITIVE line on the underdog: when the two teams' win odds are MORE than
#    FOOTBALL_UNDERDOG_ODDS_GAP apart (a lopsided match) it must be +1 or more; in a close
#    match (gap within that) any positive line, e.g. +0.5, is accepted.
FOOTBALL_SPREAD_RULES_ENABLED = True
FOOTBALL_UNDERDOG_ODDS_GAP = 0.5
FOOTBALL_UNDERDOG_MIN_LINE_IF_LOPSIDED = 1.0

# Pinnacle also posts corners / bookings markets. They are not goals markets, so they must
# never be matched to a Betway goals market (that is how wrong bets were placed).
BLOCK_ALT_STAT_MARKETS = True

# Only full-game alerts are bet (period 0) unless the Betway market names are known.
# Football: 1 = first half, 2 = second half.
# Basketball: 1 = first half, 2 = second half, 3/4/5/6 = quarters 1/2/3/4.
ALLOWED_PERIODS = {0}
ALLOWED_PERIODS_BY_MARKET = {
    ("football", "spread"): {0, 1, 2},
    ("football", "money_line"): {0, 1, 2},
    ("basketball", "money_line"): {0, 1, 2, 3, 4, 5, 6},
    ("basketball", "spread"): {0, 1, 2, 3, 4, 5, 6},
    ("basketball", "total"): {0, 1, 2, 3, 4, 5, 6},
}

_ORD = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def betway_market_names(sport, line_type, period):
    """
    The exact Betway market names (lower case) that match a Pinnacle spread/total for
    this period, from the market dumps. None = use the generic search (football spreads).
    """
    if line_type not in ("spread", "total"):
        return None
    if sport == "basketball":
        if period == 0:
            return {"handicap (incl. overtime)"} if line_type == "spread" else {"total (incl. overtime)"}
        # alert period numbers: 1 / 2 = halves, 3 / 4 / 5 / 6 = quarters 1 / 2 / 3 / 4
        part = {
            1: "1st half",
            2: "2nd half",
            3: "1st quarter",
            4: "2nd quarter",
            5: "3rd quarter",
            6: "4th quarter",
        }.get(period)
        if part:
            return {f"{part} - handicap"} if line_type == "spread" else {f"{part} - total"}
    if sport == "football" and line_type == "total" and period == 0:
        return {"total", "[total goals]"}
    return None


def betway_moneyline_names(sport, period):
    """
    Betway market names for a PERIOD moneyline (full game uses the cached listing).
    Football halves: the 3-way 1X2 of that half. Basketball halves/quarters: Draw No Bet,
    the 2-way market that, like Pinnacle's, refunds a tie.
    """
    if sport == "football":
        return {1: {"[1st half] - [win/draw/win]"}, 2: {"[2nd half] - [win/draw/win]"}}.get(period)
    if sport == "basketball":
        part = {1: "1st half", 2: "2nd half", 3: "1st quarter", 4: "2nd quarter", 5: "3rd quarter", 6: "4th quarter"}.get(period)
        return {f"{part} - draw no bet"} if part else None
    return None


_PERIOD_LABEL = {
    "football": {1: " [1st half]", 2: " [2nd half]"},
    "basketball": {
        1: " [1st half]",
        2: " [2nd half]",
        3: " [Q1]",
        4: " [Q2]",
        5: " [Q3]",
        6: " [Q4]",
    },
}

# Safety net for football and basketball spreads / totals: an EV above this is almost certainly a wrong
# line or the wrong selection (it happened: 131%), so the bet is skipped. None = off.
MAX_LINE_EV_PERCENT = 60.0

# Per-category (sport, line_type, side) -> (min_ev_percent, max_nvp).
# side is "over"/"under" for totals, "+"/"-" for spreads, None for moneyline.
# A missing or None entry means that category is EXCLUDED — no bets placed.
CATEGORY_THRESHOLDS = {
    ("basketball", "total", "over"): (4.0, 2.5),
    ("basketball", "total", "under"): (4.0, 2.5),  # unders now enabled (same criteria as overs)
    ("basketball", "spread", "+"): (5.0, 2.5),
    ("basketball", "spread", "-"): (6.0, 2.5),
    ("basketball", "money_line", None): (7.0, 2.5),
    ("football", "money_line", None): (6.0, 2.7),
    ("football", "total", "over"): (4.0, 2.5),
    ("football", "total", "under"): (4.0, 2.5),  # unders now enabled (same criteria as overs)
    ("football", "spread", "+"): (5.0, 2.5),
    ("football", "spread", "-"): (5.0, 2.5),  # now enabled (same criteria as "+" spreads)
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


def _live_placement_fields(live: dict) -> dict:
    return {
        "betway_market_id": live.get("marketId"),
        "betway_display_market_id": live.get("displayMarketId"),
        "id_variants": live.get("idVariants"),
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


def _moneyline_lookup(betway_row: dict, pinnacle_event: dict, sport: str, period: int):
    """(price, placement fields) for a moneyline side: cached listing for the full game,
    live Betway lookup of the half / quarter market otherwise."""
    if period == 0:
        price, o = find_betway_price_moneyline(betway_row, pinnacle_event, sport)
        return (None, None) if price is None else (price, _moneyline_placement_fields(o))
    names = betway_moneyline_names(sport, period)
    if not names:
        return None, None
    live, _why = resolve_live_moneyline(
        betway_row.get("eventId"),
        (pinnacle_event.get("outcome") or "").strip().lower(),
        pinnacle_event.get("home"),
        pinnacle_event.get("away"),
        names,
    )
    return (None, None) if live is None else (live["priceDec"], _live_placement_fields(live))


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


def period_allowed(sport, line_type, period) -> bool:
    return period in ALLOWED_PERIODS_BY_MARKET.get((sport, line_type), ALLOWED_PERIODS)


def _own_line(pinnacle_event: dict, outcome_label: str) -> float:
    """
    Pinnacle's `points` for a spread is the HOME team's handicap, for both sides (an alert for
    the away side at points=3 is the away team at -3). Returns the handicap of the team
    actually being bet.
    """
    try:
        p = float(pinnacle_event.get("points") or 0)
    except (TypeError, ValueError):
        p = 0.0
    return -p if outcome_label == "away" else p


def _reverse_is_favourite(betway_row, pinnacle_event, sport, period, outcome_label, original_price) -> bool:
    """True when the opposite side has the LOWER Betway price than the signalled side."""
    flipped = "away" if outcome_label == "home" else "home"
    rev_price, _fields = _moneyline_lookup(
        betway_row, dict(pinnacle_event, outcome=flipped), sport, period
    )
    if rev_price is None or original_price is None:
        return False  # cannot tell: keep the original side
    return rev_price < original_price


def _football_spread_blocked(betway_row, pinnacle_event, outcome_label, line):
    """None when the bet is allowed, else the reason it is skipped."""
    if line is None:
        return None
    other_label = "away" if outcome_label == "home" else "home"
    own_price, _ = find_betway_price_moneyline(
        betway_row, dict(pinnacle_event, outcome=outcome_label), "football"
    )
    other_price, _ = find_betway_price_moneyline(
        betway_row, dict(pinnacle_event, outcome=other_label), "football"
    )
    if own_price is None or other_price is None:
        return "football spread rule: no Betway 1X2 price to tell the favourite — skipped"
    favourite = own_price < other_price
    gap = abs(own_price - other_price)
    seen = f"(Betway win odds {own_price} vs {other_price})"
    if line < 0:
        if not favourite:
            return f"football spread {line:+g} on the underdog {seen} — negative lines only on the favourite"
        return None
    if favourite:
        return None
    if gap > FOOTBALL_UNDERDOG_ODDS_GAP and line < FOOTBALL_UNDERDOG_MIN_LINE_IF_LOPSIDED:
        return (
            f"football spread {line:+g} on the underdog {seen} — odds gap {gap:.2f} is over "
            f"{FOOTBALL_UNDERDOG_ODDS_GAP:g}, so it needs +{FOOTBALL_UNDERDOG_MIN_LINE_IF_LOPSIDED:g} or more"
        )
    return None


def _describe(pinnacle_event: dict) -> str:
    text = (
        f"{pinnacle_event.get('home')} vs {pinnacle_event.get('away')} "
        f"[{pinnacle_event.get('leagueName')}] {pinnacle_event.get('lineType')} "
        f"{pinnacle_event.get('outcome')} {pinnacle_event.get('points')}"
    )
    if pinnacle_event.get("lineType") == "spread":
        side = (pinnacle_event.get("outcome") or "").lower()
        text += f" (bet team's own line {_own_line(pinnacle_event, side):+g})"
    return text


def _fmt(val, suffix=""):
    return "N/A" if val is None else f"{val:.2f}{suffix}"


def _log(passed: bool, reason: str, pinnacle_event: dict, ev_percent, nvp):
    if not passed and pinnacle_event.get("altLine"):
        return  # alternate lines: the console only shows the ones that pass
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
        _log(False, "corners/bookings market — not a goals market, skipped", pinnacle_event, None, None)
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
        _log(False, f"period {period} market (not full game) — skipped", pinnacle_event, None, None)
        return None

    outcome_label = (pinnacle_event.get("outcome") or "").lower()
    nvp, fair_prob = compute_nvp_and_fair_prob(pinnacle_event, outcome_label)
    if nvp is None:
        stats.record_fail("no NVP")
        _log(False, "could not compute NVP", pinnacle_event, None, None)
        return None

    line_type = pinnacle_event.get("lineType")

    thresholds = _get_category_thresholds(
        sport,
        line_type,
        outcome_label,
        _own_line(pinnacle_event, outcome_label)
        if line_type == "spread"
        else pinnacle_event.get("points"),
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
        _log(False, f"{category} is switched OFF in the dashboard", pinnacle_event, None, nvp)
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
        betway_price, placement_fields = _moneyline_lookup(
            betway_row, pinnacle_event, sport, period
        )
        if betway_price is None:
            stats.record_fail("market/outcome not found on Betway")
            _log(False, "market/outcome not found on Betway", pinnacle_event, None, nvp)
            return None

        matched_line = None
        is_exact = True

    elif line_type in ("spread", "total"):
        try:
            target_line = float(pinnacle_event.get("points") or 0)
        except (ValueError, TypeError):
            target_line = 0.0
        if line_type == "spread" and outcome_label == "away":
            target_line = -target_line  # points = home handicap; the away team's own line is the opposite

        team_name = (
            pinnacle_event["home"]
            if outcome_label == "home"
            else pinnacle_event["away"]
        )

        # Basketball totals get a wider acceptable line gap; everything else
        # keeps the engine default.
        nearest = False
        window = None
        line_gap = MAX_LINE_GAP  # default; the branches below widen it where needed
        if sport == "basketball" and line_type == "spread":
            window = BASKETBALL_SPREAD_WINDOW
            nearest = True  # closest line inside the window (ties go to the easier line)
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
            pinnacle_event["away"] if outcome_label == "home" else pinnacle_event["home"]
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
            market_names=betway_market_names(sport, line_type, period),
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
        placement_fields = _live_placement_fields(live)

        if sport == "football" and line_type == "spread" and FOOTBALL_SPREAD_RULES_ENABLED:
            blocked = _football_spread_blocked(
                betway_row, pinnacle_event, outcome_label, matched_line
            )
            if blocked:
                stats.record_fail("football spread rule")
                _log(False, blocked, pinnacle_event, None, nvp)
                return None

    else:
        stats.record_fail("unsupported line type")
        _log(False, "unsupported line type", pinnacle_event, None, nvp)
        return None

    ev_percent = (betway_price * fair_prob * ev_factor - 1.0) * 100.0

    if (
        MAX_LINE_EV_PERCENT is not None
        and sport in ("football", "basketball")
        and line_type in ("spread", "total")
        and ev_percent > MAX_LINE_EV_PERCENT
    ):
        stats.record_fail("EV implausibly high (line mismatch?)")
        _log(
            False,
            f"EV {ev_percent:.1f}% is above {MAX_LINE_EV_PERCENT:g}% — "
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

    reverse_declined = False
    if (
        sport in REVERSE_MONEYLINE_SPORTS
        and line_type == "money_line"
        and outcome_label in ("home", "away")
    ):
        if REVERSE_ONLY_WHEN_FAVOURITE and not _reverse_is_favourite(
            betway_row, pinnacle_event, sport, period, outcome_label, betway_price
        ):
            reverse_declined = True  # the other side is not the favourite: bet the original side

    if (
        sport in REVERSE_MONEYLINE_SPORTS
        and line_type == "money_line"
        and outcome_label in ("home", "away")
        and not reverse_declined
    ):
        flipped_label = "away" if outcome_label == "home" else "home"
        flipped_event = dict(pinnacle_event, outcome=flipped_label)

        rev_price, rev_fields = _moneyline_lookup(
            betway_row, flipped_event, sport, period
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
        placement_fields = rev_fields
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
        + _PERIOD_LABEL.get(sport, {}).get(period, "")
        + (" [alt]" if pinnacle_event.get("altLine") else ""),
        "sport": sport,
        "line_type": line_type,
        "outcome": bet_outcome_label,
        "points": (
            _own_line(pinnacle_event, outcome_label)
            if line_type == "spread"
            else pinnacle_event.get("points")
        ),
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
            else (
                f" | ORIGINAL side kept @ {betway_price} (the other side is not the favourite on Betway)"
                if reverse_declined
                else ""
            )
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