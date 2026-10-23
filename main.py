import asyncio
import concurrent.futures
import functools
import json
import os
import sys
import threading
import time

from engineoddsretriever import OddsCache, BasketballOddsCache
from pinnacleretriever import OddsStreamer
from filter import evaluate_pinnacle_event, stats
from betplacer import BetPlacer, TokenRejected
from betlog import BET_LOG_CSV, ensure_csv_schema, append_bet, settle_pending
from liveprice import refresh_live_price
import netstatus
import controls

STAKE_NAIRA = 200
MAX_STAKE_NAIRA = 500
DAILY_CAP_NAIRA = float(
    "inf"
)  # no daily cap (the per-bet MAX_STAKE_NAIRA still applies)
SUMMARY_EVERY_N_EVENTS = 50

# ---- Rebet when Betway rejects with "100001: Price changed" -------------
# The bot re-reads the LIVE price + version numbers for the same selection and
# sends the bet again (works for moneyline, spread and total, reversed or not).
REBET_ENABLED = True
REBET_MAX_ATTEMPTS = 5  # re-checks + re-sends per signal before giving up
REBET_DELAY_SEC = 0.2  # tiny pause between attempts
# Which live prices are rebet, judged on the odds the bot ORIGINALLY tried to take.
# Each tier: (original odds limit, limit included?, rule, number). First matching tier
# wins; original odds above every tier: rebet at any live price.
#   "max_drop"  -> live price may be at most <number> below the original
#   "floor"     -> live price must not fall below <number>
#   "drop_lt"   -> live price must be LESS than <number> below the original
REBET_TIERS = [
    (2.0, True, "max_drop", 0.25),  # original 2.00 or below: at most 0.25 lower
    (
        4.0,
        False,
        "floor",
        2.0,
    ),  # original 2.01 - 3.99: live price must stay at 2.00 or above
    (6.0, False, "drop_lt", 2.0),  # original 4.00 - 5.99: less than 2.00 lower
]  # original 6.00 and above: rebet at any live price
PRICE_CHANGED_CODE = 100001

# How often the bot checks Betway for results of your Pending bets.
# (If nothing is Pending it skips the check without contacting Betway.)
SETTLE_INTERVAL_SECONDS = 30
SETTLE_PAGE_SIZE = 50
SETTLE_FIRST_RUN_MAX_PAGES = 40  # startup: look far back (covers time the bot was off)
SETTLE_LATER_MAX_PAGES = 3  # afterwards: newest settled bets only (3 x 50)

BET_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "placed_bets.json"
)
_history_lock = threading.Lock()

bet_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


# ============================================================
# SOUND ALERTS
# ============================================================


def _play_sound(kind: str):
    if sys.platform != "win32":
        return
    try:
        import winsound

        if kind == "passed":
            winsound.Beep(900, 150)
        elif kind == "placed":
            winsound.Beep(500, 300)
            winsound.Beep(700, 200)
    except Exception:
        pass


# ============================================================
# PERSISTENT BET HISTORY (survives restarts)
# ============================================================


def load_bet_history() -> set:
    with _history_lock:
        if not os.path.exists(BET_HISTORY_FILE):
            return set()
        try:
            with open(BET_HISTORY_FILE, "r") as f:
                data = json.load(f)
            return set(data.get("outcome_ids", []))
        except (json.JSONDecodeError, OSError) as e:
            print(
                f"[history] WARNING: could not read {BET_HISTORY_FILE} — {e} — starting fresh"
            )
            return set()


