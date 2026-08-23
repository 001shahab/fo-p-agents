#!/usr/bin/env python3
"""
app.py - the interface to the agent test harness
=================================================

Author  : Prof. Shahab Anbarjafari
Purpose : Serve the three screens that choose an agent, build data for it and
          watch it run, backed by the harness in TestAgent.py.

    python app.py

Nothing has to be installed and nothing has to be built. The server is the
standard library's own, the interface is React served from the static folder,
and the browser opens on its own. A test harness that needs its own set-up
notes is a test harness people stop running, so it has none.

How it is put together
----------------------
A handful of requests, and no more:

    POST /api/unlock        exchange the passphrase for a session token
    GET  /api/agents        what can be tested, and which model is configured
    POST /api/synthesise    build the data for one agent, return a preview
    GET  /api/run           run the agent, streaming the log as it happens

The run is streamed as server-sent events rather than polled, because the point
of the third screen is to watch the agent work. Each line the agent prints is
forwarded the moment it is printed, and the harness's reading of those lines
arrives on the same stream a little behind them.

The passphrase is checked here rather than in the browser. A gate that the page
enforces on itself is a picture of a lock, and anyone who opened the developer
tools would be through it; this one issues a token that every later request has
to carry, so the only way past is to know the phrase.

State lives in memory for as long as the process runs. This is one person's
tool on one person's machine, and a database would be a way of looking busy.
"""

from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import queue
import secrets
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import TestAgent
from TestAgent import AGENTS, AGENT_BY_KEY, Dataset, Harness, preview_file

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

DEFAULT_PORT = 8420
PREVIEW_ROWS = 12

# Overridable for anyone who would rather not have it written down, but there
# has to be a working default or the tool needs a set-up note to start.
PASSPHRASE = os.environ.get("HARNESS_PASSWORD", "PwC%2026")

# Long enough that a wrong guess is never worth automating, short enough that
# the person who mistyped it does not think the page has hung.
WRONG_GUESS_PAUSE = 0.6


# ===========================================================================
# What the server remembers
# ===========================================================================

class Session:
    """The datasets built so far, and the harness that built them.

    One instance per process. The lock matters because the browser can ask for
    a preview while a run is streaming, and both touch the same harness.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._harness: Optional[Harness] = None
        self._use_model = False
        self._tokens: Set[str] = set()
        self.datasets: Dict[str, Dataset] = {}

    # -- who is allowed in --------------------------------------------------

    def grant(self) -> str:
        """A token for someone who has just given the right passphrase."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens.add(token)
        return token

    def holds(self, token: str) -> bool:
        with self._lock:
            return bool(token) and token in self._tokens

    def harness(self, use_model: bool) -> Harness:
        """The harness, rebuilt if the model setting changed."""
        with self._lock:
            if self._harness is None or self._use_model != use_model:
                self._harness = Harness(use_model=use_model)
                self._use_model = use_model
            return self._harness

    def remember(self, dataset: Dataset) -> None:
        with self._lock:
            self.datasets[dataset.agent] = dataset

    def dataset(self, agent: str) -> Optional[Dataset]:
        with self._lock:
            return self.datasets.get(agent)

    def forget(self) -> None:
        with self._lock:
            self.datasets.clear()


SESSION = Session()


# ===========================================================================
# Request handling
# ===========================================================================

