"""
Fetches the CURRENT Betway price + version numbers for one specific selection,
so a bet that was rejected with "Price changed" can be re-sent with fresh data.

Works for every market type (moneyline, spread, total) because it looks the
selection up by its outcomeId, which never changes.
"""

from engineoddsretriever import (
    EVENT_MARKETS_URL,
    EVENT_MARKETS_PAGE_SIZE,
    EVENT_MARKETS_MAX_PAGES,
    _get_with_retry,
)


def refresh_live_price(event_id, market_id, outcome_id):
    """
    Returns (live_dict, "") with the up-to-date price fields, or (None, reason).
    Stops paging as soon as the selection is found, to keep it fast.
    """
    event_id_s, market_id_s, outcome_id_s = (
        str(event_id),
        str(market_id),
        str(outcome_id),
    )

    for page in range(EVENT_MARKETS_MAX_PAGES):
        params = {
            "eventId": event_id,
            "marketGroupId": " ",
            "countryCode": "NG",
            "cultureCode": "en-US",
            "skip": page * EVENT_MARKETS_PAGE_SIZE,
            "take": EVENT_MARKETS_PAGE_SIZE,
            "isBuildABetOnly": "false",
            "searchQuery": "",
        }
        data = _get_with_retry(EVENT_MARKETS_URL, params, pool_key="rebet", pool_size=4)
        if data is None:
            return None, "Betway live-price lookup failed (network)"

        markets = data.get("marketsInGroup") or []
        outcomes = data.get("outcomes") or []

        outcome = next(
            (o for o in outcomes if str(o.get("outcomeId")) == outcome_id_s), None
        )
        if outcome is not None:
            market = next(
                (m for m in markets if str(m.get("marketId")) == market_id_s), None
            ) or next(
                (
                    m
                    for m in markets
                    if str(m.get("marketId")) == str(outcome.get("marketId"))
                ),
                None,
            )
            if market is None:
                return None, "selection's market not returned by Betway"

            if (
                market.get("isSuspended")
                or market.get("isActive") is False
                or market.get("shouldDisplay") is False
                or outcome.get("shouldDisplay") is False
            ):
                return None, "market is suspended / closed on Betway"

            price = next(
                (
                    p
                    for p in (data.get("prices") or [])
                    if str(p.get("outcomeId")) == outcome_id_s
                ),
                None,
            )
            price_val = price.get("priceDecimal") if price else None
            if price_val in (None, 0):
                return None, "selection has no live price right now"

            event_version = None
            for e in data.get("events") or []:
                if str(e.get("eventId")) == event_id_s:
                    event_version = e.get("version")
                    break

            return (
                {
                    "priceDec": price_val,
                    "priceNum": price.get("numerator"),
                    "priceDen": price.get("denominator"),
                    "eventVersion": event_version
                    if event_version is not None
                    else market.get("version"),
                    "marketVersion": market.get("version"),
                    "outcomeVersion": outcome.get("version"),
                    "priceVersion": price.get("version"),
                    "serverEmopSource": price.get("emopSource"),
                    "publicHubPublishedTime": price.get("publicHubPublishedTime"),
                },
                "",
            )

        if len(markets) < EVENT_MARKETS_PAGE_SIZE:
            break  # last page and the selection wasn't on it

    return None, "selection not found on Betway any more"
