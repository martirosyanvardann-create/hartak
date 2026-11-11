"""Test doubles: a fake k6 runner and a fake browser, so tests never start k6 or Chrome."""

from __future__ import annotations

import json
from pathlib import Path

from pageload.k6 import K6Result
from pageload.results import CONSOLE_LOG, SUMMARY_JSON


def visit(n: int, *, passed: bool = True, **timings) -> dict:
    record = {"visit": n, "vu": 1, "iteration": n - 1, "startedAt": "2026-11-10T10:00:00Z",
              "httpStatus": 200 if passed else 500, "passed": passed,
              "error": None if passed else "request: HTTP status 500, expected 200",
              "requests": 10, "transferBytes": 50_000, "durationMs": 900}
    record.update(timings)
    return record


def browser_visit(n: int, full_ms: float, **extra) -> dict:
    return visit(n, ttfbMs=full_ms * 0.1, fcpMs=full_ms * 0.4, lcpMs=full_ms * 0.5,
                 domMs=full_ms * 0.6, loadMs=full_ms * 0.9, fullyLoadedMs=full_ms, **extra)


def write_run(run_dir: Path, visits: list[dict], summary: dict | None = None, other_lines=()) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = list(other_lines) + [f"VISIT {json.dumps(v)}" for v in visits]
    (run_dir / CONSOLE_LOG).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (run_dir / SUMMARY_JSON).write_text(json.dumps(summary or {"mode": "browser", "totalRuntimeMs": 5000}))
    return run_dir


class FakeRunner:
    """Writes prepared visits into the run folder instead of running k6."""

    def __init__(self, visits_per_url: list[list[dict]], *, result: dict | None = None, mode: str = "browser",
                 expected: int | None = None):
        self.visits_per_url = list(visits_per_url)
        self.result = result or {}
        self.mode = mode
        self.expected = expected
        self.calls: list[dict] = []
        self.stopped = False

    def run(self, script, k6_vars, out_dir, *, extra_env=None, watchdog_s=None, on_line=lambda line: None):
        self.calls.append({"script": script, "vars": dict(k6_vars), "env": dict(extra_env or {}),
                           "watchdog_s": watchdog_s})
        visits = self.visits_per_url.pop(0) if self.visits_per_url else []
        write_run(out_dir, visits, {"mode": self.mode, "totalRuntimeMs": 4000,
                                    "expectedVisits": self.expected or len(visits)})
        on_line("fake k6 finished")
        return K6Result(exit_code=0, elapsed_s=self.result.get("elapsed_s", 4.0),
                        errors=self.result.get("errors", []),
                        watchdog_fired=self.result.get("watchdog_fired", False), stopped=self.stopped)

    def stop(self):
        self.stopped = True


class FakeBrowser:
    """Stands in for SharedChrome."""

    instances = 0
    closed = 0

    def __init__(self, ws_url: str | None = "ws://127.0.0.1:1/devtools/browser/fake", message: str | None = None):
        self.ws_url = ws_url
        self.message = message

    def __enter__(self):
        FakeBrowser.instances += 1
        return self

    def __exit__(self, *exc):
        FakeBrowser.closed += 1


def no_report(run_dir: Path) -> Path:
    return run_dir / "report.html"