class Handler(BaseHTTPRequestHandler):
    """Routes requests to the handful of things this application does."""

    server_version = f"TestAgent/{TestAgent.HARNESS_VERSION}"
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        """Quieten the default access log; only failures are worth printing."""
        if args and str(args[0]).startswith(("GET /api", "POST /api")):
            return
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            sys.stderr.write(f"  {args[0]} -> {args[1]}\n")

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _fail(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- the gate -----------------------------------------------------------

    def _admitted(self, query: Dict[str, List[str]]) -> bool:
        """Whether this request carries a token issued by /api/unlock.

        The header is the normal route. The query string exists because two
        things the browser does cannot carry headers at all: an EventSource for
        the run stream, and a plain link for a download.
        """
        token = self.headers.get("X-Session") or (query.get("token") or [""])[0]
        return SESSION.holds(token)

    def unlock(self) -> None:
        """Trade the passphrase for a token, or say no and wait a moment."""
        given = str(self._body().get("password") or "")
        if not hmac.compare_digest(given, PASSPHRASE):
            time.sleep(WRONG_GUESS_PAUSE)
            return self._fail(401, "That is not the passphrase.")
        self._json({"token": SESSION.grant()})

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:                           # noqa: N802 - stdlib name
        route = urlparse(self.path)
        query = parse_qs(route.query)
        try:
            if route.path.startswith("/api/") and not self._admitted(query):
                return self._fail(401, "This session is not unlocked.")
            if route.path == "/api/agents":
                return self.agents()
            if route.path == "/api/preview":
                return self.preview(query)
            if route.path == "/api/run":
                return self.run(query)
            if route.path == "/api/output":
                return self.output(query)
            if route.path == "/api/download":
                return self.download(query)
            if route.path.startswith("/api/"):
                return self._fail(404, "No such endpoint.")
            return self.static(route.path)
        except BrokenPipeError:
            pass                                        # the browser went away
        except Exception:
            traceback.print_exc()
            self._fail(500, "The server hit an unexpected error. See the terminal.")

    def do_POST(self) -> None:                          # noqa: N802 - stdlib name
        route = urlparse(self.path)
        query = parse_qs(route.query)
        try:
            if route.path == "/api/unlock":
                return self.unlock()
            if route.path.startswith("/api/") and not self._admitted(query):
                return self._fail(401, "This session is not unlocked.")
            if route.path == "/api/synthesise":
                return self.synthesise()
            if route.path == "/api/reset":
                SESSION.forget()
                return self._json({"ok": True})
            return self._fail(404, "No such endpoint.")
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._fail(500, "The server hit an unexpected error. See the terminal.")

    def do_HEAD(self) -> None:                          # noqa: N802 - stdlib name
        self.do_GET()

    # -- static files -------------------------------------------------------

    def static(self, path: str) -> None:
        """Serve the interface, refusing anything outside the static folder."""
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        if relative.startswith("static/"):
            relative = relative[len("static/"):]

        target = (STATIC / relative).resolve()
        try:
            target.relative_to(STATIC.resolve())
        except ValueError:
            return self._fail(403, "Outside the served folder.")

        if not target.is_file():
            return self._fail(404, "Not found.")

        kind, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".js":
            kind = "text/javascript; charset=utf-8"
        elif target.suffix == ".css":
            kind = "text/css; charset=utf-8"
        elif target.suffix == ".html":
            kind = "text/html; charset=utf-8"
        elif target.suffix == ".woff2":
            # Not in every machine's mime database, and a font served as a
            # generic byte stream is a font the browser declines to use.
            kind = "font/woff2"
        self._send(200, target.read_bytes(), kind or "application/octet-stream")

    # -- the three things this application does -----------------------------

    def agents(self) -> None:
        """What can be tested, and what the model tier is set to."""
        harness = SESSION.harness(use_model=False)
        self._json({
            "version": TestAgent.HARNESS_VERSION,
            "agents": [spec.as_dict() for spec in AGENTS],
            "model": {
                "configured": bool(harness.config.api_key),
                "backend": harness.config.backend,
                "name": harness.config.model,
                "label": ("Azure OpenAI" if harness.config.backend == "azure"
                          else "OpenAI"),
            },
        })

    def synthesise(self) -> None:
        """Build the input for one agent and hand back a preview of it."""
        payload = self._body()
        agent = str(payload.get("agent") or "")
        if agent not in AGENT_BY_KEY:
            return self._fail(400, "Choose one of the agents first.")

        use_model = bool(payload.get("use_model"))
        seed = payload.get("seed")
        seed = int(seed) if isinstance(seed, (int, float, str)) and str(seed).strip() else None

        harness = SESSION.harness(use_model)
        dataset = harness.synthesise(agent, seed=seed, enrich=use_model)
        SESSION.remember(dataset)

        previews = []
        for generated in dataset.files:
            preview = preview_file(generated.path, PREVIEW_ROWS)
            previews.append({
                **generated.as_dict(dataset.root),
                "preview": preview,
            })

        self._json({
            "agent": agent,
            "seed": dataset.seed,
            "planted": dataset.planted,
            "facts": dataset.facts,
            "files": previews,
            "total_rows": sum(item.rows for item in dataset.files),
            "model_phrasings": dataset.model_phrasings,
            "used_model": use_model and harness.model.available,
        })

    def preview(self, query: Dict[str, List[str]]) -> None:
        """More rows from a file that was generated for a test."""
        agent = (query.get("agent") or [""])[0]
        wanted = (query.get("file") or [""])[0]
        limit = min(int((query.get("limit") or ["40"])[0]), 500)

        dataset = SESSION.dataset(agent)
        if dataset is None:
            return self._fail(404, "Build the data for this agent first.")

        for generated in dataset.files:
            if generated.path.name == wanted:
                return self._json({"file": wanted,
                                   **preview_file(generated.path, limit)})
        self._fail(404, "No such file in this dataset.")

    def run(self, query: Dict[str, List[str]]) -> None:
        """Run the agent, streaming everything it says as it says it."""
        agent = (query.get("agent") or [""])[0]
        use_model = (query.get("use_model") or ["0"])[0] in {"1", "true", "yes"}

        dataset = SESSION.dataset(agent)
        if dataset is None:
            return self._fail(404, "Build the data for this agent first.")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        # The harness runs on its own thread and posts events into a queue; this
        # thread does nothing but move them onto the wire. Keeping the two apart
        # means a slow browser cannot stall the agent, and a slow agent cannot
        # make the connection look dead.
        events: "queue.Queue[Optional[Tuple[str, Dict[str, Any]]]]" = queue.Queue()

        def emit(kind: str, payload: Dict[str, Any]) -> None:
            events.put((kind, payload))

        def work() -> None:
            try:
                harness = SESSION.harness(use_model)
                outcome = harness.run(dataset, emit)
                events.put(("result", outcome.as_dict()))
            except Exception as error:
                traceback.print_exc()
                events.put(("failed", {"message": str(error)}))
            finally:
                events.put(None)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        try:
            while True:
                try:
                    event = events.get(timeout=15)
                except queue.Empty:
                    # A comment keeps the connection alive through any proxy or
                    # power-saving layer between here and the browser.
                    self.wfile.write(b": waiting\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                    break
                kind, payload = event
                body = json.dumps(payload, ensure_ascii=False)
                self.wfile.write(f"event: {kind}\ndata: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                                        # the browser navigated away

    def output(self, query: Dict[str, List[str]]) -> None:
        """A preview of a file the agent wrote."""
        path = self._result_path(query)
        if path is None:
            return self._fail(404, "No such result file.")
        if path.suffix.lower() != ".csv":
            text = path.read_text(encoding="utf-8", errors="replace")
            return self._json({"file": path.name, "text": text[:20000],
                               "truncated": len(text) > 20000})
        limit = min(int((query.get("limit") or ["25"])[0]), 500)
        self._json({"file": path.name, **preview_file(path, limit)})

    def download(self, query: Dict[str, List[str]]) -> None:
        """Hand over a result file whole."""
        path = self._result_path(query)
        if path is None:
            return self._fail(404, "No such result file.")
        self._send(200, path.read_bytes(), "application/octet-stream",
                   {"Content-Disposition": f'attachment; filename="{path.name}"'})

    def _result_path(self, query: Dict[str, List[str]]) -> Optional[Path]:
        """Resolve a requested result file, refusing anything outside the run."""
        agent = (query.get("agent") or [""])[0]
        wanted = (query.get("file") or [""])[0]
        dataset = SESSION.dataset(agent)
        if dataset is None or not wanted:
            return None
        results = (dataset.root / "results").resolve()
        target = (results / wanted).resolve()
        try:
            target.relative_to(results)
        except ValueError:
            return None
        return target if target.is_file() else None


# ===========================================================================
# Starting up
# ===========================================================================

def free_port(preferred: int, attempts: int = 20) -> int:
    """The first port at or after ``preferred`` that nothing is holding."""
    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(f"No free port between {preferred} and {preferred + attempts}.")


def check_assets() -> None:
    """Fail early and clearly if the interface is not where it should be."""
    missing = [name for name in ("index.html", "css/app.css", "js/app.js",
                                 "js/vendor/react.production.min.js",
                                 "js/vendor/react-dom.production.min.js",
                                 "img/logo.png",
                                 "fonts/playfair-display-latin.woff2")
               if not (STATIC / name).is_file()]
    if missing:
        raise SystemExit("The interface is incomplete. Missing from static/: "
                         + ", ".join(missing))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Start the agent test harness interface.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to bind (default 127.0.0.1, this machine only)")
    args = parser.parse_args(argv)

    check_assets()
    port = free_port(args.port)
    address = f"http://{args.host}:{port}"

    harness = SESSION.harness(use_model=False)
    print()
    print(f"  TestAgent {TestAgent.HARNESS_VERSION}")
    print(f"  Interface   {address}")
    print(f"  Agents      {len(AGENTS)} available")
    print(f"  Model       {'configured, ' + harness.config.model if harness.config.api_key else 'not configured - the harness will run on local rules'}")
    print(f"  Passphrase  {PASSPHRASE if 'HARNESS_PASSWORD' not in os.environ else 'taken from HARNESS_PASSWORD'}")
    print()
    print("  Press Ctrl+C to stop.")
    print()

    server = ThreadingHTTPServer((args.host, port), Handler)
    server.daemon_threads = True

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(address)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
