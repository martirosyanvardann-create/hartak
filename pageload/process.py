"""Helpers for stopping a process together with everything it started."""

from __future__ import annotations

import os
import signal
import subprocess


def descendants(root_pid: int) -> list[int]:
    """All child, grandchild, … process ids of ``root_pid``."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    children: dict[int, list[int]] = {}
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


def kill_tree(process: subprocess.Popen | None, sig: int = signal.SIGTERM) -> None:
    """Signal a process and all its descendants.

    Chrome puts its helpers in their own process groups, so killing the
    group is not enough: every descendant is signalled by id.
    """
    if process is None or process.poll() is not None:
        return
    for pid in [process.pid, *descendants(process.pid)]:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
