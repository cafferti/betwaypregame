import base64
import json
import os
import uuid

import requests
from playwright.sync_api import sync_playwright

import netstatus

BRAND_ID = "f8a8d16a-d619-4b49-aa8c-f21211403c92"
SETTLED_URL = (
    "https://www.betway.com.ng/appsynapse/universal/v1/Betting/BetslipsIncludeVuvu"
)


class TokenRejected(Exception):
    """Betway answered 401 — the saved login token is no longer valid."""


def get_current_token(page):
    return page.evaluate("() => localStorage.getItem('prod_auth._token')")


def user_id_from_token(token):
    """Reads the account ID out of the login token (no network call)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return None

    for key in (
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    ):
        if claims.get(key):
            return claims[key]
    return None


def place_bet(auth_token, event, stake_naira):
    """
    event dict must contain:
      eventId, marketId, outcomeId, priceNum, priceDen, priceDec, handicap,
      eventVersion, marketVersion, outcomeVersion, priceVersion,
      serverEmopSource, publicHubPublishedTime
    """
    url = "https://www.betway.com.ng/appsynapse/bet-api-sr02/v2/Betting/Strike"
    headers = {
        "authorization": f"Bearer {auth_token}",
        "content-type": "application/json",
        "x-brand-id": BRAND_ID,
        "origin": "https://www.betway.com.ng",
        "referer": "https://www.betway.com.ng/",
    }
    payload = {
        "countryCode": "NG",
        "betRequests": [
            {
                "requestId": str(uuid.uuid4()),
                "paymentType": 1,
                "betSelectionType": "Normal",
                "numberOfLines": 1,
                "acceptPriceChange": "None",
                "isEachWay": False,
                "channel": "web",
                "handicap": event["handicap"],
                "priceNum": event["priceNum"],
                "priceDen": event["priceDen"],
                "referringBookingCode": "",
                "wagerAmount": stake_naira,
                "bets": [
                    {
                        "priceType": "Normal",
                        "handicap": event["handicap"],
                        "priceDen": event["priceDen"],
                        "priceNum": event["priceNum"],
                        "priceDec": event["priceDec"],
                        "isEachWayActive": False,
                        "eventId": event["eventId"],
                        "marketId": event["marketId"],
                        "displayMarketId": event.get("displayMarketId")
                        or event["marketId"],
                        "outcomeId": [event["outcomeId"]],
                        "eventVersion": event["eventVersion"],
                        "marketVersion": event["marketVersion"],
                        "outcomeVersion": event["outcomeVersion"],
                        "priceVersion": event["priceVersion"],
                        "serverEmopSource": event["serverEmopSource"],
                        "publicHubPublishedTime": event["publicHubPublishedTime"],
                    }
                ],
            }
        ],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"raw": resp.text}


def fetch_settled_betslips(auth_token, user_id, page_number, page_size):
    """One page of your settled bets from Betway, newest first."""
    headers = {
        "authorization": f"Bearer {auth_token}",
        "accept": "application/json, text/plain, */*",
        "x-brand-id": BRAND_ID,
        "origin": "https://www.betway.com.ng",
        "referer": "https://www.betway.com.ng/",
    }
    params = {
        "state": "Settled",
        "filter": "All",
        "pageSize": page_size,
        "pageNumber": page_number,
        "searchTerm": "",
        "cultureCode": "en-US",
        "userId": user_id,
    }
    resp = requests.get(SETTLED_URL, headers=headers, params=params, timeout=20)
    if resp.status_code == 401:
        raise TokenRejected("Betway returned 401 for the settled-bets request")
    resp.raise_for_status()
    return resp.json().get("betslips") or []


class BetPlacer:
    def __init__(self, profile_path=None, max_stake=100, daily_cap=1000):
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.profile_path = profile_path or os.path.join(
            self.script_dir, "betway_profile"
        )
        self.max_stake = max_stake
        self.daily_cap = daily_cap
        self.spent_today = 0
        self._playwright = None
        self._context = None
        self._page = None
        self.token = None
        self.user_id = None

    def start(self):
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_path,
            headless=True,
            channel="chrome",
        )
        self._page = self._context.new_page()

        # No internet? Wait for it (bold "WAITING FOR NETWORK...") instead of failing.
        # Any other kind of error still gives up after 5 tries.
        other_failures = 0
        while True:
            try:
                self._page.goto("https://www.betway.com.ng/", timeout=45000)
                netstatus.ok("placer")
                break
            except Exception as e:
                if netstatus.is_network_error(e):
                    netstatus.waiting("placer")
                    netstatus.wait_online()  # pauses here until the connection is back
                    continue
                other_failures += 1
                first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
                print(
                    f"[betplacer] could not open betway.com.ng "
                    f"(attempt {other_failures}/5): {first_line} — retrying in {other_failures * 5}s"
                )
                if other_failures >= 5:
                    raise RuntimeError(
                        "Cannot open www.betway.com.ng after 5 tries — check your "
                        "VPN / firewall, then restart."
                    )
                self._page.wait_for_timeout(other_failures * 5000)

        self._page.wait_for_load_state("domcontentloaded")

        for attempt in range(15):
            self.token = get_current_token(self._page)
            if self.token:
                break
            self._page.wait_for_timeout(1000)

        if not self.token:
            raise RuntimeError(
                "No token found in localStorage after 15s — check that "
                "betway_profile/ exists next to this script and contains "
                "a valid saved login."
            )

        self.user_id = user_id_from_token(self.token)
        print("[betplacer] betplacer.py executed successfully — session token loaded")

    def stop(self):
        if self._context:
            self._context.close()
        if self._playwright:
            self._playwright.stop()

    def refresh_token(self):
        if self._page:
            self.token = get_current_token(self._page)
        return self.token

    def fetch_settled_page(self, page_number, page_size):
        """Used by the settlement step. Safe to call from another thread (no browser calls)."""
        token = self.token
        if not token:
            raise RuntimeError("no login token loaded yet")
        user_id = self.user_id or user_id_from_token(token)
        if not user_id:
            raise RuntimeError("could not read the account ID from the login token")
        return fetch_settled_betslips(token, user_id, page_number, page_size)

    def try_place(self, event, stake_naira):
        stake = min(stake_naira, self.max_stake)

        if self.spent_today + stake > self.daily_cap:
            return {"skipped": "daily_cap_reached", "spent_today": self.spent_today}

        if not self.token:
            return {
                "error": "no_token",
                "detail": "BetPlacer has no token — was start() called?",
            }

        required_fields = [
            "eventId",
            "marketId",
            "outcomeId",
            "priceNum",
            "priceDen",
            "priceDec",
            "handicap",
            "eventVersion",
            "marketVersion",
            "outcomeVersion",
            "priceVersion",
        ]
        missing = [f for f in required_fields if event.get(f) is None]
        if missing:
            return {"skipped": "missing_fields", "fields": missing}

        status_code, result = place_bet(self.token, event, stake)

        if status_code == 401 or result.get("isSuccessful") is False:
            fresh_token = self.refresh_token()
            if fresh_token and fresh_token != self.token:
                self.token = fresh_token
                status_code, result = place_bet(self.token, event, stake)

        if status_code == 401 or result.get("isSuccessful") is False:
            return {
                "error": "bet_failed",
                "status_code": status_code,
                "response": result,
                "note": "token likely expired — re-run launch_and_save_session.py to refresh it manually",
            }

        self.spent_today += stake
        return {"status_code": status_code, "response": result, "stake": stake}
