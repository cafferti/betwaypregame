from playwright.sync_api import sync_playwright
import requests
import uuid
import os


def get_current_token(page):
    return page.evaluate("() => localStorage.getItem('prod_auth._token')")


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
        "x-brand-id": "f8a8d16a-d619-4b49-aa8c-f21211403c92",
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
                        "displayMarketId": event["marketId"],
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

    def start(self):
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_path,
            headless=True,
            channel="chrome",
        )
        self._page = self._context.new_page()
        self._page.goto("https://www.betway.com.ng/")
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
