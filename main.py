import asyncio
import concurrent.futures
import json
import os
import sys
import threading

from engineoddsretriever import OddsCache, BasketballOddsCache
from pinnacleretriever import OddsStreamer
from filter import evaluate_pinnacle_event, stats
from betplacer import BetPlacer

STAKE_NAIRA = 100
MAX_STAKE_NAIRA = 500
DAILY_CAP_NAIRA = 5000
SUMMARY_EVERY_N_EVENTS = 50

BET_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "placed_bets.json"
)
_history_lock = threading.Lock()

bet_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


# ============================================================
# SOUND ALERTS
# ============================================================


def _play_sound(kind: str):
    """kind: 'passed' or 'placed' — distinct tones, Windows only (winsound)."""
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


def _build_bet_event(result: dict):
    return {
        "eventId": result["betway_event_id"],
        "marketId": result["betway_market_id"],
        "outcomeId": result["outcomeId"],
        "priceNum": result["priceNum"],
        "priceDen": result["priceDen"],
        "priceDec": result["betway_price"],
        "handicap": result.get("betway_line") or 0,
        "eventVersion": result["eventVersion"],
        "marketVersion": result["marketVersion"],
        "outcomeVersion": result["outcomeVersion"],
        "priceVersion": result["priceVersion"],
        "serverEmopSource": result["serverEmopSource"],
        "publicHubPublishedTime": result["publicHubPublishedTime"],
    }


def _describe_result(result: dict) -> str:
    return f"{result['home']} vs {result['away']} [{result['league']}] {result['outcome']} {result['points']}"


def _print_filter_pass(result: dict):
    print(
        f"🟢 PASSED FILTER — {_describe_result(result)} "
        f"| EV {result['ev_percent']}% | Betway {result['betway_price']} vs NVP {result['nvp']} "
        f"| drop {result['drop_percent']}% | {result['minutes_to_kickoff']:.0f}min to kickoff"
    )
    _play_sound("passed")


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
            print(
                f"❌ BET REJECTED — {desc} | placementStatus={inner.get('placementStatus')} "
                f"errorCode={inner.get('errorCode')} hasPriceChanged={inner.get('hasPriceChanged')}"
            )
            return False
    else:
        note = bet_result.get("note") or response
        print(f"❌ BET FAILED — {desc} | reason: {note}")
        return False


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
        betway_rows = football_cache.get_rows() + basketball_cache.get_rows()

        try:
            result = evaluate_pinnacle_event(event, betway_rows)
        except Exception as e:
            print(f"[orchestrator] ERROR: evaluate_pinnacle_event failed — {e}")
            continue

        if stats.total_fetched % SUMMARY_EVERY_N_EVENTS == 0:
            print(stats.summary_line())

        if result is None:
            continue

        _print_filter_pass(result)

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

        outcome_key = result["outcomeId"]
        if outcome_key in already_bet:
            print(
                f"⏭️  BET SKIPPED [already_bet_this_outcome] — {_describe_result(result)}"
            )
            continue

        bet_event = _build_bet_event(result)

        try:
            bet_result = await loop.run_in_executor(
                bet_executor, placer.try_place, bet_event, STAKE_NAIRA
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
        except Exception as e:
            print(f"[orchestrator] ERROR: bet placement failed — {e}")


async def main():
    already_bet = load_bet_history()
    print(
        f"[history] Loaded {len(already_bet)} previously placed bets from {BET_HISTORY_FILE}"
    )

    football_cache = OddsCache()
    football_cache.start()

    basketball_cache = BasketballOddsCache()
    basketball_cache.start()

    print("[orchestrator] Waiting for first Betway fetches (football + basketball)...")
    while not (football_cache.is_ready() and basketball_cache.is_ready()):
        await asyncio.sleep(0.1)
    print("[orchestrator] Both caches ready. Starting bet placer session...")

    placer = BetPlacer(max_stake=MAX_STAKE_NAIRA, daily_cap=DAILY_CAP_NAIRA)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(bet_executor, placer.start)
    print("[orchestrator] Bet placer session ready. Starting Pinnacle stream...")

    out_queue = asyncio.Queue()
    streamer = OddsStreamer(out_queue)

    def quiet_asyncio_background_errors(loop, context):
        exc = context.get("exception")
        if exc is not None and isinstance(exc, OSError):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(quiet_asyncio_background_errors)

    try:
        await asyncio.gather(
            streamer.run(),
            consume_and_evaluate(
                streamer, football_cache, basketball_cache, placer, already_bet
            ),
        )
    finally:
        await loop.run_in_executor(bet_executor, placer.stop)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[orchestrator] Stopped by user.")
