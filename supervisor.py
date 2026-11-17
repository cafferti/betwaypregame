"""
Controller that sits in front of the bot.

`python main.py` now starts THIS controller, which

  * runs the bot (`python main.py --worker`) in the same terminal, exactly as before
  * serves the dashboard at http://127.0.0.1:8765  (the dashboard.html file also works)
  * lets the dashboard Start / Stop the bot and switch bet categories on / off

Stop  = the same clean shutdown as pressing Ctrl+C in the terminal.
Start = the same as typing  python main.py  again.
Ctrl+C in the terminal still stops everything.
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import controls

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(SCRIPT_DIR, "main.py")
DASHBOARD_HTML = os.path.join(SCRIPT_DIR, "dashboard.html")
DATA_JSON = os.path.join(SCRIPT_DIR, "dashboard_data.json")
PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
STOP_GRACE_SEC = 20  # how long the bot gets to shut itself down before it is terminated

_child = None
_child_lock = threading.Lock()
_data_cache = {"mtime": None, "bytes": b"", "gen": None}


def _data_snapshot():
    """Latest data file written by the bot (re-read only when the file changes)."""
    try:
        m = os.path.getmtime(DATA_JSON)
    except OSError:
        return None
    if m != _data_cache["mtime"]:
        try:
            with open(DATA_JSON, "rb") as f:
                raw = f.read()
            _data_cache.update(mtime=m, bytes=raw, gen=json.loads(raw).get("generated"))
        except (OSError, ValueError):
            pass  # being rewritten right now: keep serving the previous copy
    return _data_cache if _data_cache["bytes"] else None


def child_alive():
    return _child is not None and _child.poll() is None


def start_bot():
    global _child
    with _child_lock:
        if child_alive():
            return False
        controls.set_run(True)
        _child = subprocess.Popen(
            [sys.executable, "-u", MAIN, "--worker"], cwd=SCRIPT_DIR
        )
        return True


def _force_after(proc, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.3)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def stop_bot():
    """Asks the bot to shut down cleanly (it watches the run flag)."""
    with _child_lock:
        proc = _child
    controls.set_run(False)
    if proc is not None and proc.poll() is None:
        threading.Thread(
            target=_force_after, args=(proc, STOP_GRACE_SEC), daemon=True
        ).start()
        return True
    return False


def state_payload():
    st = controls.load_state(0.0)
    return {
        "running": child_alive(),
        "pid": _child.pid if child_alive() else None,
        "run_requested": st["run"],
        "markets": st["markets"],
        "stakes": st["stakes"],
        "server_time": int(time.time() * 1000),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # -- helpers --------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        tok = controls.token()
        return bool(tok) and self.headers.get("X-Token", "") == tok

    # -- routes ---------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html", "/dashboard.html"):
                try:
                    with open(DASHBOARD_HTML, "r", encoding="utf-8") as f:
                        page = f.read()
                except OSError:
                    page = (
                        "<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=3>"
                        "<body style='font:16px sans-serif;background:#0b0e14;color:#d1d4dc;padding:30px'>"
                        "Starting up… the bot is writing the dashboard (this page reloads itself)."
                    )
                # the page served from here always carries the CURRENT token (no CORS headers on purpose)
                inject = (
                    "<script>window.__TOKEN__="
                    + json.dumps(controls.token())
                    + ";</script>"
                )
                page = page.replace("<head>", "<head>" + inject, 1)
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                if not self._authorized():
                    return self._json(401, {"error": "bad token — reload the page"})
                self._json(200, state_payload())
            elif path == "/api/data":
                if not self._authorized():
                    return self._json(401, {"error": "bad token — reload the page"})
                snap = _data_snapshot()
                if snap is None:
                    return self._json(404, {"error": "no data yet"})
                have = parse_qs(parsed.query).get("gen", [""])[0]
                if have and str(snap["gen"]) == have:
                    return self._json(200, {"unchanged": True})
                body = snap["bytes"]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_error(500, str(e)[:100])
            except Exception:
                pass

    def do_POST(self):
        try:
            if not self._authorized():
                return self._json(401, {"error": "bad token — reload the page"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            path = self.path.split("?")[0]
            if path == "/api/toggle":
                controls.set_market(str(body.get("key")), bool(body.get("on")))
                return self._json(200, state_payload())
            if path == "/api/stake":
                try:
                    controls.set_stake(str(body.get("key")), body.get("amount"))
                except (TypeError, ValueError) as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, state_payload())
            if path == "/api/bot":
                action = body.get("action")
                if action == "start":
                    start_bot()
                elif action == "stop":
                    stop_bot()
                else:
                    return self._json(400, {"error": "action must be start or stop"})
                return self._json(200, state_payload())
            self.send_error(404)
        except KeyError:
            self._json(400, {"error": "unknown market"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json(500, {"error": str(e)[:100]})
            except Exception:
                pass


def run():
    controls.new_session()  # run = True, fresh token (markets keep their saved on/off state)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(
            f"[control] dashboard + Start/Stop buttons: http://127.0.0.1:{PORT}",
            flush=True,
        )
    except OSError as e:
        print(
            f"[control] WARNING: could not open port {PORT} ({e}) — dashboard buttons unavailable",
            flush=True,
        )

    start_bot()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    # Ctrl+C in the terminal reaches the bot too; give it a moment to close cleanly.
    controls.set_run(False)
    proc = _child
    if proc is not None:
        try:
            proc.wait(timeout=12)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    try:
        sys.stdout.flush()
        sys.stderr = open(os.devnull, "w")
    except Exception:
        pass
    os._exit(0)