def save_bet_record(outcome_id, desc: str, bet_result: dict):
    with _history_lock:
        try:
            if os.path.exists(BET_HISTORY_FILE):
                with open(BET_HISTORY_FILE, "r") as f:
                    data = json.load(f)
            else:
                data = {"outcome_ids": [], "records": []}
        except (json.JSONDecodeError, OSError):
            data = {"outcome_ids": [], "records": []}

        if outcome_id not in data["outcome_ids"]:
            data["outcome_ids"].append(outcome_id)

        data["records"].append(
            {
                "outcomeId": outcome_id,
                "description": desc,
                "stake": bet_result.get("stake"),
                "status_code": bet_result.get("status_code"),
                "response": bet_result.get("response"),
            }
        )

        try:
            with open(BET_HISTORY_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            print(f"[history] WARNING: could not write {BET_HISTORY_FILE} — {e}")


# ============================================================
# HELPERS
# ============================================================


def _describe_result(result: dict) -> str:
    return f"{result['home']} vs {result['away']} [{result['league']}] {result['outcome']} {result['points']}"


def _print_bet_outcome(result: dict, bet_result: dict) -> bool:
    desc = _describe_result(result)

    if bet_result.get("skipped"):
        print(f"⏭️  BET SKIPPED [{bet_result['skipped']}] — {desc}")
        return False

    response = bet_result.get("response") or {}
    bet_responses = response.get("betResponses") or []

    if bet_result.get("status_code") == 200 and bet_responses:
        inner = bet_responses[0]
        if (
            inner.get("isSuccessful")
            and inner.get("placementStatus") == "Success"
            and inner.get("errorCode") == 0
        ):
            stake = bet_result.get("stake")
            print(
                f"✅ BET PLACED — {desc} | stake ₦{stake} | odds {result['betway_price']}"
            )
            _play_sound("placed")
            return True
        else:
            meta = inner.get("errorMetaData") or {}
            msg = meta.get("message") or inner.get("placementStatus")
            icon = "⏸️ " if inner.get("errorCode") == 100010 else "❌"
            print(f"{icon} REJECTED — {desc} | {inner.get('errorCode')}: {msg}")
            return False
    else:
        note = str(bet_result.get("note") or response)
        print(f"❌ BET FAILED — {desc} | {note[:160]}")
        return False


def _build_bet_event(result: dict):
    return {
        "eventId": result["betway_event_id"],
        "marketId": result["betway_market_id"],
        "outcomeId": result["outcomeId"],
        "priceNum": result["priceNum"],
        "priceDen": result["priceDen"],
        "priceDec": result["betway_price"],
        "handicap": result.get("betway_handicap_scaled") or 0,
        "eventVersion": result["eventVersion"],
        "marketVersion": result["marketVersion"],
        "outcomeVersion": result["outcomeVersion"],
        "priceVersion": result["priceVersion"],
        "serverEmopSource": result["serverEmopSource"],
        "publicHubPublishedTime": result["publicHubPublishedTime"],
    }


# ============================================================
# PLACE A BET, AND REBET WITH THE LIVE PRICE IF BETWAY SAYS "PRICE CHANGED"
# ============================================================


def _is_price_changed(bet_result: dict) -> bool:
    response = bet_result.get("response") or {}
    bet_responses = response.get("betResponses") or []
    return (
        bool(bet_responses) and bet_responses[0].get("errorCode") == PRICE_CHANGED_CODE
    )


def _rebet_price_ok(original: float, new: float):
    """Applies REBET_TIERS. Returns (ok, short reason for the log)."""
    drop = round(original - new, 4)
    for limit, inclusive, rule, num in REBET_TIERS:
        if (original <= limit + 1e-9) if inclusive else (original < limit - 1e-9):
            if rule == "max_drop":
                return drop <= num + 1e-9, f"{drop} below original; max allowed {num}"
            if rule == "floor":
                return new >= num - 1e-9, f"below the {num} floor"
            if rule == "drop_lt":
                return drop < num - 1e-9, f"{drop} below original; must be under {num}"
    return True, ""


def place_with_rebet(placer: BetPlacer, result: dict, bet_event: dict, stake):
    """
    Runs on the single bet thread. Sends the bet; if Betway rejects it with
    100001 (price changed) it fetches the live price + versions for the SAME
    selection and sends again, up to REBET_MAX_ATTEMPTS times.

    The selection never changes, so a reversed bet stays reversed.
    A rebet only goes out if the live price passes the odds tiers in REBET_TIERS. On success, `result` is updated with the price
    actually taken, so the CSV logs the real odds.
    """
    bet_result = placer.try_place(bet_event, stake)
    if not REBET_ENABLED:
        return bet_result

    desc = _describe_result(result)
    original_price = float(result["betway_price"])
    base_ev = result.get(
        "ev_percent"
    )  # numeric for normal bets, "r8.34" text for reversed
    last_price = original_price
    attempts_done = 0

    while attempts_done < REBET_MAX_ATTEMPTS and _is_price_changed(bet_result):
        attempts_done += 1
        time.sleep(REBET_DELAY_SEC)
        netstatus.wait_online()  # paused while the internet is down; the rebet then re-checks the live price

        live, reason = refresh_live_price(
            result["betway_event_id"], result["betway_market_id"], result["outcomeId"]
        )
        if live is None:
            print(f"⏭️  REBET STOPPED [{reason}] — {desc}")
            return bet_result

        new_price = float(live["priceDec"])
        price_ok, why = _rebet_price_ok(original_price, new_price)
        if not price_ok:
            print(
                f"⏭️  REBET SKIPPED [live {new_price} vs original {original_price}: "
                f"{why}] — {desc}"
            )
            return bet_result

        print(
            f"🔁 REBET {attempts_done}/{REBET_MAX_ATTEMPTS} — {desc} | "
            f"price {last_price} -> {new_price}"
        )
        last_price = new_price

        bet_event = dict(bet_event)
        for key in (
            "priceNum",
            "priceDen",
            "eventVersion",
            "marketVersion",
            "outcomeVersion",
            "priceVersion",
            "serverEmopSource",
            "publicHubPublishedTime",
        ):
            bet_event[key] = live[key]
        bet_event["priceDec"] = new_price

        # keep the logged odds / EV honest (EV scales with price: EV+1 = price x fair prob)
        result["betway_price"] = new_price
        if isinstance(base_ev, (int, float)) and original_price:
            result["ev_percent"] = round(
                ((1.0 + base_ev / 100.0) * new_price / original_price - 1.0) * 100.0, 2
            )

        bet_result = placer.try_place(bet_event, stake)

    if attempts_done and _is_price_changed(bet_result):
        print(f"⏸️  REBET GAVE UP after {attempts_done} attempts — {desc}")
    return bet_result


# ============================================================
# SETTLEMENT (Pending -> Win / Loss / Void / Cashed Out)
# ============================================================


async def run_settlement(placer: BetPlacer, max_pages: int, label: str):
    """
    One settlement pass. If Betway rejects the token (401), grab a fresh
    one from the browser session (on the thread that owns Playwright)
    and retry once.
    """
    loop = asyncio.get_running_loop()
    await netstatus.wait_online_async()  # paused while the internet is down

    for attempt in (1, 2):
        try:
            await loop.run_in_executor(
                None,
                functools.partial(
                    settle_pending,
                    placer.fetch_settled_page,
                    SETTLE_PAGE_SIZE,
                    max_pages,
                ),
            )
            return
        except TokenRejected:
            if attempt == 2:
                print(
                    f"[settle] {label}: still 401 after refreshing — re-run "
                    f"launch_and_save_session.py, then restart the bot."
                )
                return
            old = placer.token
            new = await loop.run_in_executor(bet_executor, placer.refresh_token)
            if new and new != old:
                print(f"[settle] {label}: token refreshed from browser — retrying")
            else:
                print(
                    f"[settle] {label}: 401 and no fresh token available — retrying once anyway"
                )
        except Exception as e:
            if netstatus.is_network_error(e):
                netstatus.waiting("betway")
            else:
                print(f"[settle] ERROR: {label} settlement check failed — {e}")
            return


async def settlement_loop(placer: BetPlacer):
    """Periodic pass. The startup pass already ran in main(), so sleep first."""
    while True:
        await asyncio.sleep(SETTLE_INTERVAL_SECONDS)
        await run_settlement(placer, SETTLE_LATER_MAX_PAGES, "periodic")


# ============================================================
# MAIN LOOP
# ============================================================


async def consume_and_evaluate(
    streamer: OddsStreamer,
    football_cache: OddsCache,
    basketball_cache: BasketballOddsCache,
    placer: BetPlacer,
    already_bet: set,
):
    loop = asyncio.get_running_loop()

    while True:
        event = await streamer.out_queue.get()
        await (
            netstatus.wait_online_async()
        )  # internet out: hold this alert until it is back (nothing is skipped)
        betway_rows = football_cache.get_rows() + basketball_cache.get_rows()

        try:
            # evaluate_pinnacle_event may block on a live network call
            # for spread/total candidates — run off the event loop.
            result = await loop.run_in_executor(
                None, evaluate_pinnacle_event, event, betway_rows
            )
        except Exception as e:
            print(f"[orchestrator] ERROR: evaluate_pinnacle_event failed — {e}")
            continue

        if stats.total_fetched % SUMMARY_EVERY_N_EVENTS == 0:
            print(stats.summary_line())

        if result is None:
            continue
        _play_sound("passed")

        required = [
            "betway_event_id",
            "betway_market_id",
            "outcomeId",
            "priceNum",
            "priceDen",
            "marketVersion",
            "outcomeVersion",
            "priceVersion",
        ]
        if any(result.get(k) is None for k in required):
            print(
                f"⏭️  BET SKIPPED [missing_required_fields] — {_describe_result(result)}"
            )
            continue

        outcome_key = f"{result['line_type']}:{result['outcomeId']}"
        if outcome_key in already_bet:
            print(
                f"⏭️  BET SKIPPED [already_bet_this_outcome] — {_describe_result(result)}"
            )
            continue

        bet_event = _build_bet_event(result)

        try:
            bet_result = await loop.run_in_executor(
                bet_executor, place_with_rebet, placer, result, bet_event, STAKE_NAIRA
            )
            was_placed = _print_bet_outcome(result, bet_result)
            if was_placed:
                already_bet.add(outcome_key)
                await loop.run_in_executor(
                    bet_executor,
                    save_bet_record,
                    outcome_key,
                    _describe_result(result),
                    bet_result,
                )
                await loop.run_in_executor(
                    bet_executor,
                    append_bet,
                    result,
                    bet_result,
                )
        except Exception as e:
            if netstatus.is_network_error(e):
                netstatus.waiting(
                    "betway"
                )  # silent: the pulsing WAITING line is the only output
            else:
                print(f"[orchestrator] ERROR: bet placement failed — {e}")


# ============================================================
# CLEAN SHUTDOWN (Ctrl+C): no red errors, no traceback
# ============================================================

_state = {"placer": None, "stopped": False}


class StopBot(Exception):
    """Raised when the dashboard's Stop button was pressed (same clean path as Ctrl+C)."""


async def control_watcher():
    """Watches the run flag written by the dashboard controller."""
    while True:
        await asyncio.sleep(1)
        if not controls.run_requested():
            raise StopBot()


def _safe_stop(placer):
    """Closes the browser session; swallows the errors Playwright raises when Ctrl+C
    has already killed its driver."""
    if _state["stopped"]:
        return
    _state["stopped"] = True
    try:
        placer.stop()
    except BaseException:
        pass


def _clean_exit(message="Stopped by user."):
    print(f"\n[orchestrator] {message}")
    placer = _state.get("placer")
    if placer is not None and not _state["stopped"]:
        try:
            bet_executor.submit(_safe_stop, placer).result(timeout=3)
        except BaseException:
            pass
    try:
        bet_executor.shutdown(wait=False, cancel_futures=True)
    except BaseException:
        pass
    try:
        sys.stdout.flush()
    except BaseException:
        pass
    try:
        sys.stderr = open(os.devnull, "w")  # nothing red on the way out
    except BaseException:
        pass
    os._exit(0)  # don't wait on worker threads (that wait is what printed the errors)


async def main():
    already_bet = load_bet_history()
    print(
        f"[history] Loaded {len(already_bet)} previously placed bets from {BET_HISTORY_FILE}"
    )

    ensure_csv_schema()
    print(f"[csv] Placed bets will be logged to {BET_LOG_CSV}")

    loop = asyncio.get_running_loop()
    football_cache = None
    basketball_cache = None
    placer = BetPlacer(max_stake=MAX_STAKE_NAIRA, daily_cap=DAILY_CAP_NAIRA)
    _state["placer"] = placer

    # Everything from here on is covered by the cleanup in `finally`, so pressing
    # Ctrl+C at any stage (start-up included) closes the browser quietly.
    try:
        # 1) Log in to Betway and settle Pending rows FIRST (needs no odds caches)
        print("[orchestrator] Starting bet placer session...")
        await loop.run_in_executor(bet_executor, placer.start)
        print("[orchestrator] Bet placer session ready.")

        print("[settle] Startup pass: updating Pending rows in the CSV...")
        await run_settlement(placer, SETTLE_FIRST_RUN_MAX_PAGES, "startup")

        # 2) Only then start the odds caches
        football_cache = OddsCache()
        football_cache.start()

        basketball_cache = BasketballOddsCache()
        basketball_cache.start()

        print(
            "[orchestrator] Waiting for first Betway fetches (football + basketball)..."
        )
        while not (football_cache.is_ready() and basketball_cache.is_ready()):
            await asyncio.sleep(0.1)
        print("[orchestrator] Both caches ready.")

        print("[orchestrator] Starting Pinnacle stream...")
        out_queue = asyncio.Queue()
        streamer = OddsStreamer(out_queue)

        def quiet_asyncio_background_errors(loop, context):
            exc = context.get("exception")
            if exc is not None and isinstance(exc, OSError):
                return
            loop.default_exception_handler(context)

        loop.set_exception_handler(quiet_asyncio_background_errors)

        await asyncio.gather(
            streamer.run(),
            consume_and_evaluate(
                streamer, football_cache, basketball_cache, placer, already_bet
            ),
            settlement_loop(placer),
            control_watcher(),
        )
    finally:
        for cache in (football_cache, basketball_cache):
            if cache is not None:
                cache._stop_event.set()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(bet_executor, _safe_stop, placer), timeout=4
            )
        except BaseException:
            pass


def _run_worker():
    """The bot itself (what `python main.py` used to run directly)."""
    if not controls.run_requested():
        print("[orchestrator] Stop was requested from the dashboard — not starting.")
        return
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _clean_exit()
    except StopBot:
        _clean_exit("Stopped from the dashboard.")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        _run_worker()
    else:
        # Normal start: the controller runs the bot in this terminal and serves the
        # dashboard (Start / Stop buttons, market switches) at http://127.0.0.1:8765
        import supervisor

        supervisor.run()
