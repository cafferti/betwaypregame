import requests

BASE_URL = "https://feeds-roa2.betwayafrica.com/br/_apis/sport/v1/BetBook/Upcoming/"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.betway.com.ng/",
    "Origin": "https://www.betway.com.ng",
}

url_params = [
    ("countryCode", "NG"),
    ("sportId", "soccer"),
    ("Skip", 0),
    ("Take", 20),
    ("cultureCode", "en-US"),
    ("isEsport", "false"),
    ("boostedOnly", "false"),
    ("marketTypes", "[Win/Draw/Win]"),
    ("marketTypes", "[Handicap] [2-Way]"),
]

resp = requests.get(BASE_URL, params=url_params, headers=HEADERS, timeout=15)
resp.raise_for_status()
data = resp.json()

# Find a handicap market from this response, then find its outcomes and prices
handicap_markets = [
    m for m in data.get("markets", []) if m.get("marketTypeCName") == "handicap-wdw"
]
print(f"Found {len(handicap_markets)} handicap market(s)")

if handicap_markets:
    target = handicap_markets[0]
    target_market_id = target["marketId"]
    print(f"\nTarget market: {target}")

    matching_outcomes = [
        o for o in data.get("outcomes", []) if o.get("marketId") == target_market_id
    ]
    print(f"\nOutcomes with marketId == '{target_market_id}': {len(matching_outcomes)}")
    for o in matching_outcomes[:6]:
        print(" ", o)

    # Also check if any outcome references it via originalMarketId instead
    alt_outcomes = [
        o
        for o in data.get("outcomes", [])
        if str(target_market_id) in str(o.get("originalMarketId", ""))
    ]
    print(
        f"\nOutcomes with originalMarketId containing '{target_market_id}': {len(alt_outcomes)}"
    )
    for o in alt_outcomes[:6]:
        print(" ", o)
