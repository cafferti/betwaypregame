"""
Shared settings written by the dashboard controller and read by the bot.

bot_controls.json (next to the scripts) holds:
    "markets": {category: true/false}   which bet categories the bot may place
    "run":     true/false               false = the bot shuts itself down (like Ctrl+C)
    "stakes":  {category: naira}        stake per bet for each category (default 100)
    "token":   random string            the dashboard must send it to change anything

Default (no file): every category ON, run = True.
"""

import json
import os
import secrets
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROLS_FILE = os.path.join(SCRIPT_DIR, "bot_controls.json")

# key, label, sport, line colour on the charts
CATEGORIES = [
    {
        "key": "bb_moneyline",
        "label": "Basketball Moneyline",
        "short": "BB Moneyline",
        "sport": "basketball",
        "color": "#ff9100",
    },
    {
        "key": "bb_spread",
        "label": "Basketball Spreads",
        "short": "BB Spreads",
        "sport": "basketball",
        "color": "#ffd600",
    },
    {
        "key": "bb_over",
        "label": "Basketball Overs",
        "short": "BB Overs",
        "sport": "basketball",
        "color": "#ff4081",
    },
    {
        "key": "bb_under",
        "label": "Basketball Unders",
        "short": "BB Unders",
        "sport": "basketball",
        "color": "#d500f9",
    },
    {
        "key": "fb_moneyline",
        "label": "Football Moneyline",
        "short": "FB Moneyline",
        "sport": "football",
        "color": "#00e676",
    },
    {
        "key": "fb_spread",
        "label": "Football Spreads",
        "short": "FB Spreads",
        "sport": "football",
        "color": "#c6ff00",
    },
    {
        "key": "fb_over",
        "label": "Football Overs",
        "short": "FB Overs",
        "sport": "football",
        "color": "#1de9b6",
    },
    {
        "key": "fb_under",
        "label": "Football Unders",
        "short": "FB Unders",
        "sport": "football",
        "color": "#7c4dff",
    },
]
KEYS = [c["key"] for c in CATEGORIES]

DEFAULT_STAKE = 100.0
MIN_STAKE = 10.0
MAX_STAKE = 5000.0  # safety cap: the bot never stakes more than this on one bet

_lock = threading.Lock()
_cache = {"at": 0.0, "state": None}


def category_of(sport, line_type, outcome):
    """Maps a bet to one of the 8 categories (None if it isn't one of them)."""
    s = {"basketball": "bb", "football": "fb"}.get((sport or "").lower())
    if s is None:
        return None
    if line_type == "money_line":
        return f"{s}_moneyline"
    if line_type == "spread":
        return f"{s}_spread"
    if line_type == "total":
        o = (outcome or "").lower()
        if o in ("over", "under"):
            return f"{s}_{o}"
    return None


def _defaults():
    return {
        "run": True,
        "markets": {k: True for k in KEYS},
        "stakes": {k: DEFAULT_STAKE for k in KEYS},
        "token": "",
    }


def _read_file():
    state = _defaults()
    try:
        with open(CONTROLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state["run"] = bool(data.get("run", True))
        state["token"] = str(data.get("token", ""))
        for k in KEYS:
            state["markets"][k] = bool((data.get("markets") or {}).get(k, True))
            try:
                v = float((data.get("stakes") or {}).get(k, DEFAULT_STAKE))
                state["stakes"][k] = v if MIN_STAKE <= v <= MAX_STAKE else DEFAULT_STAKE
            except (TypeError, ValueError):
                state["stakes"][k] = DEFAULT_STAKE
    except (OSError, ValueError):
        pass
    return state


def load_state(max_age=0.5):
    """Current settings (cached for max_age seconds so hot paths stay cheap)."""
    now = time.time()
    with _lock:
        if _cache["state"] is None or now - _cache["at"] >= max_age:
            _cache["state"] = _read_file()
            _cache["at"] = now
        return json.loads(json.dumps(_cache["state"]))


def _write(state):
    tmp = CONTROLS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    for attempt in range(5):  # Windows can briefly lock the file while it is being read
        try:
            os.replace(tmp, CONTROLS_FILE)
            break
        except PermissionError:
            time.sleep(0.05)
    with _lock:
        _cache["state"] = state
        _cache["at"] = time.time()


def set_market(key, on):
    if key not in KEYS:
        raise KeyError(key)
    with _lock:
        _cache["at"] = 0.0
    state = _read_file()
    state["markets"][key] = bool(on)
    _write(state)
    return state


def set_stake(key, amount):
    """Stake per bet for one category. Raises KeyError (unknown category) or ValueError."""
    if key not in KEYS:
        raise KeyError(key)
    v = round(float(amount), 2)
    if not (MIN_STAKE <= v <= MAX_STAKE):
        raise ValueError(f"stake must be between {MIN_STAKE:g} and {MAX_STAKE:g}")
    with _lock:
        _cache["at"] = 0.0
    state = _read_file()
    state["stakes"][key] = v
    _write(state)
    return state


def stake_for(category):
    """Stake in naira for a bet category (the default for anything unknown)."""
    if category is None:
        return DEFAULT_STAKE
    return load_state()["stakes"].get(category, DEFAULT_STAKE)


def set_run(on):
    state = _read_file()
    state["run"] = bool(on)
    _write(state)
    return state


def new_session():
    """Called when the controller starts: run = True and a fresh token."""
    state = _read_file()
    state["run"] = True
    state["token"] = secrets.token_hex(16)
    _write(state)
    return state


def is_enabled(category):
    if category is None:
        return True
    return load_state()["markets"].get(category, True)


def run_requested():
    return load_state()["run"]


def token():
    return load_state(0.0)["token"]
