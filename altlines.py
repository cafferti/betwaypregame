"""
Alternate bet lines.

For every Pinnacle alert (whether or not it passes the thresholds) the event id is sent to

    https://sword.pinnacleoddsdropper.com/events/<eventId>

which returns every line Pinnacle offers for that match (all periods, all spreads / totals /
moneyline) with a price history. Every line whose odds are DROPPING (not rising) becomes a
synthetic alert shaped exactly like a real one, so it goes through the same EV / NVP checks,
the same Betway line matching and the same bet placement.

"Dropping" is measured like the alert itself: the current price against the price one alert
window ago (timeIntervalMs of the triggering alert, 3.5 minutes by default), and it must have
fallen by at least ALT_MIN_DROP_PERCENT (None = the triggering alert's own minDropPercent).
"""

import threading
import time
from collections import deque

import requests

import netstatus

EVENTS_URL = "https://sword.pinnacleoddsdropper.com/events/{event_id}"

ALT_LINES_ENABLED = True
ALT_REFETCH_SEC = 20  # don't reload the same match more often than this
ALT_MIN_DROP_PERCENT = (
    None  # None = use the triggering alert's own minDropPercent (5 if missing)
)
ALT_MAX_PER_ALERT = (
    30  # at most this many alternate lines per alert (biggest drops first)
)
REQUEST_TIMEOUT_SEC = 12
SEEN_MAX = 20000

_SPORTS = {"1": "football", "3": "basketball"}

_lock = threading.Lock()
_last_fetch = {}
_seen = set()
_seen_order = deque()


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _remember(key) -> bool:
    """True the first time a (match, line, side, price) is seen, False for repeats."""
    with _lock:
        if key in _seen:
            return False
        if len(_seen_order) >= SEEN_MAX:
            _seen.discard(_seen_order.popleft())
        _seen.add(key)
        _seen_order.append(key)
        return True


