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

The same command is what a hosting platform runs. Where one hands the process a
port to listen on, the defaults invert on their own: every interface rather than
the loopback, that exact port rather than the first free one, and no attempt to
open a browser on a machine that has no screen. See ``hosted`` below.

How it is put together
----------------------
A handful of requests, and no more:

    GET  /healthz           a fixed answer for whatever is watching from outside
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
from runtime import prepare_hub_environment

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


def hosted() -> bool:
    """Whether something other than a person is deciding how this is reached.

    Every platform that runs a web process hands it a port in the environment
    and then waits for something to answer on exactly that port. That single
    fact is enough to tell a deployment from somebody's laptop, and all three
    defaults that differ between the two follow from it, so there is no second
    switch to remember and nothing to pass on the command line.
    """
    return bool(os.environ.get("PORT"))


# ===========================================================================
# What the server remembers
# ===========================================================================

class Session:
    """The datasets built so far, and the harness that built them.

    One instance per process. The lock matters because the browser can ask for
    a preview while a run is streaming, and both touch the same harness.
    """

    # Granted tokens are kept on disk as well as in memory. A hosted process
    # restarts whenever the platform decides to, and until this was persisted a
    # restart turned every open browser into "This session is not unlocked."
    # with no way back except a reload. Tokens expire so the file cannot grow
    # without bound and an old one cannot be used indefinitely.
    TOKEN_LIFETIME = 12 * 60 * 60

    def __init__(self, store: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._harness: Optional[Harness] = None
        self._use_model = False
        self._store = store
        self._tokens: Dict[str, float] = self._read()
        self.datasets: Dict[str, Dataset] = {}

    # -- who is allowed in --------------------------------------------------

    def _read(self) -> Dict[str, float]:
        if self._store is None or not self._store.is_file():
            return {}
        try:
            payload = json.loads(self._store.read_text(encoding="utf-8"))
            issued = {str(token): float(when) for token, when in payload.items()}
        except (OSError, ValueError, AttributeError):
            return {}
        cutoff = time.time() - self.TOKEN_LIFETIME
        return {token: when for token, when in issued.items() if when >= cutoff}

    def _write(self) -> None:
        """Persist under the lock. A failure here must not refuse the caller."""
        if self._store is None:
            return
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            self._store.write_text(json.dumps(self._tokens), encoding="utf-8")
        except OSError:
            pass

    def grant(self) -> str:
        """A token for someone who has just given the right passphrase."""
        token = secrets.token_urlsafe(24)
        cutoff = time.time() - self.TOKEN_LIFETIME
        with self._lock:
            self._tokens = {held: when for held, when in self._tokens.items()
                            if when >= cutoff}
            self._tokens[token] = time.time()
            self._write()
        return token

    def holds(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            issued = self._tokens.get(token)
            if issued is None:
                return False
            if issued < time.time() - self.TOKEN_LIFETIME:
                del self._tokens[token]
                self._write()
                return False
            return True

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


SESSION = Session(TestAgent.WORKSPACE / "sessions.json")


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
            if route.path == "/healthz":
                return self.health()
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

    def health(self) -> None:
        """Confirm the process is up and the interface is intact.

        Outside /api/ and outside the gate, both deliberately. A platform's
        health check has no passphrase, and a service that answered "locked"
        would be judged unhealthy and restarted for ever.
        """
        self._json({
            "status": "ok",
            "version": TestAgent.HARNESS_VERSION,
            "agents": len(AGENTS),
        })

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
    prepare_hub_environment()
    away = hosted()
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Start the agent test harness interface.")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT") or DEFAULT_PORT),
                        help=f"port to listen on (default $PORT, or {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("--host", default="0.0.0.0" if away else "127.0.0.1",
                        help="address to bind (default 127.0.0.1, this machine "
                             "only; 0.0.0.0 when $PORT is set)")
    args = parser.parse_args(argv)

    if away:
        # A platform reads this process through a pipe, and a pipe is buffered
        # in blocks rather than lines, so everything below would sit unwritten
        # until something eventually filled it. Whoever is reading a deploy log
        # should not have to wonder whether the process reached the next line.
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(line_buffering=True)

    check_assets()

    # Stepping to the next free port is the friendly thing to do on a laptop,
    # where the alternative is a stack trace because yesterday's copy is still
    # running. It is the wrong thing to do under a platform, which is waiting
    # on one particular port and will call the deploy a failure if nothing
    # answers there. Better to fail loudly on the port that was asked for.
    port = args.port if away else free_port(args.port)
    address = os.environ.get("RENDER_EXTERNAL_URL") or f"http://{args.host}:{port}"

    harness = SESSION.harness(use_model=False)
    print()
    print(f"  TestAgent {TestAgent.HARNESS_VERSION}")
    print(f"  Interface   {address}")
    print(f"  Agents      {len(AGENTS)} available")
    print(f"  Model       {'configured, ' + harness.config.model if harness.config.api_key else 'not configured - the harness will run on local rules'}")
    print(f"  Passphrase  {PASSPHRASE if 'HARNESS_PASSWORD' not in os.environ else 'taken from HARNESS_PASSWORD'}")
    if away and "HARNESS_PASSWORD" not in os.environ:
        # On a laptop the built-in phrase is a convenience. On a public address
        # it is the whole of the security, and it is printed in the repository.
        print()
        print("  The built-in passphrase is published in the source. Set")
        print("  HARNESS_PASSWORD before leaving this reachable from the internet.")
    print()
    if not away:
        print("  Press Ctrl+C to stop.")
        print()

    server = ThreadingHTTPServer((args.host, port), Handler)
    server.daemon_threads = True

    if not args.no_browser and not away:
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
