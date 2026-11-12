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

from . import analysis, diagnosis
from .burst import LABELS, Watcher, check_url, plan_result, round_result
from .chrome import SharedChrome
from .config import RunConfig
from .formatting import host_of
from .k6 import K6Result, K6Runner, find_k6
from .machine import MachineMonitor
from .modes import MODES, load_k6_vars
from .report import build_report
from .results import LOAD_DIR, MACHINE_JSON, REPORT_HTML, count_visits, load_run

LOG_LINES = 200
DOWN_WATCH_S = 300          # burst mode: after the site went down, watch up to 5 more minutes
LOAD_WARMUP_S = 5           # load runs this long before the browsers start measuring
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
    burst: dict[str, Any] | None = None    # burst mode: check before, rounds and the answer
    stats: dict[str, Any] | None = None    # computed once, after the URL finished


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
                 monitor_factory: Callable[[], Any] = MachineMonitor,
                 load_warmup_s: float = LOAD_WARMUP_S,
                 url_check: Callable[..., tuple] = check_url,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.mode = config.mode
        self._runner_factory = runner_factory
        self._browser_factory = browser_factory
        self._report_builder = report_builder
        self._monitor_factory = monitor_factory
        self._load_warmup_s = load_warmup_s
        self._url_check = url_check
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._runner: Runner | None = None
        self._load_runner: Runner | None = None
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
                self._run_target(target, runner_factory)
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

    def _run_target(self, target: Target, runner_factory: Callable[[], Runner]) -> None:
        config, mode = self.config, self.mode
        if config.burst:
            self._run_burst(target, runner_factory)
            return
        runner = runner_factory()
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
            monitor = self._monitor_factory()
            with monitor:
                load = self._start_load(target, runner_factory) if config.load_users else None
                result = None
                try:
                    if not self._stop_requested:
                        result = runner.run(
                            mode.script, mode.k6_vars(config, target.url, target.run_dir), target.run_dir,
                            extra_env=extra_env,
                            watchdog_s=limit + config.graceful_stop_s + 60,
                            on_line=self._add_log,
                        )
                finally:
                    self._stop_load(load)
        self._runner = None
        if result is None:      # stopped before the measured run started
            return
        self._save_machine(target.run_dir, monitor)
        self._report_builder(target.run_dir)
        target.finished = True
        if not self._stop_requested:
            target.problem = diagnose(count_visits(target.run_dir), config.iterations, mode.noun, result, limit)

    # -- burst mode --------------------------------------------------------------------
    def _run_burst(self, target: Target, runner_factory: Callable[[], Runner]) -> None:
        """Check before, then every step × round: burst, recovery check, pause."""
        config, mode, plan = self.config, self.mode, self.config.burst
        run_dir = target.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        watcher = Watcher(target.url, timeout_s=config.settings.timeout_s, check=self._url_check,
                          sleep=self._sleep, clock=self._clock, stopped=lambda: self._stop_requested)
        rounds: list = []
        stopped_early = None
        started = self._clock()
        monitor = self._monitor_factory()
        with monitor:
            before = watcher.once()
            target.burst = {"before": before.__dict__, "rounds": []}
            self._add_log(f"Check before the test: {'answers' if before.ok else 'NOT answering'} "
                          f"({before.status or before.error}, {before.ms:.0f} ms)")
            if not before.ok:
                target.problem = (f"The check before the test failed ({before.error or before.status}), "
                                  "so no burst was sent.")
            for users in plan.steps if before.ok else ():
                for number in range(1, plan.rounds + 1):
                    if rounds and not self._wait(plan.pause_s):
                        break
                    burst_dir = run_dir / "bursts" / f"{users:04d}-users-round-{number}"
                    self._add_log(f"Burst: {users} users at the same moment (round {number} of {plan.rounds})")
                    runner = runner_factory()
                    self._runner = runner
                    runner.run(mode.script, mode.burst_vars(config, target.url, burst_dir, users), burst_dir,
                               watchdog_s=config.settings.timeout_s * 2 + 90, on_line=self._add_log)
                    self._runner = None
                    if self._stop_requested:
                        break
                    visits = load_run(burst_dir).visits
                    recovery = watcher.recovery(plan.recovery_s)
                    result = round_result(visits, users, number, recovery, plan.slow_ms)
                    rounds.append(result)
                    target.burst["rounds"] = [r.to_dict() for r in rounds]
                    self._append_burst_visits(run_dir, visits, users, number)
                    self._add_log(f"  → {LABELS[result.verdict]}: {result.answered} of {users} answered"
                                  + (f", {result.problem}" if result.problem else ""))
                    if result.verdict == "down":
                        # Keep watching (lightly) to learn how long the site was down.
                        self._add_log(f"  The site is not answering: watching for up to {DOWN_WATCH_S // 60} more minutes")
                        later = watcher.recovery(DOWN_WATCH_S)
                        if later["recovered"]:
                            result.recovery["cameBackAfterS"] = round(plan.recovery_s + (later["afterS"] or 0))
                        result.recovery["laterChecks"] = len(later["checks"])
                        target.burst["rounds"] = [r.to_dict() for r in rounds]
                        stopped_early = "The test stopped there."
                        break
                if stopped_early or self._stop_requested:
                    break
                mine = [r for r in rounds if r.users == users]
                if users != plan.steps[-1] and sum(r.verdict in ("failing", "down") for r in mine) * 2 > len(mine):
                    stopped_early = f"Larger bursts were skipped because {users} users already failed in most rounds."
                    break

        target.burst.update(plan_result(rounds, plan.steps, plan.rounds, target.burst["before"], stopped_early),
                            plan=plan.to_dict())
        (run_dir / "burst.json").write_text(json.dumps(target.burst, indent=2, default=str), encoding="utf-8")
        (run_dir / "summary.json").write_text(json.dumps({
            "mode": "burst", "url": target.url, "profile": "burst",
            "expectedVisits": sum(r.users for r in rounds), "vus": max(plan.steps),
            "timeoutMs": config.settings.timeout_s * 1000,
            "totalRuntimeMs": (self._clock() - started) * 1000}), encoding="utf-8")
        if not (run_dir / "console.log").exists():
            (run_dir / "console.log").write_text("", encoding="utf-8")
        self._save_machine(run_dir, monitor)
        self._report_builder(run_dir)
        target.finished = True
        if not target.problem and any(r.verdict == "down" for r in rounds):
            target.problem = "The site went down during a burst and did not come back within the recovery check."

    def _wait(self, seconds: float) -> bool:
        """Pause between bursts; False if the run was stopped meanwhile."""
        end = self._clock() + seconds
        while self._clock() < end:
            if self._stop_requested:
                return False
            self._sleep(min(1.0, max(0.0, end - self._clock())))
        return not self._stop_requested

    @staticmethod
    def _append_burst_visits(run_dir: Path, visits: list[dict], users: int, number: int) -> None:
        """Collect every burst's requests in the run folder's console.log (for the report and downloads)."""
        console = run_dir / "console.log"
        existing = count_visits(run_dir)
        with console.open("a", encoding="utf-8") as handle:
            for i, visit in enumerate(visits, start=1):
                record = {**visit, "visit": existing + i, "burstUsers": users, "burstRound": number}
                handle.write(f"VISIT {json.dumps(record)}\n")

    def _start_load(self, target: Target, runner_factory: Callable[[], Runner]) -> tuple:
        """Hybrid mode: start the HTTP load users and let them run for a few seconds."""
        config = self.config
        load_dir = target.run_dir / LOAD_DIR
        runner = runner_factory()
        self._load_runner = runner
        self._add_log(f"Starting background load: {config.load_users} HTTP users on {target.url}")
        thread = threading.Thread(
            target=runner.run, name="pageload-load",
            args=(MODES["api"].script, load_k6_vars(config, target.url, load_dir), load_dir),
            kwargs={"watchdog_s": config.time_limit_s() + config.graceful_stop_s + 120,
                    "on_line": lambda line: None},
            daemon=True)
        thread.start()
        deadline = self._clock() + self._load_warmup_s
        while self._clock() < deadline and not self._stop_requested and thread.is_alive():
            time.sleep(0.2)
        return runner, thread

    def _stop_load(self, load: tuple | None) -> None:
        if not load:
            return
        runner, thread = load
        runner.stop()
        thread.join(30)
        self._load_runner = None

    @staticmethod
    def _save_machine(run_dir: Path, monitor: Any) -> None:
        try:
            summary = monitor.summary()
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / MACHINE_JSON).write_text(json.dumps(summary), encoding="utf-8")
        except Exception as error:  # noqa: BLE001 - only diagnostics
            log.warning("Could not save the test computer load: %s", error)

    def stop(self) -> None:
        self._stop_requested = True
        for runner in (self._runner, self._load_runner):
            if runner:
                runner.stop()

    def _add_log(self, line: str) -> None:
        self.log.append(line)
        del self.log[:-LOG_LINES]

    # -- reporting -----------------------------------------------------------------
    def progress(self, index: int) -> int:
        run_dir = self.targets[index].run_dir
        if self.config.burst:
            bursts = run_dir / "bursts"
            return sum(count_visits(d) for d in bursts.iterdir()) if bursts.is_dir() else 0
        return count_visits(run_dir)

    def _phase(self, index: int, target: Target, done: int) -> str:
        if target.finished and target.problem:
            return "failed" if done == 0 else "incomplete"
        if target.finished:
            return "done"
        if self.is_running:
            return "running" if index == self.current else "waiting"
        return self.state.value

    def _burst_stats(self, target: Target) -> dict[str, Any]:
        data = load_run(target.run_dir)
        stats = analysis.target_stats(data.visits, data.summary, self.mode)
        stats["burst"] = target.burst
        stats["diagnosis"] = None
        stats["load"] = None
        stats["machine"] = {k: v for k, v in (data.machine or {}).items() if k != "samples"} or None
        report = target.run_dir / REPORT_HTML
        stats["report"] = f"/reports/{target.run_dir.name}/{REPORT_HTML}" if report.exists() else None
        stats["reportPath"] = str(report) if report.exists() else None
        stats["download"] = f"/api/runs/{target.run_dir.name}/download"
        target.stats = stats
        return stats

    def status(self) -> dict[str, Any]:
        """Everything the GUI, the API and the command line show about this run."""
        config, mode = self.config, self.mode
        targets = []
        for index, target in enumerate(self.targets):
            done = self.progress(index)
            stats = target.stats
            if stats is None and target.finished and config.burst:
                stats = self._burst_stats(target)
            elif stats is None and target.finished and done > 0:
                data = load_run(target.run_dir)
                stats = analysis.target_stats(data.visits, data.summary, mode)
                load_visits = load_run(target.run_dir / LOAD_DIR).visits if config.load_users else []
                stats["load"] = analysis.load_stats(load_visits, config.load_users)
                stats["diagnosis"] = diagnosis.diagnose(
                    data.visits, data.summary, mode=mode, vus=config.measuring_users, expected=config.iterations,
                    timeout_ms=config.settings.timeout_s * 1000, other_lines=data.other_lines,
                    machine=data.machine, load=stats["load"])
                stats["machine"] = {k: v for k, v in (data.machine or {}).items() if k != "samples"} or None
                report = target.run_dir / REPORT_HTML
                stats["report"] = f"/reports/{target.run_dir.name}/{REPORT_HTML}" if report.exists() else None
                stats["reportPath"] = str(report) if report.exists() else None
                stats["download"] = f"/api/runs/{target.run_dir.name}/download"
                target.stats = stats
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
        if config.burst:
            pass    # bursts are compared by how many users each site handled (burstComparison)
        elif len(targets) == 2 and all(t["stats"] for t in targets):
            comparison = analysis.compare(targets[0]["url"], targets[0]["stats"],
                                          targets[1]["url"], targets[1]["stats"], mode)
        if config.burst:
            ok = self.state is JobState.DONE and all(
                t["stats"] and t["stats"]["burst"]["rounds"]
                and all(r["verdict"] == "ok" for r in t["stats"]["burst"]["rounds"]) for t in targets)
        else:
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
            "browsers": config.browsers,
            "loadUsers": config.load_users,
            "iterations": config.iterations,
            "warnings": config.warnings(),
            "waitUntil": config.wait_until,
            "elapsedMs": ((self.finished or self._clock()) - self.started) * 1000,
            "targets": targets,
            "comparison": comparison,
            "burst": config.burst.to_dict() if config.burst else None,
            "burstComparison": _burst_comparison(targets) if config.burst else None,
            "log": self.log[-40:],
        }


def _burst_comparison(targets: list[dict[str, Any]]) -> str | None:
    """One sentence comparing how many simultaneous users each site handled."""
    results = [(host_of(t["url"]), (t.get("stats") or {}).get("burst") or {}) for t in targets]
    if len(results) != 2 or not all(r.get("steps") for _, r in results):
        return None
    (a, ra), (b, rb) = results
    ha, hb = ra.get("handledUsers") or 0, rb.get("handledUsers") or 0
    if ha == hb:
        return f"{a} and {b} handled the same bursts: up to {ha} users at once." if ha else \
            f"Neither {a} nor {b} handled every round of the smallest burst."
    better, worse = ((a, ha), (b, hb)) if ha > hb else ((b, hb), (a, ha))
    return (f"{better[0]} handled bursts of up to {better[1]} users at once; "
            + (f"{worse[0]} only up to {worse[1]}." if worse[1] else f"{worse[0]} not even the smallest burst."))