def fetch_event(event_id):
    """The match's full odds (the 'data' object) or None. Pauses while the internet is down."""
    netstatus.wait_online()
    try:
        r = requests.get(
            EVENTS_URL.format(event_id=event_id),
            timeout=REQUEST_TIMEOUT_SEC,
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        j = r.json()
        return j.get("data", j) if isinstance(j, dict) else None
    except Exception as e:
        if netstatus.is_network_error(e):
            netstatus.waiting("alt")
        else:
            print(f"[alt] could not load event {event_id}: {type(e).__name__}")
        return None


def _drop(series, ref_ms, window_ms, current):
    """
    (drop %, reference price) when the line is genuinely DROPPING, else (None, None).
    Needs: a fall against the price one window ago, no bounce back up at the end, and the
    price still near its low of the window. Entries with price 0 are ignored (the odds before
    them are used), they do not mean the line is closed.
    """
    rows = []
    for row in series or []:
        try:
            rows.append((row[0], float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    pts = [
        (t, p) for t, p in rows if p > 0
    ]  # zeros are skipped: use the odds before them
    if not pts or not current or current <= 0:
        return None, None  # no usable history
    ref = None
    for t, p in pts:
        if t <= ref_ms - window_ms:
            ref = p  # last price at or before the start of the window
        else:
            break
    if ref is None:
        ref = pts[0][1]  # history shorter than the window: first price seen
    drop = (ref - current) / ref * 100.0
    in_window = [p for t, p in pts if t > ref_ms - window_ms] + [current]
    if len(pts) >= 2 and pts[-1][1] > pts[-2][1] * 1.005:
        return None, None  # the last move was UP
    if current > min(in_window) * 1.02:
        return None, None  # bounced more than 2% off the low
    return drop, ref


def build_alt_alerts(alert, allowed_fn=None):
    """
    Synthetic alerts for every dropping alternate line of the alert's match.
    allowed_fn(sport_name, line_type, period) -> bool says which periods may be bet.
    Returns [] when disabled, throttled, already started, or nothing is dropping.
    """
    if not ALT_LINES_ENABLED:
        return []
    eid = str(alert.get("eventId") or "")
    if not eid:
        return []

    now = time.time()
    with _lock:
        if now - _last_fetch.get(eid, 0.0) < ALT_REFETCH_SEC:
            return []
        _last_fetch[eid] = now
        if len(_last_fetch) > 3000:
            for k in sorted(_last_fetch, key=_last_fetch.get)[:1500]:
                _last_fetch.pop(k, None)

    data = fetch_event(eid)
    if not data:
        return []

    now_ms = int(now * 1000)
    starts = int(_f(data.get("starts")) or _f(alert.get("starts")) or 0)
    if starts and starts <= now_ms:
        return []  # already started: pregame only

    sport_id = str(data.get("sport_id") or alert.get("sportId") or "")
    sport = _SPORTS.get(sport_id)
    if sport is None:
        return []

    window_ms = int(_f(alert.get("timeIntervalMs")) or 210000)
    ref_ms = int(_f(alert.get("timestamp")) or now_ms)
    min_drop = ALT_MIN_DROP_PERCENT
    if min_drop is None:
        min_drop = _f(alert.get("minDropPercent")) or 5.0
    lo, hi = _f(alert.get("lowerBoundOdds")), _f(alert.get("upperBoundOdds"))

    orig = (
        int(_f(alert.get("periodNumber")) or 0),
        alert.get("lineType"),
        _f(alert.get("points")),
        (alert.get("outcome") or "").lower(),
    )

    base = {
        "type": "altLine",
        "altLine": True,
        "sportId": sport_id,
        "eventId": eid,
        "leagueName": data.get("league_name") or alert.get("leagueName"),
        "home": data.get("home") or alert.get("home"),
        "away": data.get("away") or alert.get("away"),
        "starts": str(starts) if starts else alert.get("starts"),
        "timestamp": str(ref_ms),
    }

    found = []

    def consider(number, line_type, points, side, price, series, prices):
        if not price or price <= 0:
            return
        if lo is not None and price < lo:
            return
        if hi is not None and price > hi:
            return
        if (number, line_type, points, side) == orig:
            return  # the alert's own line was already evaluated
        drop, ref = _drop(series, ref_ms, window_ms, price)
        if drop is None or drop < min_drop:
            return  # only DROPPING odds, never rising ones
        if not _remember((eid, number, line_type, points, side, round(price, 2))):
            return
        a = dict(base)
        a.update(
            {
                "id": f"alt-{eid}-{number}-{line_type}-{points}-{side}",
                "periodNumber": str(number),
                "lineType": line_type,
                "points": "" if points is None else str(points),
                "outcome": side,
                "changeTo": str(price),
                "changeFrom": str(round(ref, 3)),
                "percentageChange": str(round(drop, 2)),
            }
        )
        a.update(prices)
        found.append((drop, a))

    for per in (data.get("periods") or {}).values():
        number = int(_f(per.get("number")) or 0)
        if per.get("period_status") not in (None, 1):
            continue  # period closed
        hist = per.get("history") or {}

        if allowed_fn is None or allowed_fn(sport, "spread", number):
            for key, ln in (per.get("spreads") or {}).items():
                hdp = _f(ln.get("hdp") if ln.get("hdp") is not None else key)
                ph, pa = _f(ln.get("home")), _f(ln.get("away"))
                if hdp is None or not ph or not pa:
                    continue
                for side, price in (("home", ph), ("away", pa)):
                    series = ((hist.get("spreads") or {}).get(key) or {}).get(side)
                    consider(
                        number,
                        "spread",
                        hdp,
                        side,
                        price,
                        series,
                        {"priceHome": str(ph), "priceAway": str(pa)},
                    )

        if allowed_fn is None or allowed_fn(sport, "total", number):
            for key, ln in (per.get("totals") or {}).items():
                pts = _f(ln.get("points") if ln.get("points") is not None else key)
                po, pu = _f(ln.get("over")), _f(ln.get("under"))
                if pts is None or not po or not pu:
                    continue
                for side, price in (("over", po), ("under", pu)):
                    series = ((hist.get("totals") or {}).get(key) or {}).get(side)
                    consider(
                        number,
                        "total",
                        pts,
                        side,
                        price,
                        series,
                        {"priceOver": str(po), "priceUnder": str(pu)},
                    )

        ml = per.get("money_line")
        if ml and (allowed_fn is None or allowed_fn(sport, "money_line", number)):
            ph, pd, pa = _f(ml.get("home")), _f(ml.get("draw")), _f(ml.get("away"))
            if ph and pa:
                prices = {
                    "priceHome": str(ph),
                    "priceAway": str(pa),
                    "moneylineNumberOfWays": "3" if pd else "2",
                }
                if pd:
                    prices["priceDraw"] = str(pd)
                for side, price in (("home", ph), ("draw", pd), ("away", pa)):
                    if side == "draw" and not pd:
                        continue
                    series = (hist.get("moneyline") or {}).get(side)
                    consider(number, "money_line", None, side, price, series, prices)

    found.sort(key=lambda x: -x[0])
    return [a for _, a in found[:ALT_MAX_PER_ALERT]]
