"""A run of one or two URLs (state machine).

States:  running ──► done | problem | stopped | error   (final states never change)

* done     every URL ran all its visits (some visits may still have failed)
* problem  at least one URL is missing visits: they hung, or k6 could not run
* stopped  the user pressed Stop
* error    the run could not start at all (e.g. k6 is not installed)

The k6 runner, the shared browser and the report builder are injected, so
tests can run a Job without k6 or Chrome.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ContextManager, Protocol

from . import analysis
from .chrome import SharedChrome
from .config import RunConfig
from .formatting import host_of
from .k6 import K6Result, K6Runner, find_k6
from .report import build_report
from .results import REPORT_HTML, count_visits, load_run

LOG_LINES = 200
RESULT_SUFFIX = ".json"     # reports/<job id>.json: the saved result of a finished run
log = logging.getLogger("pageload.job")


def job_result_path(reports_dir: Path, job_id: str) -> Path:
    return reports_dir / f"{job_id}{RESULT_SUFFIX}"


class JobState(str, Enum):
    RUNNING = "running"
    DONE = "done"
    PROBLEM = "problem"
    STOPPED = "stopped"
    ERROR = "error"


_ALLOWED = {JobState.RUNNING: {JobState.DONE, JobState.PROBLEM, JobState.STOPPED, JobState.ERROR}}


class Runner(Protocol):
    def run(self, script: Path, k6_vars: dict[str, str], out_dir: Path, *,
            extra_env: dict[str, str] | None = None, watchdog_s: float | None = None,
            on_line: Callable[[str], None] = ...) -> K6Result: ...

    def stop(self) -> None: ...


@dataclass
class Target:
    url: str
    run_dir: Path
    problem: str | None = None
    finished: bool = False


def diagnose(recorded: int, expected: int, noun: str, result: K6Result, limit_s: int) -> str | None:
    """Why a URL is missing visits, in one sentence (None if nothing is missing)."""
    missing = expected - recorded
    if missing <= 0 or result.stopped:
        return None
    if result.watchdog_fired:
        return (f"k6 stopped responding and was ended after {round(result.elapsed_s)} s. "
                f"{missing} of {expected} {noun} did not finish.")
    connect = [e for e in result.errors
               if any(s in e.lower() for s in ("connecting to browser", "broken pipe", "connection reset"))]
    if connect and recorded == 0:
        return f"No {noun} ran: k6 could not connect to Chrome. All {expected} {noun} are missing."
    if result.elapsed_s >= limit_s:
        return (f"{missing} of {expected} {noun} hung and did not finish within the {limit_s} s time "
                f"limit. They were stopped and are not in the results.")
    reason = _short_reason(result.errors[-1] if result.errors else "")
    return f"{missing} of {expected} {noun} did not finish. {reason}"


def _short_reason(line: str) -> str:
    text = line.lower()
    if "timed out" in text or "deadline exceeded" in text:
        return "Pages timed out: the site or this computer may be overloaded."
    if "executable" in text or "chrome not found" in text:
        return "Chrome was not found."
    if line:
        return line.split(" at default (")[0][-200:]
    return "See the k6 output for details."


class Job:
    def __init__(self, config: RunConfig, reports_dir: Path, *,
                 runner_factory: Callable[[], Runner] | None = None,
                 browser_factory: Callable[[], ContextManager[Any]] = SharedChrome,
                 report_builder: Callable[[Path], Path] = build_report,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.mode = config.mode
        self._runner_factory = runner_factory
        self._browser_factory = browser_factory
        self._report_builder = report_builder
        self._clock = clock
        self._lock = threading.Lock()
        self._runner: Runner | None = None
        self._stop_requested = False

        self.state = JobState.RUNNING
        self.message = ""
        self.started = clock()
        self.finished: float | None = None
        self.current = 0
        self.log: list[str] = []
        self.reports_dir = reports_dir
        self.id = self._new_id(reports_dir)
        self.targets = [
            Target(url, reports_dir / f"{self.id}-{chr(65 + i)}-{host_of(url)}")
            for i, url in enumerate(config.urls)
        ]

    @staticmethod
    def _new_id(reports_dir: Path) -> str:
        """Timestamp id, made unique if two runs start within the same second."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate, n = stamp, 1
        while job_result_path(reports_dir, candidate).exists() or any(reports_dir.glob(f"{candidate}-[A-Z]-*")):
            n += 1
            candidate = f"{stamp}-{n}"
        return candidate

    # -- state machine -------------------------------------------------------
    def _transition(self, new_state: JobState, message: str = "") -> None:
        with self._lock:
            if new_state not in _ALLOWED.get(self.state, set()):
                raise RuntimeError(f"Invalid job transition {self.state.value} -> {new_state.value}")
            self.state, self.message, self.finished = new_state, message, self._clock()
        self._save_result()

    def _save_result(self) -> None:
        """Keep the finished run's result on disk, so it can be downloaded later,
        even after the app restarts."""
        try:
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            job_result_path(self.reports_dir, self.id).write_text(
                json.dumps(self.status(), indent=2, default=str), encoding="utf-8")
        except OSError as error:
            log.warning("Could not save the run result: %s", error)

    @property
    def is_running(self) -> bool:
        return self.state is JobState.RUNNING

    # -- running ---------------------------------------------------------------
    def run(self) -> None:
        """Run every URL, one after the other (call in a background thread)."""
        runner_factory = self._runner_factory
        if runner_factory is None:
            k6_path = find_k6()
            if not k6_path:
                self._transition(JobState.ERROR, "k6 was not found. Install it with: brew install k6")
                return
            runner_factory = lambda: K6Runner(k6_path)  # noqa: E731

        try:
            for index, target in enumerate(self.targets):
                if self._stop_requested:
                    break
                self.current = index
                self._run_target(target, runner_factory())
        except Exception as error:  # noqa: BLE001 - reported to the user, never silently lost
            self._add_log(f"Unexpected error: {error}")
            self._transition(JobState.ERROR, f"The run failed: {error}")
            return

        if self._stop_requested:
            self._transition(JobState.STOPPED, "Stopped.")
        elif any(t.problem for t in self.targets):
            self._transition(JobState.PROBLEM, "The run finished with problems: see the URL marked in red.")
        else:
            self._transition(JobState.DONE)

    def _run_target(self, target: Target, runner: Runner) -> None:
        config, mode = self.config, self.mode
        limit = config.time_limit_s()
        browser = self._browser_factory() if mode.needs_browser else contextlib.nullcontext()
        with browser as chrome:
            extra_env = {}
            ws_url = getattr(chrome, "ws_url", None)
            if ws_url:
                extra_env["K6_BROWSER_WS_URL"] = ws_url
            elif getattr(chrome, "message", None):
                self._add_log(chrome.message)
            self._runner = runner
            if self._stop_requested:
                return
            result = runner.run(
                mode.script, mode.k6_vars(config, target.url, target.run_dir), target.run_dir,
                extra_env=extra_env,
                watchdog_s=limit + config.graceful_stop_s + 60,
                on_line=self._add_log,
            )
        self._runner = None
        self._report_builder(target.run_dir)
        target.finished = True
        if not self._stop_requested:
            target.problem = diagnose(count_visits(target.run_dir), config.iterations, mode.noun, result, limit)

    def stop(self) -> None:
        self._stop_requested = True
        runner = self._runner
        if runner:
            runner.stop()

    def _add_log(self, line: str) -> None:
        self.log.append(line)
        del self.log[:-LOG_LINES]

    # -- reporting -----------------------------------------------------------------
    def progress(self, index: int) -> int:
        return count_visits(self.targets[index].run_dir)

    def _phase(self, index: int, target: Target, done: int) -> str:
        if target.finished and target.problem:
            return "failed" if done == 0 else "incomplete"
        if target.finished:
            return "done"
        if self.is_running:
            return "running" if index == self.current else "waiting"
        return self.state.value

    def status(self) -> dict[str, Any]:
        """Everything the GUI, the API and the command line show about this run."""
        config, mode = self.config, self.mode
        targets = []
        for index, target in enumerate(self.targets):
            done = self.progress(index)
            stats = None
            if target.finished and done > 0:
                data = load_run(target.run_dir)
                stats = analysis.target_stats(data.visits, data.summary, mode)
                report = target.run_dir / REPORT_HTML
                stats["report"] = f"/reports/{target.run_dir.name}/{REPORT_HTML}" if report.exists() else None
                stats["reportPath"] = str(report) if report.exists() else None
                stats["download"] = f"/api/runs/{target.run_dir.name}/download"
            targets.append({
                "url": target.url,
                "runFolder": target.run_dir.name,
                "phase": self._phase(index, target, done),
                "done": done,
                "total": config.iterations,
                "problem": target.problem,
                "stats": stats,
                "budget": analysis.budget_check(stats, mode, config.settings.budget_p95_ms),
            })

        comparison = None
        if len(targets) == 2 and all(t["stats"] for t in targets):
            comparison = analysis.compare(targets[0]["url"], targets[0]["stats"],
                                          targets[1]["url"], targets[1]["stats"], mode)
        ok = self.state is JobState.DONE and all(
            t["stats"] and t["stats"]["passed"] == config.iterations
            and (t["budget"] is None or t["budget"]["passed"])
            for t in targets
        )
        return {
            "id": self.id,
            "download": None if self.is_running else f"/api/jobs/{self.id}/download",
            "state": self.state.value,
            "ok": ok,
            "message": self.message,
            "mode": mode.name,
            "method": config.request.method if mode.name == "api" else None,
            "timings": [[t.field, t.label] for t in mode.timings],
            "mainMetric": mode.main_metric,
            "mainLabel": mode.main_label,
            "noun": mode.noun,
            "settings": config.settings.to_dict(),
            "vus": config.vus,
            "iterations": config.iterations,
            "warnings": config.warnings(),
            "waitUntil": config.wait_until,
            "elapsedMs": ((self.finished or self._clock()) - self.started) * 1000,
            "targets": targets,
            "comparison": comparison,
            "log": self.log[-40:],
        }
