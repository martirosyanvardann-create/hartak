"""Burst test logic: check before, bursts in rounds, recovery watch, verdicts.

A burst is N separate users requesting the page at the same moment (one k6 run
with N users and N requests). Around every burst the site is checked like a
single visitor would:

* before the first burst: is the site up at all? (otherwise the test stops and
  says so, instead of blaming the load)
* after every burst: one request every few seconds until the site answers again,
  for at most ``recovery_s`` seconds ("did it die, and for how long?")

Verdict of one burst:  ok · slow · failing · down
  ok       every user got the page, first byte p95 within the "slow" limit
  slow     every user got the page, but the server's first byte (p95) was above the limit
           (judged on the first byte: the whole-page time also depends on the test computer's connection)
  failing  some users got an error or nothing; the site answered again afterwards
  down     the site still did not answer when the recovery check ended
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .analysis import percentile
from .diagnosis import CATEGORIES, classify, diagnose
from .modes import get_mode

CHECK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36 pageload-check",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}
CHECK_EVERY_S = 3
VERDICTS = ("ok", "slow", "failing", "down")
ICONS = {"ok": "✅", "slow": "⚠️", "failing": "❌", "down": "💀"}
LABELS = {"ok": "Handled", "slow": "Slow", "failing": "Failing", "down": "Went down"}


@dataclass
class Check:
    at: float              # seconds since the test started
    ok: bool
    status: int | None
    ms: float
    error: str | None = None


def check_url(url: str, timeout_s: float = 15, expect_2xx: bool = True) -> tuple[bool, int | None, float, str | None]:
    """One request like a single visitor: (ok, status, ms, error)."""
    started = time.monotonic()
    request = urllib.request.Request(url, headers=CHECK_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    except Exception as error:  # noqa: BLE001 - any network problem is a failed check
        return False, None, (time.monotonic() - started) * 1000, str(error)[:200]
    ms = (time.monotonic() - started) * 1000
    ok = 200 <= status < 300 if expect_2xx else status < 400
    return ok, status, ms, None if ok else f"HTTP {status}"


class Watcher:
    """Runs the single-visitor checks. Injectable (``check``, ``sleep``, ``clock``) for tests."""

    def __init__(self, url: str, *, timeout_s: float = 15,
                 check: Callable[..., tuple] = check_url, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, stopped: Callable[[], bool] = lambda: False):
        self.url, self.timeout_s = url, timeout_s
        self._check, self._sleep, self._clock, self._stopped = check, sleep, clock, stopped
        self.started = clock()

    def once(self) -> Check:
        ok, status, ms, error = self._check(self.url, self.timeout_s)
        return Check(round(self._clock() - self.started, 1), ok, status, round(ms), error)

    def recovery(self, window_s: float) -> dict[str, Any]:
        """Check every few seconds until the site answers, for at most ``window_s`` seconds."""
        begin = self._clock()
        checks: list[Check] = []
        while True:
            check = self.once()
            checks.append(check)
            if check.ok:
                waited = self._clock() - begin
                return {"recovered": True, "afterS": round(waited, 1) if len(checks) > 1 else 0,
                        "checks": [asdict(c) for c in checks]}
            if self._clock() - begin >= window_s or self._stopped():
                return {"recovered": False, "afterS": None, "checks": [asdict(c) for c in checks]}
            self._sleep(CHECK_EVERY_S)


@dataclass
class BurstRound:
    users: int
    round: int
    started: str | None
    answered: int
    failed: int
    first_byte_p95: float | None
    page_p95: float | None
    page_max: float | None
    errors: dict[str, int] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    verdict: str = "ok"
    problem: str | None = None        # most important finding of this burst, if any

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(icon=ICONS[self.verdict], label=LABELS[self.verdict])
        return data


def round_result(visits: list[dict[str, Any]], users: int, number: int, recovery: dict[str, Any],
                 slow_ms: int) -> BurstRound:
    answered = [v for v in visits if v.get("passed")]
    missing = max(0, users - len(visits))          # requests k6 did not even record (hung)
    failed = len(visits) - len(answered) + missing
    errors: dict[str, int] = {}
    for v in visits:
        kind = classify(v)
        if kind:
            label = CATEGORIES[kind].label
            errors[label] = errors.get(label, 0) + 1
    if missing:
        errors["No answer recorded"] = missing
    page = [v.get("pageMs") if v.get("pageMs") is not None else v.get("totalMs") for v in answered]
    page = [p for p in page if p is not None]
    first = [v["ttfbMs"] for v in answered if v.get("ttfbMs") is not None]
    page_p95 = percentile(page, 95)
    first_p95 = percentile(first, 95)

    if not recovery.get("recovered", True):
        verdict = "down"
    elif failed:
        verdict = "failing"
    elif first_p95 is not None and first_p95 > slow_ms:
        verdict = "slow"
    else:
        verdict = "ok"
    problem = None
    if failed:
        headline = diagnose(visits, mode=get_mode("burst"), vus=users, expected=users)["headline"]
        problem = None if headline == "No problems found." else headline
    elif verdict == "slow":
        problem = f"first byte p95 {first_p95 / 1000:.1f} s, above the {slow_ms / 1000:.1f} s limit"
    started = min((v.get("startedAt") for v in visits if v.get("startedAt")), default=None)
    return BurstRound(users=users, round=number, started=started, answered=len(answered), failed=failed,
                      first_byte_p95=first_p95, page_p95=page_p95,
                      page_max=max(page) if page else None, errors=errors, recovery=recovery,
                      verdict=verdict, problem=problem)


def plan_result(rounds: list[BurstRound], plan_steps: tuple[int, ...], rounds_per_step: int,
                before: dict[str, Any] | None, stopped_early: str | None = None) -> dict[str, Any]:
    """Per step: how many rounds were handled; overall: the answer in one sentence."""
    steps = []
    for users in plan_steps:
        mine = [r for r in rounds if r.users == users]
        if not mine:
            continue
        counts = {v: sum(1 for r in mine if r.verdict == v) for v in VERDICTS}
        worst = next(v for v in reversed(VERDICTS) if counts[v]) if any(counts.values()) else "ok"
        down_times = [r.recovery.get("afterS") or r.recovery.get("cameBackAfterS") for r in mine
                      if r.recovery.get("afterS") or r.recovery.get("cameBackAfterS")]
        never = any(r.verdict == "down" and not r.recovery.get("cameBackAfterS") for r in mine)
        steps.append({"users": users, "rounds": len(mine), "planned": rounds_per_step, "counts": counts,
                      "worst": worst, "icon": ICONS[worst],
                      "text": _step_text(users, len(mine), counts, down_times, never)})

    handled = [s["users"] for s in steps if s["counts"]["ok"] == s["rounds"]]
    first_problem = next((s for s in steps if s["counts"]["ok"] < s["rounds"]), None)
    if before and not before.get("ok"):
        answer = "The site was already not answering before the test, so no burst was sent."
    elif not steps:
        answer = "No burst finished."
    elif first_problem is None:
        answer = f"Handled every burst: up to {steps[-1]['users']} users at the same moment, in every round."
    else:
        answer = _headline(steps) + " " + " ".join(s["text"] for s in steps)
    if stopped_early:
        answer += f" {stopped_early}"
    return {"steps": steps, "answer": answer, "handledUsers": max(handled) if handled else 0,
            "firstProblemUsers": first_problem["users"] if first_problem else None}


def _headline(steps: list[dict[str, Any]]) -> str:
    """The most important fact first: went down > failing > slow."""
    for verdict, words in (("down", "Went down"), ("failing", "Errors"), ("slow", "Slow")):
        hit = next((s["users"] for s in steps if s["counts"][verdict]), None)
        if hit:
            return f"{words} at {hit} users at the same moment."
    return ""


def _step_text(users: int, total: int, counts: dict[str, int], down_times: list[float],
               never: bool = False) -> str:
    if counts["ok"] == total:
        return f"{users} users at once: handled in {'all ' + str(total) + ' rounds' if total > 1 else 'the round'}."
    parts = [f"{counts[v]} {LABELS[v].lower()}" for v in VERDICTS if counts[v]]
    text = f"{users} users at once: {', '.join(parts)} (of {total} {'rounds' if total != 1 else 'round'})"
    if counts["down"] and never:
        text += "; the site stopped answering and had not come back when the test ended"
    elif counts["down"]:
        text += f"; the site stopped answering and came back after {_duration(max(down_times))}"
    elif down_times:
        text += f"; after the burst the site needed up to {max(down_times):.0f} s to answer again"
    return text + "."


def _duration(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"
