"""
Network pause/resume for the whole bot.

When any request fails because the internet is down, the bot PAUSES: every fetcher
waits (no retries, no error spam) while ONE line pulses in place:

    ⏳ WAITING FOR NETWORK /  00:42      (bold, colour pulsing yellow <-> red)

A background watcher checks whether Betway and the Pinnacle feed are reachable again.
When both are, it prints a bold green  ✅ NETWORK RESTORED  line and everything resumes.
"""

import asyncio
import os
import socket
import sys
import threading
import time

if sys.platform == "win32":
    os.system("")  # switches on ANSI colours/bold in the Windows console

BOLD = "\033[1m"
GREEN = "\033[1;92m"
RESET = "\033[0m"
_PULSE = (
    "\033[1;93m",
    "\033[1;33m",
    "\033[1;91m",
    "\033[1;33m",
)  # yellow -> red -> yellow
_SPIN = "|/-\\"

PROBE_EVERY_SEC = 2.0
FRAME_SEC = 0.35
# The bot needs BOTH of these to work, so both must be reachable before it resumes.
ONLINE_TARGETS = (("www.betway.com.ng", 443), ("sword.pinnacleoddsdropper.com", 443))

_lock = threading.Lock()
_online = threading.Event()
_online.set()
_watcher = None
_outages = 0

_NET_TYPE_NAMES = {
    "ClientConnectorError",
    "ClientConnectionError",
    "ClientOSError",
    "ServerDisconnectedError",
    "ServerTimeoutError",
    "ClientConnectorDNSError",
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "Timeout",
    "NameResolutionError",
    "NewConnectionError",
    "MaxRetryError",
    "ProtocolError",
    "TimeoutError",
}

_NET_TEXT = (
    "getaddrinfo failed",
    "name or service not known",
    "temporary failure in name resolution",
    "network is unreachable",
    "no route to host",
    "cannot connect to host",
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection closed",
    "remote end closed",
    "timed out",
    "net::err_",
    "eof occurred in violation of protocol",
    "failed to establish a new connection",
    "max retries exceeded",
)


def is_network_error(exc) -> bool:
    """True if the exception (or anything it was raised from) looks like no-internet."""
    depth = 0
    while exc is not None and depth < 6:
        if isinstance(
            exc, (ConnectionError, TimeoutError, socket.gaierror, socket.timeout)
        ):
            return True
        if type(exc).__name__ in _NET_TYPE_NAMES:
            return True
        text = str(exc).lower()
        if any(k in text for k in _NET_TEXT):
            return True
        exc = exc.__cause__ or exc.__context__
        depth += 1
    return False


def _probe_once() -> bool:
    for host, port in ONLINE_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=2.5):
                pass
        except OSError:
            return False
    return True


def _watch(started: float):
    global _watcher, _outages
    tty = sys.stdout.isatty()
    state = {"streak": 0, "stop": False}

    def prober():
        while not state["stop"]:
            state["streak"] = state["streak"] + 1 if _probe_once() else 0
            time.sleep(1.0 if state["streak"] else PROBE_EVERY_SEC)

    threading.Thread(target=prober, daemon=True).start()

    if not tty:  # redirected output: no animation, just one line
        print(f"{BOLD}⏳ WAITING FOR NETWORK...{RESET}", flush=True)

    i = 0
    while state["streak"] < 2:  # two good checks in a row, so a blip doesn't resume it
        if tty:
            secs = int(time.time() - started)
            label = f"⏳ WAITING FOR NETWORK {_SPIN[i % 4]}  {secs // 60:02d}:{secs % 60:02d}"
            sys.stdout.write(f"\r{_PULSE[i % 4]}{label}{RESET}\033[K")
            sys.stdout.flush()
        i += 1
        time.sleep(FRAME_SEC)
    state["stop"] = True

    secs = int(time.time() - started)
    if tty:
        sys.stdout.write("\r\033[K")
    print(
        f"{GREEN}✅ NETWORK RESTORED — resuming (was offline {secs}s){RESET}",
        flush=True,
    )
    with _lock:
        _watcher = None
        _outages += 1
        _online.set()  # releases everything that was paused


def waiting(source: str = "network"):
    """Called when a request failed because the internet is down. Starts the pause."""
    global _watcher
    with _lock:
        if not _online.is_set():
            return  # already paused
        _online.clear()
        _watcher = threading.Thread(target=_watch, args=(time.time(),), daemon=True)
        _watcher.start()


def ok(source: str = "network"):
    """Kept for compatibility; resuming is decided by the watcher."""
    return


def is_down(source=None) -> bool:
    return not _online.is_set()


def outage_count() -> int:
    """How many outages have ended so far (lets callers drop work that went stale)."""
    return _outages


def wait_online(timeout=None) -> bool:
    """Blocks (in a normal thread) while the internet is down."""
    return _online.wait(timeout)


async def wait_online_async():
    """Pauses a coroutine while the internet is down (doesn't block the event loop)."""
    while not _online.is_set():
        await asyncio.sleep(0.5)
