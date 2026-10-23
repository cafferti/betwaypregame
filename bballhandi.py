"""
Dump how Betway names and lays out the markets of ONE match, so they can be mapped.

  python inspect_markets.py basketball              list upcoming basketball matches (pick an eventId)
  python inspect_markets.py basketball 74786208     dump that match
  python inspect_markets.py football 73050830       same for football

  --words handicap,total,winner     which markets to show in detail (default shown)
Everything is also saved to  market_dump_<sport>_<eventId>.txt  -> send me that file.
"""

import sys
from datetime import datetime

from engineoddsretriever import (
    fetch_basketball_listing_page,
    fetch_page,
    _fetch_event_markets_all_pages,
)

DEFAULT_WORDS = "handicap,total,winner,spread,money,1x2,result"


def list_events(sport):
    data = fetch_page(0) if sport == "football" else fetch_basketball_listing_page(0)
    if not data:
        sys.exit("could not load the match list (network?)")
    events = [e for e in data.get("events", []) if e.get("regionId") != "esoccer"]
    events.sort(key=lambda e: e.get("expectedStartEpoch") or 0)
    print(
        f"\nUpcoming {sport} matches (showing {min(len(events), 40)} of {len(events)}):\n"
    )
    for e in events[:40]:
        start = e.get("expectedStartEpoch")
        when = datetime.fromtimestamp(start).strftime("%d %b %H:%M") if start else "?"
        print(
            f"  {e.get('eventId')}   {when}   {e.get('homeTeam')} vs {e.get('awayTeam')}   [{e.get('league')}]"
        )
    print(
        f"\nPick one (bigger leagues have more markets, e.g. quarters/halves) and run:\n"
        f"  python inspect_markets.py {sport} <eventId>"
    )


def dump(sport, event_id, words):
    data = _fetch_event_markets_all_pages(event_id, "inspect", 4)
    if not data:
        sys.exit("lookup failed (network?)")

    lines = []

    def out(text=""):
        print(text)
        lines.append(text)

    prices = {p["outcomeId"]: p for p in data.get("prices", [])}
    outcomes = data.get("outcomes", [])
    markets = data.get("marketsInGroup", [])
    ev = next(
        (e for e in data.get("events", []) if str(e.get("eventId")) == str(event_id)),
        {},
    )

    out(
        f"EVENT {event_id}: {ev.get('homeTeam')} vs {ev.get('awayTeam')}  (sport {sport})"
    )
    out(f"event fields: {sorted(ev.keys())}")
    out(f"\n=== ALL {len(markets)} MARKET NAMES (name | displayName | id) ===")
    for m in markets:
        out(f"  {m.get('name')} | {m.get('displayName')} | {m.get('marketId')}")

    out("\n=== DETAIL (selections listed under the market they BELONG to) ===")
    for m in markets:
        label = f"{m.get('name')} | {m.get('displayName')}"
        if not any(w in label.lower() for w in words):
            continue
        mid = str(m["marketId"])
        out(
            f"\nMARKET id={mid}  {label}  handicap={m.get('handicap')}  "
            f"parent={m.get('isSquashedParent')} suspended={m.get('isSuspended')} active={m.get('isActive')}"
        )
        own = [o for o in outcomes if str(o.get("marketId", "")) == mid]
        for o in own:
            p = prices.get(o.get("outcomeId"), {})
            out(
                f"   {o.get('name')!r:34} handicap={o.get('handicap')!r:7} sbv={o.get('sbv')!r:12} "
                f"price={p.get('priceDecimal')}"
            )
        extra = sum(
            1
            for o in outcomes
            if str(o.get("outcomeId", "")).startswith(mid)
            and str(o.get("marketId", "")) != mid
        )
        if extra:
            out(
                f"   (+{extra} more selections whose id starts with this market's id but belong to other markets)"
            )
    name = f"market_dump_{sport}_{event_id}.txt"
    with open(name, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nSaved to {name} — send me that file.")


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    words = DEFAULT_WORDS
    for a in argv[1:]:
        if a.startswith("--words="):
            words = a.split("=", 1)[1]
    words = [w.strip().lower() for w in words.split(",") if w.strip()]
    if not args or args[0] not in ("football", "basketball"):
        sys.exit(__doc__)
    sport = args[0]
    if len(args) == 1:
        list_events(sport)
    else:
        dump(sport, int(args[1]), words)


if __name__ == "__main__":
    main(sys.argv)
