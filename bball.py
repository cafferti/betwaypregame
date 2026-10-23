import sys
from engineoddsretriever import fetch_page, _fetch_event_markets_all_pages

if len(sys.argv) > 1:
    event_id = int(sys.argv[1])
else:
    data = fetch_page(0)
    events = [e for e in data["events"] if e.get("regionId") != "esoccer"]
    ev = events[0]
    event_id = ev["eventId"]
    print(f"Using: {ev.get('homeTeam')} vs {ev.get('awayTeam')} (eventId {event_id})")

data = _fetch_event_markets_all_pages(event_id, "inspect", 4)
if not data:
    sys.exit("lookup failed")

prices = {p["outcomeId"]: p for p in data.get("prices", [])}
for m in data["marketsInGroup"]:
    label = f"{m.get('name')} | {m.get('displayName')}"
    if "handicap" not in label.lower():
        continue
    print(f"\nMARKET id={m['marketId']}  {label}  handicap={m.get('handicap')}")
    for o in data.get("outcomes", []):
        if str(o.get("outcomeId", "")).startswith(str(m["marketId"])):
            p = prices.get(o["outcomeId"], {})
            print(
                f"   {o.get('name')!r:32} outcome.handicap={o.get('handicap')!r:7} "
                f"sbv={o.get('sbv')!r:7} price={p.get('priceDecimal')}"
            )
