#!/usr/bin/env python3
"""Page Load Compare — GUI, HTTP API and command line for loadtest.js.

Each URL gets exactly the same number of visits by the same number of
parallel users (k6 "parallel" profile), so the results are comparable.

GUI (opens http://127.0.0.1:8000):
    python3 app.py
    python3 app.py --port 9000 --no-browser

API only (no GUI; drive it with curl or scripts):
    python3 app.py --api-only
    curl -X POST http://127.0.0.1:8000/api/run -H 'Content-Type: application/json' \
         -d '{"url": "https://hartak.am/", "compareUrl": "https://dev.hartak.am/",
              "vus": 20, "iterations": 40, "wait": true}'
    GET  /api            this help as JSON
    POST /api/run        start a run ("wait": true answers when it is finished)
    GET  /api/status     progress and results of the latest run
    POST /api/stop       stop the running run

Command line (no server, prints results and exits):
    python3 app.py run https://hartak.am/ https://dev.hartak.am/ --vus 20 --visits 40
    python3 app.py run https://hartak.am/ --json > result.json
    python3 app.py run https://hartak.am/ https://dev.hartak.am/ --api --vus 50 --visits 500
    Exit code: 0 all visits passed, 1 failed/hung visits, 2 bad input, 130 stopped.

Standard library only. Needs k6 (and Chrome) installed.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
import posixpath

ROOT = Path(__file__).resolve().parent
GUI_DIR = ROOT / "gui"
REPORTS_DIR = ROOT / "reports"

# Reuse the report generator's loader and percentile function.
_spec = importlib.util.spec_from_file_location("make_report", ROOT / "make-report.py")
make_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_report)

TIMINGS = make_report.TIMINGS  # browser timings: [(field, label, description), ...]
WAIT_MODES = ("load", "networkidle")
MODES = ("browser", "api")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
# Browser mode runs a real Chrome per user; API mode only sends HTTP requests.
LIMITS = {
    "browser": {"vus": (1, 20), "iterations": (1, 1000)},
    "api": {"vus": (1, 200), "iterations": (1, 10000)},
}


def find_k6():
    return shutil.which("k6") or next(
        (p for p in ("/opt/homebrew/bin/k6", "/usr/local/bin/k6") if Path(p).exists()), None
    )


def descendants(root_pid):
    """All child, grandchild, … process ids of root_pid."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    children = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, stack = [], [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


GRACEFUL_STOP_S = 30   # matches GRACEFUL_STOP in loadtest.js

# Settings the GUI can change (Advanced settings). Everything a run needs is
# set here or in the form, so no command-line flags are required.
DEFAULT_SETTINGS = {
    "timeoutSec": 30,          # per visit / request
    "timeLimitSec": 0,         # whole run per URL; 0 = automatic
    "expectStatus": "",        # "" = 200 (browser) / 2xx (API)
    "budgetP95Ms": 0,          # 0 = no performance budget
    "viewportWidth": 1440,     # browser only
    "viewportHeight": 900,
    "locale": "",              # browser only; "" = browser default
    "screenshots": True,       # browser only: screenshot failed visits
}
SETTING_RANGES = {
    "timeoutSec": (1, 300),
    "timeLimitSec": (0, 7200),
    "budgetP95Ms": (0, 600000),
    "viewportWidth": (320, 3840),
    "viewportHeight": (320, 2160),
}
SETTING_NAMES = {
    "timeoutSec": "Timeout", "timeLimitSec": "Time limit", "budgetP95Ms": "Budget",
    "viewportWidth": "Screen width", "viewportHeight": "Screen height",
}
STATUS_SPEC = re.compile(r"^\s*([1-5](\d\d|xx))(\s*,\s*[1-5](\d\d|xx))*\s*$", re.I)
LOCALE_SPEC = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def parse_settings(raw):
    """Validated settings from the GUI/API, with defaults filled in."""
    errors = []
    raw = raw if isinstance(raw, dict) else {}
    settings = dict(DEFAULT_SETTINGS)
    for key, (low, high) in SETTING_RANGES.items():
        if raw.get(key) in (None, ""):
            continue
        try:
            value = int(raw[key])
            if not low <= value <= high:
                raise ValueError
            settings[key] = value
        except (TypeError, ValueError):
            errors.append(f"{SETTING_NAMES[key]} must be a whole number from {low} to {high}.")
    if settings["timeLimitSec"] and settings["timeLimitSec"] < 10:
        errors.append("Time limit must be 0 (automatic) or at least 10 seconds.")
    status = str(raw.get("expectStatus") or "").strip()
    if status and not STATUS_SPEC.match(status):
        errors.append("Expected status must look like 200, 2xx or 200,301.")
    settings["expectStatus"] = status.replace(" ", "").lower()
    locale = str(raw.get("locale") or "").strip()
    if locale and not LOCALE_SPEC.match(locale):
        errors.append("Locale must look like hy-AM or en-US.")
    settings["locale"] = locale
    if "screenshots" in raw:
        settings["screenshots"] = raw.get("screenshots") is True
    return errors, settings


