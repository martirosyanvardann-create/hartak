"""Possible reasons for failed or slow visits: a rule-based diagnosis of one URL's run.

Pure functions over the visit records (no files, no processes). The tool only
sees what a client sees, so every finding is a *possible* reason, backed by the
evidence found in the run, plus what to check on the server to confirm it.

    diagnose(visits, summary, mode=..., vus=..., expected=..., timeout_ms=..., other_lines=...)

returns a dict with:
    headline    one sentence: the most important finding (or "No problems found.")
    findings    ranked list: level, side, title, detail, evidence, reasons, checks
    categories  failures grouped by kind (timeout, 502, reset, 404, ...)
    timeline    passed / failed per time bucket, to see *when* things went wrong
    resources   (browser mode) page resources that failed or were slowest
    answers     what the server answered on failures (Server header, body excerpt)
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable
from urllib.parse import urlsplit

from .analysis import percentile
from .formatting import size
from .modes import TestMode

LEVELS = ("critical", "warning", "info")


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    meaning: str


CATEGORIES = {c.key: c for c in (
    Category("timeout", "Timed out", "No complete answer within the timeout."),
    Category("bad_gateway", "502 Bad Gateway", "The proxy in front of the app got no valid answer from the app."),
    Category("unavailable", "503 Service Unavailable", "The server refused the request: overloaded, in maintenance or no healthy app instance."),
    Category("gateway_timeout", "504 Gateway Timeout", "The proxy gave up waiting for the app."),
    Category("server_error", "500 / other 5xx", "The application failed while handling the request."),
    Category("rate_limited", "429 Too Many Requests", "A rate limit (app, proxy or firewall) rejected the request."),
    Category("auth", "401 / 403", "Access denied: a login or token is required, or a firewall blocked the request."),
    Category("not_found", "404 Not Found", "The URL does not exist on the server."),
    Category("client_error", "Other 4xx", "The server rejected the request (method, headers or body)."),
    Category("wrong_status", "Unexpected status", "The status code did not match the expected status."),
    Category("reset", "Connection reset", "The connection was closed in the middle of the request."),
    Category("refused", "Connection refused", "Nothing accepted the connection: the server or its port was down."),
    Category("dns", "DNS error", "The domain name could not be resolved."),
    Category("tls", "TLS / certificate error", "The HTTPS connection could not be set up."),
    Category("not_loaded", "Page did not finish", "The HTML arrived but the browser never finished loading the page."),
    Category("browser", "Test browser error", "The test browser itself failed: this is about the test computer, not the site."),
    Category("other", "Other error", "See the error text."),
)}

_STATUS_RE = re.compile(r"http status (\d{3})")
_BROWSER_ERRORS = ("cannot find context", "target closed", "session closed", "connecting to browser",
                   "browser has been closed", "websocket", "browser context")


# -- classification -------------------------------------------------------------------
def visit_status(visit: dict[str, Any]) -> int | None:
    match = _STATUS_RE.search((visit.get("error") or "").lower())
    if match:
        return int(match.group(1))
    status = visit.get("httpStatus")
    return status if isinstance(status, int) and status > 0 else None


def classify(visit: dict[str, Any]) -> str | None:
    """Kind of failure of one visit (a key of CATEGORIES), or None if it passed."""
    if visit.get("passed"):
        return None
    text = (visit.get("error") or "").lower()
    status = visit_status(visit) if "http status" in text else None
    if status:
        return _status_category(status)
    if "timed out" in text or "timeout" in text or "deadline exceeded" in text:
        return "timeout"
    if any(s in text for s in _BROWSER_ERRORS):
        return "browser"
    if "name_not_resolved" in text or "no such host" in text or "lookup " in text:
        return "dns"
    if "refused" in text:
        return "refused"
    if "reset" in text or "connection_closed" in text or "empty_response" in text or " eof" in text:
        return "reset"
    if "cert" in text or "ssl" in text or "tls" in text or "x509" in text:
        return "tls"
    if "did not report a finished load" in text:
        return "not_loaded"
    status = visit_status(visit)
    return _status_category(status) if status and status >= 400 else "other"


def _status_category(status: int) -> str:
    return {502: "bad_gateway", 503: "unavailable", 504: "gateway_timeout", 429: "rate_limited",
            401: "auth", 403: "auth", 404: "not_found"}.get(status) or (
        "server_error" if status >= 500 else "client_error" if status >= 400 else "wrong_status")


# -- time helpers ----------------------------------------------------------------------
def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class _Clock:
    """Seconds since the first visit started, and the matching local clock time."""

    def __init__(self, visits: list[dict[str, Any]]):
        times = [t for t in (_parse_time(v.get("startedAt")) for v in visits) if t]
        self.t0 = min(times) if times else None

    def offset(self, visit: dict[str, Any]) -> float:
        started = _parse_time(visit.get("startedAt"))
        return (started - self.t0).total_seconds() if started and self.t0 else 0.0

    def at(self, seconds: float) -> str:
        """'1:53 (14:54:33)': offset since start and local clock time."""
        offset = f"{int(seconds // 60)}:{int(seconds % 60):02d}"
        if not self.t0:
            return offset
        return f"{offset} ({(self.t0 + timedelta(seconds=seconds)).astimezone().strftime('%H:%M:%S')})"


def _bucket_size(span_s: float) -> int:
    for step in (1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 1800):
        if span_s / step <= 24:
            return step
    return 3600


# -- main entry -------------------------------------------------------------------------
def diagnose(visits: list[dict[str, Any]], summary: dict[str, Any] | None = None, *,
             mode: TestMode, vus: int | None = None, expected: int | None = None,
             timeout_ms: float | None = None, other_lines: Iterable[str] = ()) -> dict[str, Any]:
    summary = summary or {}
    visits = list(visits)
    ctx = _Context(visits, summary, mode, vus, expected, timeout_ms, list(other_lines))
    findings = ctx.findings()
    findings.sort(key=lambda f: LEVELS.index(f["level"]))
    serious = [f for f in findings if f["level"] != "info"]
    headline = serious[0]["title"] if serious else (
        "No problems found." if visits else f"No {mode.noun} were recorded.")
    return {
        "headline": headline,
        "findings": findings,
        "categories": ctx.categories(),
        "timeline": ctx.timeline(),
        "resources": ctx.resources(),
        "answers": ctx.answers(),
        "notes": ctx.notes(),
    }


class _Context:
    def __init__(self, visits, summary, mode, vus, expected, timeout_ms, other_lines):
        self.visits = sorted(visits, key=lambda v: v.get("startedAt") or "")
        self.summary = summary
        self.mode = mode
        self.noun, self.one = mode.noun, mode.noun_singular
        self.browser = mode.name != "api"
        self.vus = vus or summary.get("vus") or len({v.get("vu") for v in visits}) or 1
        self.expected = expected or summary.get("expectedVisits") or len(visits)
        self.timeout_s = round((timeout_ms or summary.get("timeoutMs") or 30000) / 1000)
        self.other_lines = other_lines
        self.clock = _Clock(self.visits)
        self.kind = {id(v): classify(v) for v in self.visits}
        self.failed = [v for v in self.visits if not v.get("passed")]
        self.passed = [v for v in self.visits if v.get("passed")]
        self.by_kind: dict[str, list[dict]] = defaultdict(list)
        for v in self.failed:
            self.by_kind[self.kind[id(v)]].append(v)

    # -- small helpers ------------------------------------------------------------------
    def total(self) -> int:
        return len(self.visits)

    def span(self, items: list[dict]) -> tuple[float, float]:
        offsets = [self.clock.offset(v) for v in items]
        return (min(offsets), max(offsets)) if offsets else (0.0, 0.0)

    def when(self, items: list[dict]) -> str:
        first, last = self.span(items)
        if last - first < 1:
            return f"at {self.clock.at(first)}"
        return f"between {self.clock.at(first)} and {self.clock.at(last)}"

    def users_hit(self, items: list[dict]) -> str:
        users = len({v.get("vu") for v in items})
        return f"spread across {users} of {self.vus} parallel users" if users > 1 else "from a single user"

    def ttfb(self, items: list[dict]) -> list[float]:
        return [v["ttfbMs"] for v in items if isinstance(v.get("ttfbMs"), (int, float))]

    def server_header(self, items: list[dict]) -> str | None:
        servers = Counter((v.get("responseHeaders") or {}).get("server") for v in items)
        servers.pop(None, None)
        return servers.most_common(1)[0][0] if servers else None

    @staticmethod
    def finding(level, side, title, detail, *, evidence=(), reasons=(), checks=()) -> dict[str, Any]:
        return {"level": level, "side": side, "title": title, "detail": detail,
                "evidence": [e for e in evidence if e], "reasons": list(reasons), "checks": list(checks)}

    # -- findings -------------------------------------------------------------------------
    def findings(self) -> list[dict[str, Any]]:
        if not self.visits:
            return []
        rules = (self._outage, self._config_errors, self._gateway_and_server_errors, self._timeouts,
                 self._slow_start, self._network_errors, self._test_browser, self._missing,
                 self._load_growth, self._queued_together, self._where_time_goes, self._resources_failing,
                 self._heavy_page)
        found: list[dict[str, Any]] = []
        for rule in rules:
            found.extend(rule())
        return found

    def _config_errors(self) -> list[dict]:
        """The same 4xx on (almost) every visit: a wrong URL or missing access, not load."""
        out = []
        total = self.total()
        for key in ("not_found", "auth", "client_error", "rate_limited", "wrong_status"):
            items = self.by_kind.get(key, [])
            if not items:
                continue
            share = len(items) / total
            statuses = Counter(visit_status(v) for v in items)
            status_text = ", ".join(f"{s} ×{n}" for s, n in statuses.most_common() if s)
            first_failed = self.visits[0] in items
            speed = percentile([v.get("durationMs") for v in items if v.get("durationMs")], 50)
            fast = f" The server answered quickly (median {round(speed)} ms), so it is up." if speed and speed < 3000 else ""
            evidence = [f"{len(items)} of {total} {self.noun}: {status_text}",
                        "from the very first one" if first_failed else f"{self.when(items)}",
                        *self._answer_evidence(items)]
            level = "critical" if share >= 0.5 else "warning"
            if key == "not_found":
                out.append(self.finding(
                    level, "URL / configuration", "The URL does not exist on the server (404 Not Found)",
                    f"{len(items)} of {total} {self.noun} got 404.{fast} This is not a load problem: "
                    "the server has nothing at this address.", evidence=evidence,
                    reasons=["A typo or wrong path in the URL",
                             "The endpoint is under another path or prefix (e.g. /api/v1/…), or on another host",
                             "The endpoint expects another HTTP method (e.g. POST instead of GET)",
                             "The route exists on another environment only (e.g. prod but not dev)"],
                    checks=["Open the URL once in a browser or with curl",
                            "Check the exact path in the API documentation (Swagger/OpenAPI) or ask the backend team"]))
            elif key == "auth":
                out.append(self.finding(
                    level, "URL / configuration", "Access was denied (401 / 403)",
                    f"{len(items)} of {total} {self.noun} were refused.{fast}", evidence=evidence,
                    reasons=["The endpoint needs a login, cookie or API token (add it under Headers in API-only mode)",
                             "The token in the headers is wrong or expired",
                             "A firewall / bot protection (WAF, Cloudflare) blocks automated or headless traffic, "
                             "or this computer's IP address"],
                    checks=["Try the same request with a valid token",
                            "Ask whether the test IP needs to be allowed in the firewall"]))
            elif key == "rate_limited":
                retry = self._header_values(items, "retry-after")
                out.append(self.finding(
                    level, "server", "Requests were rate-limited (429 Too Many Requests)",
                    f"{len(items)} of {total} {self.noun} were rejected by a rate limit {self.when(items)}. "
                    "All test traffic comes from one IP address, which makes rate limits trigger early.",
                    evidence=evidence + ([f"Retry-After: {retry}"] if retry else []),
                    reasons=["A rate limit in the app, the proxy (e.g. nginx limit_req) or a CDN/firewall"],
                    checks=["Ask for the test IP to be excluded from the rate limit, or test with fewer users"]))
            else:
                out.append(self.finding(
                    level, "URL / configuration", f"The server rejected the request ({status_text or 'wrong status'})",
                    f"{len(items)} of {total} {self.noun} did not get the expected status.{fast}", evidence=evidence,
                    reasons=["405: the endpoint expects another HTTP method",
                             "400 / 415 / 422: the body or Content-Type header is not what the API expects",
                             "3xx: the URL redirects (expect 200,301 or test the final URL)"],
                    checks=["Compare the request (method, headers, body) with a working one, e.g. from the "
                            "browser's developer tools (Network tab)"]))
        return out

    def _gateway_and_server_errors(self) -> list[dict]:
        out = []
        total = self.total()
        texts = {
            "bad_gateway": (
                "The app behind the proxy stopped answering (502 Bad Gateway)",
                "A 502 is sent by the proxy / load balancer (e.g. nginx) when the application behind it "
                "does not give a valid answer.",
                ["The application process crashed or was restarted, often because it ran out of memory under load",
                 "All app workers were busy, so the proxy could not get a connection",
                 "The app closed connections that the proxy was still using (keep-alive timeout mismatch)",
                 "A deploy or container restart happened during the test"],
                ["App / container restarts and exit codes in this time window "
                 "(docker ps, kubectl get pods → RESTARTS, OOMKilled)",
                 "Proxy error log, e.g. nginx: \"upstream prematurely closed connection\", "
                 "\"connect() failed (111: Connection refused) while connecting to upstream\"",
                 "Memory and CPU of the app during the test"]),
            "unavailable": (
                "The server refused requests (503 Service Unavailable)",
                "A 503 means the server or load balancer chose not to handle the request.",
                ["Overload protection: too many requests at once (queue or connection limit reached)",
                 "The load balancer had no healthy app instance (health checks failed under load)",
                 "Maintenance mode, or the app was still starting"],
                ["Load balancer / proxy logs and health-check status in this time window",
                 "Connection and worker limits of the app and proxy"]),
            "gateway_timeout": (
                "The app was too slow for the proxy (504 Gateway Timeout)",
                "A 504 means the proxy waited for the app longer than its own timeout and gave up.",
                ["Slow database queries or slow backend API calls under load",
                 "Requests queued because all app workers were busy",
                 "The proxy timeout (e.g. nginx proxy_read_timeout) is shorter than the slowest pages"],
                ["Slow-query log of the database and traces (APM) of the slowest requests",
                 "Proxy access log: upstream response times in this time window"]),
            "server_error": (
                "The application failed with server errors (5xx)",
                "The app itself returned an error.",
                ["A bug in the app (an exception for this page or request)",
                 "Errors that appear only under load: database connection pool exhausted, too many open files, "
                 "limits of an external API",
                 "A dependency (database, cache, backend service) was down"],
                ["Application error log / stack traces in this time window"]),
        }
        for key, (title, intro, reasons, checks) in texts.items():
            items = self.by_kind.get(key, [])
            if not items:
                continue
            server = self.server_header(items)
            out.append(self.finding(
                "critical" if len(items) / total >= 0.05 or key == "bad_gateway" else "warning", "server",
                title, f"{intro} {len(items)} of {total} {self.noun} got it, {self.when(items)}.",
                evidence=[self.users_hit(items), f"Answered by: {server}" if server else None,
                          *self._answer_evidence(items)],
                reasons=reasons, checks=checks))
        return out

    def _timeouts(self) -> list[dict]:
        items = self.by_kind.get("timeout", [])
        if not items:
            return []
        total = self.total()
        has_doc_info = any("documentStatus" in v for v in items)
        no_answer = [v for v in items if v.get("documentStatus") is None]
        html_arrived = [v for v in items if v.get("documentStatus") is not None] if has_doc_info else []
        level = "critical" if len(items) / total >= 0.1 else "warning"
        out = []
        if no_answer:
            known = has_doc_info or not self.browser
            what = "not even the first byte of the answer" if known else "no complete page"
            out.append(self.finding(
                level, "server", f"The server did not answer within the {self.timeout_s} s timeout",
                f"{len(no_answer)} of {total} {self.noun} got {what} within {self.timeout_s} s, "
                f"{self.when(no_answer)}. The connection was open but the server kept the request waiting.",
                evidence=[self.users_hit(no_answer), self._passed_ttfb_evidence()],
                reasons=["All app workers / threads were busy, so new requests waited in a queue until the timeout",
                         "The app's event loop was blocked by heavy work, e.g. server-side rendering of a large "
                         "page in Node / Nuxt",
                         "The app was waiting on a slow dependency: database, backend API or cache. Calls the "
                         "server makes while building the page are invisible to the browser",
                         "The database connection pool was exhausted, so requests waited for a free connection",
                         "The app hung or was restarting (look for 502s and an outage in the timeline)"],
                checks=["Server CPU and memory during the test",
                        "Proxy access log for this time window: request and upstream response times "
                        "(nginx $request_time / $upstream_response_time)",
                        "Traces / slow logs of the backend and database calls the page makes on the server",
                        "How many app workers or instances run, and their limits"]))
        if html_arrived:
            resources = self.resources()
            top = [f"{r['url']} — {r['problem']}" for r in resources["failing"][:3]]
            out.append(self.finding(
                level, "page", "The HTML arrived, but the page never finished loading",
                f"{len(html_arrived)} of {total} {self.noun} received the HTML (status "
                f"{Counter(v['documentStatus'] for v in html_arrived).most_common(1)[0][0]}) but did not reach "
                f"the load state within {self.timeout_s} s: a resource of the page kept it waiting.",
                evidence=top,
                reasons=["A script, font, image or API call made by the page hung or was very slow",
                         "A third-party script (analytics, chat, ads) did not answer",
                         "With \"networkidle\": the page keeps polling or holds a connection open"],
                checks=["The failing / slowest resources listed in this report",
                        "Run with Wait until = load to see if only late requests are the problem"]))
        return out

    def _outage(self) -> list[dict]:
        """A block of failures at the end of the run: the site went down and did not come back."""
        if not self.failed or not self.passed:
            return []
        last_pass = max(self.clock.offset(v) for v in self.passed)
        tail = [v for v in self.failed if self.clock.offset(v) > last_pass]
        _, end = self.span(self.visits)
        if len(tail) < max(5, 0.2 * self.total()) or end - last_pass < 10:
            return []
        kinds = Counter(self.kind[id(v)] for v in tail)
        breakdown = ", ".join(f"{CATEGORIES[k].label.lower()} ×{n}" for k, n in kinds.most_common())
        before = [v for v in self.failed if last_pass - 60 <= self.clock.offset(v) <= last_pass + 5
                  and self.kind[id(v)] in ("bad_gateway", "unavailable", "reset", "refused", "server_error")]
        crash = ""
        if before:
            kinds_before = Counter(CATEGORIES[self.kind[id(v)]].label for v in before)
            crash = (f" Just before that, {self.when(before)}, there were "
                     f"{', '.join(f'{n}× {k}' for k, n in kinds_before.items())}: a typical sign that the app "
                     "crashed or restarted and did not come back, or came back and was overloaded again at once.")
        return [self.finding(
            "critical", "server",
            f"The site stopped answering at about {self.clock.at(last_pass)} and did not recover during the test",
            f"After the last successful {self.one} at {self.clock.at(last_pass)}, all {len(tail)} remaining "
            f"{self.noun} failed until the end of the test at {self.clock.at(end)} ({breakdown}).{crash}",
            evidence=[self.users_hit(tail)],
            reasons=["The app crashed (e.g. out of memory) and was not restarted, or restarted into the same load",
                     "The app or its database became saturated and never caught up with the queue of waiting requests",
                     "Auto-scaling or health checks removed the app from the load balancer"],
            checks=[f"Server, proxy and app logs around {self.clock.at(last_pass)}: crashes, restarts, OOM kills",
                    "Whether the site is back now: run 1 user to compare"])]

    def _slow_start(self) -> list[dict]:
        """Failures at the start, then success: cold start or the first burst overloaded the server."""
        if not self.failed or not self.passed:
            return []
        first_pass = min(self.clock.offset(v) for v in self.passed)
        head = [v for v in self.failed if self.clock.offset(v) < first_pass]
        if len(head) < max(3, 0.1 * self.total()):
            return []
        return [self.finding(
            "warning", "server", f"Nothing worked for the first {round(first_pass)} s",
            f"The first {len(head)} {self.noun} failed; the first success came at {self.clock.at(first_pass)}. "
            f"All {self.vus} users started at the same moment.",
            reasons=["The burst of simultaneous first requests overloaded the server (no warm caches, all "
                     "pages rendered at once)",
                     "Cold start: the app or its caches were not warmed up",
                     "Auto-scaling added capacity only after the load began"],
            checks=["Run again right after this run: if the start is fine then, caches / cold start explain it"])]

    def _network_errors(self) -> list[dict]:
        out = []
        texts = {
            "reset": ("Connections were reset", "server",
                      ["The proxy / load balancer dropped connections (connection limit reached)",
                       "The app crashed or restarted in the middle of requests",
                       "A firewall or DDoS protection cut off traffic from this IP address",
                       "Keep-alive timeout mismatch between proxy and app"],
                      ["Proxy and firewall logs in this time window", "App restarts in this time window"]),
            "refused": ("Connections were refused", "server",
                        ["The server or the app was down or restarting", "The port or address is wrong"],
                        ["Whether the site was reachable at that time; app restarts"]),
            "dns": ("The domain name could not be resolved (DNS)", "URL / configuration",
                    ["A typo in the domain", "The domain has no DNS record (yet)",
                     "A local network / VPN / DNS problem on this computer"],
                    ["Check the domain: dig / nslookup", "Try from another network"]),
            "tls": ("The HTTPS connection failed (TLS / certificate)", "server",
                    ["The certificate is expired, self-signed or for another domain",
                     "The server could not keep up with TLS handshakes under load"],
                    ["Open the URL in a browser and check the certificate"]),
        }
        for key, (title, side, reasons, checks) in texts.items():
            items = self.by_kind.get(key, [])
            if items:
                out.append(self.finding(
                    "critical" if len(items) / self.total() >= 0.1 else "warning", side, title,
                    f"{len(items)} of {self.total()} {self.noun}, {self.when(items)}.",
                    evidence=[self.users_hit(items), self._example(items)], reasons=reasons, checks=checks))
        return out

    def _test_browser(self) -> list[dict]:
        out = []
        items = self.by_kind.get("browser", [])
        if items:
            out.append(self.finding(
                "warning", "test computer", "The test browser had problems",
                f"{len(items)} {self.noun} failed inside the test browser itself, not on the site.",
                evidence=[self._example(items)],
                reasons=["This computer was overloaded by too many browser users", "Chrome crashed or was closed"],
                checks=["Run again with fewer parallel users"]))
        recommended = self.mode.limits.recommended_vus
        timeouts = self.by_kind.get("timeout", [])
        if self.browser and self.vus > recommended and timeouts:
            out.append(self.finding(
                "info", "test computer", "The test computer may also be a bottleneck",
                f"{self.vus} browser users is more than the recommended {recommended}: every user is a real "
                "Chrome page on this computer, so some slowness can come from here.",
                checks=["Run the same URL in API-only mode with the same users: if it also times out or is slow, "
                        "the server is the cause",
                        "Or run again with fewer users and compare"]))
        return out

    def _missing(self) -> list[dict]:
        missing = self.expected - self.total()
        if missing <= 0:
            return []
        return [self.finding(
            "warning", "server", f"{missing} {self.noun} never finished",
            f"{missing} of {self.expected} {self.noun} hung beyond the run's time limit and were stopped, "
            "so they are not in these results.",
            reasons=["The site never answered them (see the timeouts)", "The test computer was overloaded"])]

    def _load_growth(self) -> list[dict]:
        values = self.ttfb(self.passed)
        if len(values) < 5:
            return []
        low, med, p95 = min(values), percentile(values, 50), percentile(values, 95)
        if not (med >= 1000 and (med >= 3 * low or p95 >= 4 * low)):
            if med < 3000 or self._queued_together():
                return []
            return [self.finding(
                "warning", "server", f"The server is slow to answer: median {round(med)} ms to the first byte",
                f"Even the fastest successful {self.one} waited {round(low)} ms for the first byte "
                f"(median {round(med)} ms, p95 {round(p95)} ms) with {self.vus} parallel users.",
                reasons=["Every page is rendered again on the server (no page / data cache)",
                         "Slow database queries or backend API calls while building the page",
                         "Too few app workers for this many parallel users"],
                checks=["Run 1 user to get the no-load response time and compare",
                        "Traces of the server-side render: which call takes most of the time"])]
        return [self.finding(
            "warning", "server", "The server gets much slower under parallel load",
            f"Server response time (TTFB) of the successful {self.noun} went from {round(low)} ms (fastest) "
            f"to a median of {round(med)} ms and p95 of {round(p95)} ms with {self.vus} parallel users.",
            evidence=[self._ttfb_by_time()],
            reasons=["Requests wait for a free app worker: too few workers / instances for this load",
                     "The server's CPU is saturated (e.g. rendering every page again: no page cache)",
                     "Slow database queries or backend calls that get slower as more run at the same time"],
            checks=["Run 1 user to get the no-load TTFB and compare",
                    "Server CPU during the test; page / API caching; number of app workers"])]

    def _queued_together(self) -> list[dict]:
        """Many visits that started together all got their first byte at nearly the same moment."""
        ordered = sorted((v for v in self.passed if isinstance(v.get("ttfbMs"), (int, float))),
                         key=self.clock.offset)
        best: list[dict] = []
        group: list[dict] = []
        for v in ordered:
            if group and self.clock.offset(v) - self.clock.offset(group[0]) > 2:
                group = []
            group.append(v)
            if len(group) > len(best):
                best = list(group)
        if len(best) < 5:
            return []
        values = [v["ttfbMs"] for v in best]
        med = percentile(values, 50)
        arrivals = [self.clock.offset(v) * 1000 + v["ttfbMs"] for v in best]
        spread = max(arrivals) - min(arrivals)
        if med < 3000 or spread > 0.2 * med:
            return []
        return [self.finding(
            "warning", "server", "Requests were answered all together after a long wait",
            f"{len(best)} {self.noun} that started together {self.when(best)} each waited "
            f"{round(min(values))}–{round(max(values))} ms for the first byte, and the answers arrived within "
            f"{round(spread)} ms of each other. When many requests are released at the same moment, they were "
            "usually all waiting for one shared, blocking thing.",
            reasons=["A cold cache being rebuilt: every request waited for the same slow rebuild",
                     "One slow backend / API call that all page renders depend on",
                     "A blocked event loop: one Node process doing heavy work and answering nothing meanwhile",
                     "A single worker or a lock (mutex) letting one request through at a time"],
            checks=["Traces of the server-side render: which call takes most of the time",
                    "Whether the app runs one process or several (cluster / replicas)"])]

    def _where_time_goes(self) -> list[dict]:
        if not self.browser:
            return []
        pairs = [(v["ttfbMs"], v["fullyLoadedMs"]) for v in self.passed
                 if isinstance(v.get("ttfbMs"), (int, float)) and isinstance(v.get("fullyLoadedMs"), (int, float))]
        if len(pairs) < 3:
            return []
        ttfb = percentile([a for a, _ in pairs], 50)
        full = percentile([b for _, b in pairs], 50)
        if not full or full < 3000:
            return []
        share = ttfb / full
        if share >= 0.5:
            return [self.finding(
                "info", "server", "Most of the load time is spent waiting for the server",
                f"The server's first byte takes {round(ttfb)} ms of the {round(full)} ms full load (median): "
                f"{round(share * 100)}%. Speeding up the page's resources helps little; the server is the bottleneck.")]
        if share <= 0.2:
            slow = [f"{r['url']} — median {round(r['medMs'])} ms" for r in self.resources()["slowest"][:3]]
            return [self.finding(
                "info", "page", "The server answers fast; the time goes into loading the page's resources",
                f"First byte after {round(ttfb)} ms, fully loaded after {round(full)} ms (median).",
                evidence=slow,
                reasons=["Large JavaScript bundles or images", "Slow API calls made by the page in the browser",
                         "Third-party scripts"])]
        return []

    def _resources_failing(self) -> list[dict]:
        failing = [r for r in self.resources()["failing"] if r["visits"] >= max(2, 0.2 * self.total())]
        if not failing:
            return []
        return [self.finding(
            "warning", "page", "Some of the page's resources fail",
            "These requests made by the page failed in many visits:",
            evidence=[f"{r['url']} — {r['problem']} in {r['visits']} {self.noun}" for r in failing[:6]],
            reasons=["Missing files (404) after a deploy", "An API the page calls fails under load",
                     "Blocked third-party resources"])]

    def _heavy_page(self) -> list[dict]:
        sizes = [v["transferBytes"] for v in self.passed if isinstance(v.get("transferBytes"), (int, float))]
        med = percentile(sizes, 50)
        if not self.browser or not med or med < 3_000_000:
            return []
        return [self.finding(
            "info", "page", f"The page is heavy: {size(med)} per visit",
            f"Every visit transfers about {size(med)} (median). With {self.vus} users that is about "
            f"{size(med * self.vus)} at once, which costs the server CPU and bandwidth.",
            reasons=["Responses not compressed (gzip / brotli)", "Large JavaScript bundles",
                     "Large data embedded in the HTML (e.g. search indexes, state)", "Unoptimized images"])]

    # -- evidence helpers -------------------------------------------------------------------
    def _example(self, items: list[dict]) -> str | None:
        error = next((v.get("error") for v in items if v.get("error")), None)
        return f"Example: {error[:220]}" if error else None

    def _answer_evidence(self, items: list[dict]) -> list[str]:
        bodies = Counter(readable_body(v.get("bodySnippet")) for v in items)
        bodies.pop("", None)
        return [f"Server answered: {body}" for body, _ in bodies.most_common(2)]

    def _header_values(self, items: list[dict], name: str) -> str | None:
        values = Counter((v.get("responseHeaders") or {}).get(name) for v in items)
        values.pop(None, None)
        return ", ".join(str(k) for k, _ in values.most_common(3)) or None

    def _passed_ttfb_evidence(self) -> str | None:
        values = self.ttfb(self.passed)
        if not values:
            return f"No {self.one} succeeded, so there is no response time to compare."
        return (f"Even the {len(values)} successful {self.noun} waited {round(min(values))}–{round(max(values))} ms "
                f"for the first byte (median {round(percentile(values, 50))} ms)")

    def _ttfb_by_time(self) -> str | None:
        rows = [(self.clock.offset(v), v["ttfbMs"]) for v in self.passed if isinstance(v.get("ttfbMs"), (int, float))]
        if len(rows) < 6:
            return None
        rows.sort()
        third = len(rows) // 3
        first = percentile([t for _, t in rows[:third]], 50)
        last = percentile([t for _, t in rows[-third:]], 50)
        return f"Median TTFB: first third of the run {round(first)} ms, last third {round(last)} ms"

    # -- tables -------------------------------------------------------------------------------
    def categories(self) -> list[dict[str, Any]]:
        rows = []
        for key, items in sorted(self.by_kind.items(), key=lambda kv: -len(kv[1])):
            first, last = self.span(items)
            rows.append({"key": key, "label": CATEGORIES[key].label, "meaning": CATEGORIES[key].meaning,
                         "count": len(items), "firstS": first, "lastS": last,
                         "first": self.clock.at(first), "last": self.clock.at(last),
                         "users": len({v.get("vu") for v in items}),
                         "example": (items[0].get("error") or "")[:300]})
        return rows

    def timeline(self) -> dict[str, Any]:
        if not self.visits:
            return {"bucketS": 0, "buckets": []}
        _, end = self.span(self.visits)
        step = _bucket_size(end + 1)
        buckets: dict[int, dict[str, Any]] = {}
        for v in self.visits:
            start = int(self.clock.offset(v) // step) * step
            b = buckets.setdefault(start, {"startS": start, "label": self.clock.at(start), "passed": 0,
                                           "failed": 0, "kinds": Counter(), "ttfb": []})
            if v.get("passed"):
                b["passed"] += 1
                if isinstance(v.get("ttfbMs"), (int, float)):
                    b["ttfb"].append(v["ttfbMs"])
            else:
                b["failed"] += 1
                b["kinds"][self.kind[id(v)]] += 1
        rows = []
        for start in range(0, int(end // step) * step + step, step):
            b = buckets.get(start) or {"startS": start, "label": self.clock.at(start), "passed": 0,
                                       "failed": 0, "kinds": Counter(), "ttfb": []}
            rows.append({"startS": b["startS"], "label": b["label"], "passed": b["passed"], "failed": b["failed"],
                         "kinds": dict(b["kinds"]), "ttfbMed": percentile(b["ttfb"], 50)})
        return {"bucketS": step, "buckets": rows}

    def resources(self) -> dict[str, list[dict[str, Any]]]:
        failing: dict[str, dict[str, Any]] = {}
        slow: dict[str, list[float]] = defaultdict(list)
        for v in self.visits:
            seen = set()
            for r in v.get("failedRequests") or []:
                url = _short_url(r.get("url"))
                entry = failing.setdefault(url, {"url": url, "visits": 0, "problems": Counter(), "type": r.get("type")})
                if url not in seen:
                    entry["visits"] += 1
                    seen.add(url)
                entry["problems"][str(r.get("status") or r.get("error") or "failed")] += 1
            for r in v.get("slowRequests") or []:
                if isinstance(r.get("ms"), (int, float)):
                    slow[_short_url(r.get("url"))].append(r["ms"])
        failing_rows = sorted(failing.values(), key=lambda r: -r["visits"])
        for row in failing_rows:
            row["problem"] = ", ".join(f"{p}" + (f" ×{n}" if n > 1 else "") for p, n in row.pop("problems").most_common(3))
        slow_rows = sorted(({"url": url, "visits": len(ms), "medMs": percentile(ms, 50), "maxMs": max(ms)}
                            for url, ms in slow.items()), key=lambda r: -(r["medMs"] or 0))
        return {"failing": failing_rows[:20], "slowest": slow_rows[:10]}

    def answers(self) -> list[dict[str, Any]]:
        """What the server sent back on failures: status, Server header, body excerpt."""
        groups: dict[tuple, int] = Counter()
        for v in self.failed:
            headers = v.get("responseHeaders") or {}
            body = (v.get("bodySnippet") or "").strip()[:300]
            if headers or body:
                groups[(visit_status(v), headers.get("server"), headers.get("content-type"), body)] += 1
        return [{"status": s, "server": server, "contentType": ctype, "body": body, "count": n}
                for (s, server, ctype, body), n in groups.most_common(5)]

    def notes(self) -> list[str]:
        notes = []
        screenshot_timeouts = sum(1 for line in self.other_lines
                                  if "screenshot failed" in line.lower() and "timed out" in line.lower())
        if screenshot_timeouts:
            notes.append(f"{screenshot_timeouts} screenshots of hung pages timed out, so the visit times of those "
                         "failed visits include up to 30 s of waiting for the screenshot.")
        return notes


def readable_body(body: Any) -> str:
    """A short, readable version of what the server answered: an HTML page's title, else the text."""
    text = str(body or "").strip()
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if title:
        return f"HTML page titled \"{title.group(1).strip()[:150]}\""
    return text[:200]


def _short_url(url: Any) -> str:
    text = str(url or "")
    parts = urlsplit(text)
    if not parts.scheme:
        return text[:160]
    return f"{parts.scheme}://{parts.netloc}{parts.path}"[:160]
