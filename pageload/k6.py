"""Running one k6 script as a child process."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .process import kill_tree
from .results import CONSOLE_LOG, VISIT_PREFIX

K6_CANDIDATES = ("/opt/homebrew/bin/k6", "/usr/local/bin/k6")
_ERROR_LINE = re.compile(r"error|failed|timed out", re.I)
_NOT_ERRORS = ("Visits:", "Requests:", "thresholds on")


def find_k6() -> str | None:
    return os.environ.get("K6_PATH") or shutil.which("k6") or next(
        (path for path in K6_CANDIDATES if Path(path).exists()), None
    )


@dataclass
class K6Result:
    exit_code: int
    elapsed_s: float
    errors: list[str] = field(default_factory=list)   # error-looking output lines
    watchdog_fired: bool = False                      # k6 stopped responding and was ended
    stopped: bool = False                             # ended by stop()


class K6Runner:
    """Runs a k6 script, streams its output, and can be stopped at any time.

    A watchdog ends k6 if it keeps running well past its own time limit, so a
    caller never waits forever.
    """

    def __init__(self, k6_path: str):
        self.k6_path = k6_path
        self._process: subprocess.Popen | None = None
        self._stopped = False

    def run(self, script: Path, k6_vars: Mapping[str, str], out_dir: Path, *,
            extra_env: Mapping[str, str] | None = None,
            watchdog_s: float | None = None,
            on_line: Callable[[str], None] = lambda line: None) -> K6Result:
        out_dir.mkdir(parents=True, exist_ok=True)
        command = [self.k6_path, "run", "-q", "--log-format", "raw",
                   "--console-output", str(out_dir / CONSOLE_LOG)]
        for name, value in k6_vars.items():
            command += ["-e", f"{name}={value}"]
        command.append(str(script))

        env = {**os.environ, **(extra_env or {})}
        started = time.monotonic()
        errors: list[str] = []
        watchdog_fired = threading.Event()

        # New session: stop() can end k6 and everything it started.
        self._process = subprocess.Popen(
            command, cwd=out_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        watchdog = None
        if watchdog_s:
            watchdog = threading.Timer(watchdog_s, lambda: (watchdog_fired.set(), kill_tree(self._process)))
            watchdog.daemon = True
            watchdog.start()

        def take(line: str) -> None:
            line = line.rstrip()
            if not line or line.startswith(VISIT_PREFIX):
                return
            on_line(line)
            if _ERROR_LINE.search(line) and not line.startswith(_NOT_ERRORS):
                errors.append(line)

        try:
            for line in self._process.stdout:
                take(line)
            exit_code = self._process.wait()
        finally:
            if watchdog:
                watchdog.cancel()

        # k6 writes console messages (errors, visit records) to the console log.
        console = out_dir / CONSOLE_LOG
        if console.exists():
            for line in console.read_text(encoding="utf-8").splitlines():
                take(line)

        return K6Result(exit_code=exit_code, elapsed_s=time.monotonic() - started, errors=errors,
                        watchdog_fired=watchdog_fired.is_set(), stopped=self._stopped)

    def stop(self) -> None:
        self._stopped = True
        kill_tree(self._process)
