"""HTTP server: the GUI, the JSON API and the report files.

Routes
    GET  /                 the GUI (not in --api-only mode)
    GET  /gui/<file>       GUI files
    GET  /reports/<...>    run reports and screenshots
    GET  /api              API help (also / in --api-only mode)
    GET  /api/config       modes, limits, labels and setting defaults (used by the GUI)
    GET  /api/status       progress and results of the latest run
    GET  /api/jobs/<id>              saved result of a finished run (JSON)
    GET  /api/jobs/<id>/download     whole run / comparison as a ZIP
    GET  /api/runs/<folder>/download one URL's run as a ZIP
    POST /api/run          start a run ({"wait": true} answers when it has finished)
    POST /api/stop         stop the running run

Only files inside the GUI and reports folders are ever served.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import posixpath
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from . import __version__, export
from .chrome import find_chrome
from .config import BurstPlan, HTTP_METHODS, WAIT_MODES, RunConfig, Settings, ValidationError
from .job import Job
from .k6 import find_k6
from .machine import capacity
from .modes import MODES
from .paths import GUI_DIR

log = logging.getLogger("pageload.server")
MAX_BODY_BYTES = 1_000_000


class BusyError(RuntimeError):
    """A run is already in progress."""


class JobManager:
    """Holds the current run. One run at a time."""

    def __init__(self, reports_dir: Path, job_factory: Callable[[RunConfig, Path], Job] = Job):
        self.reports_dir = reports_dir
        self._job_factory = job_factory
        self._lock = threading.Lock()
        self._job: Job | None = None

    @property
    def current(self) -> Job | None:
        return self._job

    def start(self, config: RunConfig) -> Job:
        with self._lock:
            if self._job and self._job.is_running:
                raise BusyError("A run is already in progress.")
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            job = self._job_factory(config, self.reports_dir)
            self._job = job
        threading.Thread(target=job.run, name="pageload-job", daemon=True).start()
        return job

    def stop(self) -> None:
        job = self._job
        if job and job.is_running:
            job.stop()


def config_payload() -> dict[str, Any]:
    return {
        "version": __version__,
        "modes": {name: mode.describe() for name, mode in MODES.items()},
        "settings": Settings.describe(),
        "waitModes": list(WAIT_MODES),
        "methods": list(HTTP_METHODS),
        "machine": capacity().to_dict(),
        "burst": BurstPlan.describe(),
    }


API_HELP = {
    "name": "Page Load Compare API",
    "version": __version__,
    "endpoints": {
        "POST /api/run": {
            "body": {
                "url": "https://hartak.am/  (required)",
                "compareUrl": "https://dev.hartak.am/  (optional)",
                "vus": "parallel users, >= 1. Above the recommended number (browser: depends on this "
                       "computer, see /api/config; API: 200) is allowed but returns a warning",
                "browsers": "browser mode, optional: hybrid mode with this many real browsers measuring "
                            "while the other users load the page with plain HTTP requests (default: all users)",
                "iterations": "visits/requests per URL, >= vus. Above 1000 / 10000 returns a warning",
                "waitUntil": "load (default) or networkidle  (browser mode)",
                "apiOnly": "true = HTTP/API test: plain requests, no browser",
                "mode": "browser (default), api, or burst: separate users at the same moment, in rounds",
                "burstUsers": "burst mode: users per burst, e.g. \"10, 20, 30\" (default 30)",
                "rounds": "burst mode: rounds per burst size, 1-10 (default 3)",
                "pauseSec": "burst mode: pause between bursts, 5-600 s (default 30)",
                "recoverySec": "burst mode: how long to wait for the site to answer again, 10-600 s (default 60)",
                "slowMs": "burst mode: a burst whose first-byte p95 (server time) is above this counts as slow (default 3000)",
                "fullPage": "burst mode: also download the page's own scripts, styles and images (default true)",
                "method": "GET (default), POST, PUT, PATCH, DELETE, HEAD, OPTIONS  (API mode)",
                "headers": "{\"Name\": \"value\"} or \"Name: value\" lines  (API mode)",
                "body": "request body text  (API mode)",
                "settings": {
                    "timeoutSec": "per visit/request, 1-300 (default 30)",
                    "timeLimitSec": "per URL, 0 = automatic (default) or 10-7200",
                    "expectStatus": "200, 2xx or 200,301 (default 200 browser, 2xx API)",
                    "budgetP95Ms": "the run is not OK if p95 is above this, 0 = off",
                    "viewportWidth": "browser, 320-3840 (default 1440)",
                    "viewportHeight": "browser, 320-2160 (default 900)",
                    "locale": "browser language, e.g. hy-AM (default: browser default)",
                    "screenshots": "browser: save screenshots of failed visits (default true)",
                },
                "wait": "true = answer only when the run is finished, with the results",
            },
            "returns": "{ok: true} or, with wait, the same body as GET /api/status",
        },
        "GET /api/status": "state, ok, progress per URL, stats, comparison, log",
        "GET /api/config": "modes, limits, labels and setting defaults",
        "GET /api/jobs/<id>": "saved result of a finished run (the id is in /api/status)",
        "GET /api/jobs/<id>/download": "ZIP: comparison.html, comparison.csv, result.json and every run folder",
        "GET /api/runs/<folder>/download": "ZIP of one URL's run: report.html, visits.csv, raw k6 output, screenshots",
        "POST /api/stop": "stop the running run",
    },
    "states": "running | done | problem (missing visits) | stopped | error",
    "warnings": "runs above the recommended users or visits still start; their warnings are in the "
                "POST /api/run answer and in GET /api/status",
}


def resolve_static(raw_path: str, reports_dir: Path, api_only: bool) -> Path | None:
    """File to serve for a request path, or None.

    ".." and %-escapes are resolved first and the final file must lie inside
    the GUI or reports folder, so paths like /reports/../app.py are refused.
    """
    path = posixpath.normpath(unquote(urlparse(raw_path).path))
    if "\\" in path or "\0" in path:
        return None
    if path in ("/", "/index.html"):
        base, rel = (None, "") if api_only else (GUI_DIR, "index.html")
    elif path.startswith("/gui/"):
        base, rel = (None, "") if api_only else (GUI_DIR, path[len("/gui/"):])
    elif path.startswith("/reports/"):
        base, rel = reports_dir, path[len("/reports/"):]
    else:
        return None
    if base is None or not rel:
        return None
    root = base.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        return None
    return target if target.is_file() else None


class PageLoadServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], manager: JobManager, api_only: bool = False):
        super().__init__(address, RequestHandler)
        self.manager = manager
        self.api_only = api_only


class RequestHandler(BaseHTTPRequestHandler):
    server: PageLoadServer
    server_version = f"PageLoadCompare/{__version__}"

    ROUTES = {
        ("GET", "/api"): "api_help",
        ("GET", "/api/"): "api_help",
        ("GET", "/api/config"): "api_config",
        ("GET", "/healthz"): "healthz",
        ("GET", "/api/status"): "api_status",
        ("POST", "/api/run"): "api_run",
        ("POST", "/api/stop"): "api_stop",
    }

    # Paths with an id in them: (method, pattern, handler).
    PATTERN_ROUTES = (
        ("GET", re.compile(r"^/api/jobs/([^/]+)$"), "api_job"),
        ("GET", re.compile(r"^/api/jobs/([^/]+)/download$"), "api_job_download"),
        ("GET", re.compile(r"^/api/runs/([^/]+)/download$"), "api_run_download"),
    )

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- dispatch --------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET", head=True)

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str, head: bool = False) -> None:
        path = urlparse(self.path).path
        if self.server.api_only and method == "GET" and path == "/":
            path = "/api"
        handler = self.ROUTES.get((method, path))
        if handler:
            return getattr(self, handler)()
        for route_method, pattern, name in self.PATTERN_ROUTES:
            match = pattern.match(path)
            if route_method == method and match:
                return getattr(self, name)(unquote(match.group(1)), head)
        if method == "GET":
            self._serve_static(head)
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found.")

    # -- API ---------------------------------------------------------------------
    def api_help(self) -> None:
        self._send_json(API_HELP)

    def api_config(self) -> None:
        self._send_json(config_payload())

    def healthz(self) -> None:
        self._send_json({"ok": True, "k6": bool(find_k6()), "chrome": bool(find_chrome())})

    def api_status(self) -> None:
        job = self.server.manager.current
        self._send_json({"job": job.status() if job else None, "k6": bool(find_k6())})

    def api_run(self) -> None:
        body = self._read_json()
        if body is None:
            return
        try:
            config = RunConfig.from_dict(body)
            job = self.server.manager.start(config)
        except ValidationError as error:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(error), errors=error.errors)
        except BusyError as error:
            return self._send_error(HTTPStatus.CONFLICT, str(error))
        if body.get("wait") is True:
            while job.is_running:
                time.sleep(0.5)
            return self._send_json({"job": job.status(), "k6": True})
        self._send_json({"ok": True, "warnings": config.warnings()})

    def api_job(self, job_id: str, head: bool = False) -> None:
        result = self._job_result(job_id)
        if result is not None:
            self._send_json(result)

    def api_job_download(self, job_id: str, head: bool = False) -> None:
        result = self._job_result(job_id)
        if result is not None:
            self._send_download(export.job_zip(result, self.server.manager.reports_dir),
                                export.job_zip_name(result), head)

    def api_run_download(self, folder: str, head: bool = False) -> None:
        run_dir = export.find_run_dir(self.server.manager.reports_dir, folder)
        if run_dir is None:
            return self._send_error(HTTPStatus.NOT_FOUND, "Run not found.")
        self._send_download(export.run_zip(run_dir), export.run_zip_name(run_dir), head)

    def _job_result(self, job_id: str) -> dict | None:
        """Saved result of a finished run; the current one while it is still running is not downloadable."""
        result = export.find_job_result(self.server.manager.reports_dir, job_id)
        if result is None:
            current = self.server.manager.current
            message = ("This run is still running; download it when it has finished."
                       if current and current.id == job_id and current.is_running else "Run not found.")
            self._send_error(HTTPStatus.NOT_FOUND, message)
        return result

    def api_stop(self) -> None:
        self.server.manager.stop()
        self._send_json({"ok": True})

    # -- helpers -----------------------------------------------------------------
    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request is too large.")
                return None
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, json.JSONDecodeError):
            self._send_error(HTTPStatus.BAD_REQUEST, "Invalid request: expected a JSON object.")
            return None

    def _send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, status: HTTPStatus, message: str, **extra: Any) -> None:
        self._send_json({"error": message, **extra}, status)

    def _send_download(self, content: bytes, filename: str, head: bool) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(content)

    def _serve_static(self, head: bool) -> None:
        target = resolve_static(self.path, self.server.manager.reports_dir, self.server.api_only)
        if target is None:
            return self._send_error(HTTPStatus.NOT_FOUND, "Not found.")
        content = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        # The GUI is always revalidated, so app updates show up without a hard refresh.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head:
            self.wfile.write(content)


def create_server(port: int, reports_dir: Path, *, host: str = "127.0.0.1", api_only: bool = False,
                  manager: JobManager | None = None) -> PageLoadServer:
    return PageLoadServer((host, port), manager or JobManager(reports_dir), api_only=api_only)
