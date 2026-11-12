"""Reading a run folder written by the k6 scripts.

A run folder holds:
  console.log    k6 console output; one "VISIT {json}" line per visit/request
  summary.json   written by the script's handleSummary (mode, runtime, ...)
  report.html    written by :mod:`pageload.report`
  machine.json   CPU / memory of the test computer during the run
  screenshots/   failed browser visits (optional)
  load/          hybrid mode: console.log of the background HTTP load users
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VISIT_PREFIX = "VISIT "
CONSOLE_LOG = "console.log"
SUMMARY_JSON = "summary.json"
REPORT_HTML = "report.html"
MACHINE_JSON = "machine.json"     # CPU / memory of the test computer during the run
LOAD_DIR = "load"                 # hybrid mode: the background HTTP load's own run folder


@dataclass
class RunData:
    visits: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    other_lines: list[str] = field(default_factory=list)   # console lines that are not visits
    machine: dict[str, Any] | None = None                  # test computer load (machine.json)


def count_visits(run_dir: Path) -> int:
    """Number of visit records so far (cheap enough to call for progress)."""
    path = run_dir / CONSOLE_LOG
    try:
        with path.open(encoding="utf-8") as handle:
            return sum(1 for line in handle if line.startswith(VISIT_PREFIX))
    except OSError:
        return 0


def load_run(run_dir: Path) -> RunData:
    data = RunData()
    console = run_dir / CONSOLE_LOG
    if console.exists():
        for line in console.read_text(encoding="utf-8").splitlines():
            if line.startswith(VISIT_PREFIX):
                try:
                    data.visits.append(json.loads(line[len(VISIT_PREFIX):]))
                except json.JSONDecodeError:
                    pass
            elif line.strip():
                data.other_lines.append(line)
    data.visits.sort(key=lambda visit: visit.get("visit", 0))

    machine = run_dir / MACHINE_JSON
    if machine.exists():
        try:
            data.machine = json.loads(machine.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data.machine = None

    summary = run_dir / SUMMARY_JSON
    if summary.exists():
        try:
            data.summary = json.loads(summary.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data.summary = {}
    return data