def visit_timeout_s():
    try:
        return max(1, int(os.environ.get("TIMEOUT_MS", "30000")) // 1000)
    except ValueError:
        return 30


def time_limit_s(vus, iterations, mode="browser", timeout_s=None, fixed_s=0):
    """Run time limit for one URL. Visits still running after it are
    reported as "did not finish" (hung) instead of blocking the run."""
    if fixed_s:
        return fixed_s
    override = os.environ.get("MAX_DURATION_S")
    if override and override.isdigit():
        return int(override)
    rounds = -(-iterations // vus)               # visits per user, rounded up
    # Page timeout + browser overhead, or request timeout + a little for API.
    per_visit = (timeout_s or visit_timeout_s()) + (30 if mode == "browser" else 5)
    return rounds * per_visit + 30


def summarize_error(line):
    """Short, readable reason for a failed run, from a k6 log line."""
    text = line.lower()
    if "connecting to browser" in text or "broken pipe" in text or "connection reset" in text:
        return "k6 could not connect to Chrome."
    if "timed out" in text or "deadline exceeded" in text:
        return "Pages timed out: the site or this computer may be overloaded."
    if "chrome not found" in text or "executable" in text:
        return "Chrome was not found."
    if line:
        return line.split(" at default (")[0][-200:]
    return "See the k6 output below for details."


def host_of(url):
    return urlparse(url).hostname or "site"


def stats_for(run_dir, mode="browser"):
    """Summarise one finished (or running) report folder."""
    visits, summary = make_report.load(run_dir)
    pct = make_report.percentile

    timings = {}
    for field, label, _ in make_report.timings_for(mode)[0]:
        values = [v[field] for v in visits if v.get(field) is not None]
        timings[field] = {
            "label": label,
            "med": pct(values, 50),
            "p90": pct(values, 90),
            "p95": pct(values, 95),
            "avg": sum(values) / len(values) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }

    requests = [v["requests"] for v in visits if v.get("requests") is not None]
    transfer = [v["transferBytes"] for v in visits if v.get("transferBytes") is not None]
    runtime = summary.get("totalRuntimeMs")
    passed = sum(1 for v in visits if v.get("passed"))

    return {
        "visits": len(visits),
        "passed": passed,
        "failed": len(visits) - passed,
        "runtimeMs": runtime,
        "visitsPerMin": (len(visits) / (runtime / 60000)) if runtime else None,
        "requestsMed": pct(requests, 50),
        "transferMed": pct(transfer, 50),
        "timings": timings,
        "errors": [
            {"visit": v.get("visit"), "error": v.get("error")}
            for v in visits if v.get("error")
        ][:20],
        "report": f"/{run_dir.relative_to(ROOT).as_posix()}/report.html"
        if (run_dir / "report.html").exists() else None,
    }


def diff(a, b):
    """How b did compared with a (lower is better). Same rules as the GUI."""
    if a is None or b is None or a == 0:
        return {"verdict": None, "ratio": None, "text": "—"}
    if a == b:
        return {"verdict": "same", "ratio": 1.0, "text": "the same"}
    ratio = b / a
    pct = lambda x: f"{max(1, round(x * 100))}%"
    if ratio > 1:
        text = f"{ratio:.1f}× slower" if ratio >= 2 else f"{pct(ratio - 1)} slower"
        return {"verdict": "slower", "ratio": ratio, "text": text}
    inv = a / b if b else float("inf")
    text = f"{inv:.1f}× faster" if b and inv >= 2 else f"{pct(1 - ratio)} faster"
    return {"verdict": "faster", "ratio": ratio, "text": text}


def compare(target_a, target_b, mode="browser"):
    """Comparison of two finished targets: rows, headline and result sentence."""
    a, b = target_a["stats"], target_b["stats"]
    host_a, host_b = host_of(target_a["url"]), host_of(target_b["url"])
    timings, _, main_label = make_report.timings_for(mode)
    action = "to respond" if mode == "api" else "to fully load"
    rows = []
    for field, label, _ in timings:
        x, y = a["timings"][field], b["timings"][field]
        rows.append({
            "metric": field, "label": label,
            "a": {"median": x["med"], "p95": x["p95"]},
            "b": {"median": y["med"], "p95": y["p95"]},
            "median": diff(x["med"], y["med"]),
            "p95": diff(x["p95"], y["p95"]),
        })
    rows.append({"metric": "runtimeMs", "label": "Run time",
                 "a": {"value": a["runtimeMs"]}, "b": {"value": b["runtimeMs"]},
                 "value": diff(a["runtimeMs"], b["runtimeMs"])})
    size = diff(a["transferMed"], b["transferMed"])
    size["text"] = size["text"].replace("slower", "larger").replace("faster", "smaller")
    rows.append({"metric": "transferMed", "label": "Transferred",
                 "a": {"value": a["transferMed"]}, "b": {"value": b["transferMed"]},
                 "value": size})

    counts = {"slower": 0, "faster": 0, "same": 0}
    for row in rows[:len(timings)]:
        if row["median"]["verdict"]:
            counts[row["median"]["verdict"]] += 1
    compared = sum(counts.values())
    if not compared:
        summary = "Not enough data to compare the timings."
    elif counts["slower"] == compared:
        summary = f"{host_b} was slower than {host_a} on all {compared} timings (median)."
    elif counts["faster"] == compared:
        summary = f"{host_b} was faster than {host_a} on all {compared} timings (median)."
    else:
        parts = [f"{word} on {counts[key]}" for key, word in
                 (("slower", "slower"), ("faster", "faster"), ("same", "the same")) if counts[key]]
        listed = ", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 1 else parts[0]
        summary = f"Compared with {host_a}, {host_b} was {listed} of {compared} timings (median)."

    full = rows[len(timings) - 1]["median"]
    if full["verdict"] == "same":
        headline = f"{host_b} is as fast as {host_a} {action}."
    elif full["verdict"]:
        headline = f"{host_b} is {full['text']} than {host_a} {action}."
    else:
        headline = f"{host_b} and {host_a} cannot be compared."
    return {"a": host_a, "b": host_b, "headline": headline, "summary": summary, "rows": rows}


class Job:
    """One run: one or two URLs, executed one after the other."""

    def __init__(self, urls, vus, iterations, wait_until, mode="browser",
                 method="GET", headers=None, body=None, settings=None):
        self.urls = urls
        self.vus = vus
        self.iterations = iterations
        self.wait_until = wait_until
        self.mode = mode
        self.method = method
        self.headers = headers or {}
        self.body = body
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.started = time.time()
        self.finished = None
        self.state = "running"  # running | done | stopped | error
        self.message = ""
        self.current = 0
        self.process = None
        self.stop_requested = False
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dirs = [REPORTS_DIR / f"{stamp}-{chr(65 + i)}-{host_of(u)}" for i, u in enumerate(urls)]
        self.log = []
        # Per URL: problem message when a run failed or finished early.
        self.problems = [None] * len(urls)

    def progress(self, index):
        console = self.dirs[index] / "console.log"
        if not console.exists():
            return 0
        try:
            return sum(1 for line in console.read_text(encoding="utf-8").splitlines()
                       if line.startswith("VISIT "))
        except OSError:
            return 0

    def run(self):
        k6 = find_k6()
        if not k6:
            self.state, self.message = "error", "k6 was not found. Install it with: brew install k6"
            self.finished = time.time()
            return

        env = os.environ.copy()
        env["PATH"] = f"{Path(k6).parent}:{env.get('PATH', '')}"
        env["K6"] = k6

        for index, url in enumerate(self.urls):
            if self.stop_requested:
                break
            self.current = index
            env["REPORT_OUT"] = str(self.dirs[index].relative_to(ROOT))
            cfg = self.settings
            limit = time_limit_s(self.vus, self.iterations, self.mode,
                                 cfg["timeoutSec"], cfg["timeLimitSec"])
            env["TIMEOUT_MS"] = str(cfg["timeoutSec"] * 1000)
            env["EXPECT_STATUS"] = cfg["expectStatus"] or ("2xx" if self.mode == "api" else "200")
            env["VIEWPORT_WIDTH"] = str(cfg["viewportWidth"])
            env["VIEWPORT_HEIGHT"] = str(cfg["viewportHeight"])
            env["SCREENSHOT_ON_FAIL"] = "1" if cfg["screenshots"] else "0"
            if cfg["locale"]:
                env["LOCALE"] = cfg["locale"]
            else:
                env.pop("LOCALE", None)
            if self.mode == "api":
                env["SCRIPT"] = "apitest.js"
                env["API_METHOD"] = self.method
                env["API_HEADERS"] = json.dumps(self.headers)
                if self.body:
                    env["API_BODY"] = self.body
                else:
                    env.pop("API_BODY", None)
            else:
                env["SCRIPT"] = "loadtest.js"
            cmd = [
                str(ROOT / "run-report.sh"),
                "-e", "PROFILE=parallel",
                "-e", f"BASE_URL={url}",
                "-e", f"VUS={self.vus}",
                "-e", f"ITERATIONS={self.iterations}",
                "-e", f"WAIT_UNTIL={self.wait_until}",
                "-e", f"MAX_DURATION={limit}s",
                "-e", f"GRACEFUL_STOP={GRACEFUL_STOP_S}s",
            ]
            target_started = time.time()
            # New session, so Stop can end k6 and the browsers it started.
            self.process = subprocess.Popen(
                cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )
            target_errors = []
            # Last resort: if k6 itself stops responding well past its own
            # limit, end it so the app never waits forever.
            watchdog_fired = threading.Event()
            watchdog = threading.Timer(
                limit + GRACEFUL_STOP_S + 60,
                lambda: (watchdog_fired.set(), self._kill_process_tree()),
            )
            watchdog.daemon = True
            watchdog.start()
            for line in self.process.stdout:
                line = line.rstrip()
                if line:
                    self.log.append(line)
                    del self.log[:-200]
                    if re.search(r"error|failed|timed out", line, re.I) and not line.startswith(("Visits:", "thresholds on")):
                        target_errors.append(line)
            self.process.wait()
            watchdog.cancel()

            # A run can end without all visits (k6 could not start the
            # browser, the time limit was hit, ...). Never report that as done.
            recorded = self.progress(index)
            if recorded < self.iterations and not self.stop_requested:
                missing = self.iterations - recorded
                elapsed = time.time() - target_started
                connect_failed = any(
                    re.search(r"connecting to browser|broken pipe|connection reset", e, re.I)
                    for e in target_errors
                )
                if watchdog_fired.is_set():
                    self.problems[index] = (
                        f"k6 stopped responding and was ended after {round(elapsed)} s. "
                        f"{missing} of {self.iterations} visits did not finish."
                    )
                elif connect_failed and recorded == 0:
                    self.problems[index] = (
                        f"No visits ran: {summarize_error(target_errors[-1])} "
                        f"All {self.iterations} visits are missing."
                    )
                elif elapsed >= limit:
                    self.problems[index] = (
                        f"{missing} of {self.iterations} visits hung and did not finish within "
                        f"the {limit} s time limit. They were stopped and are not in the results."
                    )
                else:
                    reason = summarize_error(target_errors[-1] if target_errors else "")
                    self.problems[index] = f"{missing} of {self.iterations} visits did not finish. {reason}"

        self.finished = time.time()
        if self.stop_requested:
            self.state, self.message = "stopped", "Stopped."
        elif any(self.problems):
            self.state, self.message = "problem", "The run finished with problems: see the URL marked in red."
        else:
            self.state = "done"

    def stop(self):
        self.stop_requested = True
        self._kill_process_tree()

    def _kill_process_tree(self):
        if self.process and self.process.poll() is None:
            # Chrome puts itself in its own process group, so killing k6's
            # group is not enough: end every process started under this run.
            for pid in [self.process.pid, *descendants(self.process.pid)]:
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass

    def status(self):
        targets = []
        for index, url in enumerate(self.urls):
            done = self.progress(index)
            finished = (self.dirs[index] / "report.html").exists()
            if finished and self.problems[index]:
                phase = "failed" if done == 0 else "incomplete"
            elif finished:
                phase = "done"
            elif index == self.current and self.state == "running":
                phase = "running"
            elif self.state == "running":
                phase = "waiting"
            else:
                phase = self.state
            targets.append({
                "url": url,
                "phase": phase,
                "done": done,
                "total": self.iterations,
                "problem": self.problems[index],
                "stats": stats_for(self.dirs[index], self.mode) if finished and done > 0 else None,
            })
        main = make_report.timings_for(self.mode)[1]
        budget = self.settings["budgetP95Ms"]
        for t in targets:
            t["budget"] = None
            if budget and t["stats"]:
                p95 = t["stats"]["timings"][main]["p95"]
                t["budget"] = {"limitMs": budget, "p95Ms": p95,
                               "passed": p95 is not None and p95 <= budget}

        comparison = None
        if len(targets) == 2 and all(t["stats"] for t in targets):
            comparison = compare(targets[0], targets[1], self.mode)
        all_passed = self.state == "done" and all(
            t["stats"] and t["stats"]["passed"] == self.iterations
            and (t["budget"] is None or t["budget"]["passed"])
            for t in targets
        )
        return {
            "state": self.state,
            "ok": all_passed,
            "message": self.message,
            "comparison": comparison,
            "mode": self.mode,
            "settings": self.settings,
            "method": self.method if self.mode == "api" else None,
            "timings": [[f, label] for f, label, _ in make_report.timings_for(self.mode)[0]],
            "mainMetric": make_report.timings_for(self.mode)[1],
            "mainLabel": make_report.timings_for(self.mode)[2],
            "vus": self.vus,
            "iterations": self.iterations,
            "waitUntil": self.wait_until,
            "elapsedMs": ((self.finished or time.time()) - self.started) * 1000,
            "targets": targets,
            "log": [line for line in self.log if not line.startswith("VISIT ")][-40:],
        }


JOB = None
JOB_LOCK = threading.Lock()


def parse_run_request(body):
    errors = []
    urls = []
    mode = "api" if body.get("apiOnly") is True or body.get("mode") == "api" else "browser"
    for key in ("url", "compareUrl"):
        value = str(body.get(key) or "").strip()
        if not value:
            if key == "url":
                errors.append("URL is required.")
            continue
        if not re.match(r"^https?://[^\s/]+", value, re.I):
            errors.append(f"{value!r} is not an http(s) URL.")
        urls.append(value)

    numbers = {}
    for key, (low, high) in LIMITS[mode].items():
        try:
            numbers[key] = int(body.get(key))
            if not low <= numbers[key] <= high:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{'VUs' if key == 'vus' else 'Total visits'} must be a whole number from {low} to {high}.")

    if not errors and numbers.get("vus", 0) > numbers.get("iterations", 0):
        errors.append(
            f"Parallel users ({numbers['vus']}) can't be more than total visits "
            f"({numbers['iterations']}): each user needs at least one visit."
        )

    wait_until = body.get("waitUntil") or "load"
    if wait_until not in WAIT_MODES:
        errors.append("Wait until must be load or networkidle.")

    # API mode extras: method, headers (JSON object or "Name: value" lines), body.
    api = {"mode": mode, "method": "GET", "headers": {}, "body": None}
    if mode == "api":
        method = str(body.get("method") or "GET").upper()
        if method not in HTTP_METHODS:
            errors.append(f"Method must be one of {', '.join(HTTP_METHODS)}.")
        api["method"] = method
        headers = body.get("headers") or {}
        if isinstance(headers, str):
            parsed = {}
            text = headers.strip()
            if text.startswith("{"):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    errors.append("Headers must be a JSON object or lines like 'Name: value'.")
            else:
                for line in text.splitlines():
                    if not line.strip():
                        continue
                    if ":" not in line:
                        errors.append(f"Header line {line.strip()!r} needs 'Name: value'.")
                        continue
                    name, value = line.split(":", 1)
                    parsed[name.strip()] = value.strip()
            headers = parsed
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            errors.append("Headers must be name/value text pairs.")
            headers = {}
        api["headers"] = headers
        request_body = body.get("body")
        api["body"] = request_body if isinstance(request_body, str) and request_body != "" else None

    setting_errors, settings = parse_settings(body.get("settings"))
    errors.extend(setting_errors)
    api["settings"] = settings
    return errors, urls, numbers, wait_until, api


PUBLIC_PREFIXES = ("/gui/", "/reports/", "/screenshots/")
API_ONLY = False   # set by --api-only: no GUI, only /api/* and report files

API_HELP = {
    "name": "Page Load Compare API",
    "endpoints": {
        "POST /api/run": {
            "body": {
                "url": "https://hartak.am/  (required)",
                "compareUrl": "https://dev.hartak.am/  (optional)",
                "vus": "parallel users: 1-20 browser, 1-200 API",
                "iterations": "visits/requests per URL: 1-1000 browser, 1-10000 API, >= vus",
                "waitUntil": "load (default) or networkidle  (browser mode)",
                "apiOnly": "true = HTTP/API test: plain requests, no browser",
                "method": "GET (default), POST, PUT, PATCH, DELETE, HEAD, OPTIONS  (API mode)",
                "headers": "{\"Name\": \"value\"} or \"Name: value\" lines  (API mode)",
                "body": "request body text  (API mode)",
                "settings": {
                    "timeoutSec": "per visit/request, 1-300 (default 30)",
                    "timeLimitSec": "per URL, 0 = automatic (default) or 10-7200",
                    "expectStatus": "200, 2xx or 200,301 (default 200 browser, 2xx API)",
                    "budgetP95Ms": "fail the run if p95 is above this, 0 = off",
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
        "POST /api/stop": "stop the running run",
    },
    "states": "running | done | problem (failed or hung visits) | stopped | error",
}


def public_path(raw_path):
    """Resolved request path if it may be served, else None.

    Resolves "..", "." and %-escapes first, so /reports/../app.py is refused.
    """
    path = posixpath.normpath(unquote(urlparse(raw_path).path))
    if API_ONLY and (path in ("/", "/index.html") or path.startswith("/gui/")):
        return None
    if path in ("/", "/index.html"):
        return "/gui/index.html"
    if path.startswith(PUBLIC_PREFIXES) and "\\" not in path:
        return path
    return None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def end_headers(self):
        # Always serve the latest GUI after app updates.
        if self.path.startswith("/gui/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, data, status=HTTPStatus.OK):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            with JOB_LOCK:
                job = JOB
            return self.send_json({"job": job.status() if job else None, "k6": bool(find_k6())})
        if path in ("/api", "/api/") or (API_ONLY and path == "/"):
            return self.send_json(API_HELP)
        # Only the GUI and report files are served, nothing else from the project.
        safe = public_path(self.path)
        if safe:
            self.path = safe
            return super().do_GET()
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self):
        # Same rules as GET: only the GUI and report files.
        safe = public_path(self.path)
        if safe:
            self.path = safe
            return super().do_HEAD()
        self.send_error(HTTPStatus.NOT_FOUND)

    def list_directory(self, path):
        # Never show folder listings.
        self.send_error(HTTPStatus.NOT_FOUND)
        return None

    def do_POST(self):
        global JOB
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "Invalid request."}, HTTPStatus.BAD_REQUEST)

        if path == "/api/run":
            errors, urls, numbers, wait_until, api = parse_run_request(body)
            if errors:
                return self.send_json({"error": " ".join(errors)}, HTTPStatus.BAD_REQUEST)
            with JOB_LOCK:
                if JOB and JOB.state == "running":
                    return self.send_json({"error": "A run is already in progress."}, HTTPStatus.CONFLICT)
                JOB = Job(urls, numbers["vus"], numbers["iterations"], wait_until,
                          api["mode"], api["method"], api["headers"], api["body"],
                          api["settings"])
                job = JOB
                threading.Thread(target=job.run, daemon=True).start()
            if body.get("wait") is True:
                # Answer only when the run is finished (for scripts and CI).
                while job.state == "running":
                    time.sleep(1)
                return self.send_json({"job": job.status(), "k6": True})
            return self.send_json({"ok": True})

        if path == "/api/stop":
            with JOB_LOCK:
                if JOB and JOB.state == "running":
                    JOB.stop()
            return self.send_json({"ok": True})

        self.send_error(HTTPStatus.NOT_FOUND)


def fmt_ms(value):
    if value is None:
        return "—"
    return f"{value / 1000:.2f} s" if value >= 10000 else f"{round(value)} ms"


def print_result(status):
    """Plain-text result for the command line."""
    out = []
    for t in status["targets"]:
        host = host_of(t["url"])
        s = t["stats"]
        if s:
            full = s["timings"][status["mainMetric"]]
            out.append(
                f"{host:<24} {s['passed']}/{t['total']} passed   {status['mainLabel'].lower()} median {fmt_ms(full['med'])}"
                f"   p95 {fmt_ms(full['p95'])}   run time {round((s['runtimeMs'] or 0) / 1000)} s"
            )
        else:
            out.append(f"{host:<24} no results")
        if t["problem"]:
            out.append(f"{'':<24} ⚠ {t['problem']}")

    cmp = status.get("comparison")
    if cmp:
        out.append("")
        out.append(f"{'Timing':<16}{cmp['a'] + ' median':>26}{cmp['b'] + ' median':>26}   {cmp['b']} is…")
        for row in cmp["rows"]:
            if "median" in row:
                out.append(f"{row['label']:<16}{fmt_ms(row['a']['median']):>26}{fmt_ms(row['b']['median']):>26}   {row['median']['text']}")
        out.append("")
        out.append(cmp["headline"])
        out.append(f"Result: {cmp['summary']}")
    elif len(status["targets"]) == 1 and status["targets"][0]["stats"]:
        t = status["targets"][0]["stats"]["timings"]
        out.append("")
        out.append(f"{'Timing':<16}{'median':>10}{'p90':>10}{'p95':>10}{'min':>10}{'max':>10}")
        for field, label in status["timings"]:
            x = t[field]
            out.append(f"{label:<16}" + "".join(f"{fmt_ms(x[k]):>10}" for k in ("med", "p90", "p95", "min", "max")))

    reports = [t["stats"]["report"] for t in status["targets"] if t["stats"] and t["stats"]["report"]]
    if reports:
        out.append("")
        out.extend(f"Report: {ROOT / r.lstrip('/')}" for r in reports)
    print("\n".join(out))


def run_cli(argv):
    parser = argparse.ArgumentParser(
        prog="app.py run",
        description="Run a page load test (one URL, or two to compare) without the GUI.",
    )
    parser.add_argument("url")
    parser.add_argument("compare_url", nargs="?")
    parser.add_argument("--vus", type=int, default=5, help="parallel users (default 5)")
    parser.add_argument("--visits", type=int, default=30, help="visits per URL (default 30)")
    parser.add_argument("--wait-until", choices=WAIT_MODES, default="load")
    parser.add_argument("--api", action="store_true",
                        help="API test: plain HTTP requests, no browser")
    parser.add_argument("--method", default="GET", help="HTTP method for --api (default GET)")
    parser.add_argument("--header", action="append", default=[], metavar="'Name: value'",
                        help="request header for --api (repeatable)")
    parser.add_argument("--body", help="request body for --api")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args(argv)

    errors, urls, numbers, wait_until, api = parse_run_request({
        "url": args.url, "compareUrl": args.compare_url,
        "vus": args.vus, "iterations": args.visits, "waitUntil": args.wait_until,
        "apiOnly": args.api, "method": args.method,
        "headers": "\n".join(args.header), "body": args.body,
    })
    if errors:
        print("Error: " + " ".join(errors), file=sys.stderr)
        return 2

    REPORTS_DIR.mkdir(exist_ok=True)
    job = Job(urls, numbers["vus"], numbers["iterations"], wait_until,
              api["mode"], api["method"], api["headers"], api["body"],
              api["settings"])
    worker = threading.Thread(target=job.run, daemon=True)
    worker.start()

    # Progress goes to stderr, so --json output on stdout stays clean.
    last = None
    try:
        while worker.is_alive():
            line = "  ".join(f"{host_of(u)} {job.progress(i)}/{job.iterations}" for i, u in enumerate(urls))
            if line != last:
                print(f"[{round(time.time() - job.started):>4}s] {line}", file=sys.stderr, flush=True)
                last = line
            worker.join(2)
    except KeyboardInterrupt:
        print("\nStopping…", file=sys.stderr)
        job.stop()
        worker.join(30)
        return 130

    status = job.status()
    if status["state"] == "error":
        print(f"Error: {status['message']}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(status, indent=2, default=str))
    else:
        print()
        print_result(status)
    return 0 if status["ok"] else 1


def main():
    global API_ONLY
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        sys.exit(run_cli(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        description="Page Load Compare: GUI and HTTP API. Use 'app.py run --help' for the command line.",
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="don't open the GUI in a browser")
    parser.add_argument("--api-only", action="store_true", help="serve only the HTTP API, no GUI")
    args = parser.parse_args()
    API_ONLY = args.api_only

    REPORTS_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    if API_ONLY:
        print(f"Page Load Compare API running at {url}api  (Ctrl+C to quit)")
    else:
        print(f"Page Load Compare running at {url}  (Ctrl+C to quit)")
        if not args.no_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with JOB_LOCK:
            if JOB and JOB.state == "running":
                JOB.stop()
        server.server_close()


if __name__ == "__main__":
    main()
