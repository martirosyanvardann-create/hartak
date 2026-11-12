"""The test computer itself: its capacity and how loaded it was during a run.

Besides CPU and memory, the monitor records how fast this computer downloads
(Mbit/s). When the site sends large pages, the test computer's own connection
(e.g. a phone hotspot) can be the limit instead of the server.

In browser mode every user is a real Chrome page on *this* computer. When the
computer runs out of CPU or memory, pages time out or get slow although the
site is fine. So every run records CPU and memory once a second
(:class:`MachineMonitor`), and the recommended number of browser users is
derived from the computer's cores and memory (:func:`recommended_browsers`).

Standard library only. Linux (including Docker) reads /proc and cgroups;
macOS uses sysctl, ps and vm_stat.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

CPU_BUSY = 90.0          # % of all cores: the computer is saturated
MEMORY_FULL = 95.0       # % of memory in use (macOS keeps ~75% in use even when idle)
GB = 1024 ** 3
NET_NEAR_MAX = 0.75      # samples at 75 %+ of the highest download speed count as "at the ceiling"
CHROME_PAGE_GB = 0.35    # memory one browser user needs for a typical page
BROWSER_MAX_RECOMMENDED = 20


# -- capacity ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Capacity:
    cores: float
    memory_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"cores": self.cores, "memoryGb": round(self.memory_bytes / GB, 1)}


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _sysctl(name: str) -> str | None:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _cgroup_cores() -> float | None:
    quota = _read("/sys/fs/cgroup/cpu.max")          # "max 100000" or "200000 100000"
    if quota and not quota.startswith("max"):
        limit, period = quota.split()[:2]
        return int(limit) / int(period)
    return None


def _cgroup_memory_limit() -> int | None:
    value = _read("/sys/fs/cgroup/memory.max")
    return int(value) if value and value.isdigit() else None


def _meminfo() -> dict[str, int]:
    info = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        name, _, rest = line.partition(":")
        number = rest.strip().split(" ")[0]
        if number.isdigit():
            info[name] = int(number) * 1024
    return info


def capacity() -> Capacity:
    """CPU cores and memory available to this process (respects Docker limits)."""
    try:
        cores: float = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        cores = os.cpu_count() or 1
    cores = min(cores, _cgroup_cores() or cores)

    memory = 0
    if platform.system() == "Darwin":
        memory = int(_sysctl("hw.memsize") or 0)
    else:
        memory = _meminfo().get("MemTotal", 0)
        limit = _cgroup_memory_limit()
        if limit:
            memory = min(memory or limit, limit)
    return Capacity(cores=cores, memory_bytes=memory or 8 * GB)


def recommended_browsers(cap: Capacity | None = None) -> int:
    """How many browser users this computer can run without distorting the timings.

    About one CPU core and ~350 MB per Chrome page, keeping 1 GB for everything
    else. PAGELOAD_RECOMMENDED_BROWSERS overrides it.
    """
    configured = os.environ.get("PAGELOAD_RECOMMENDED_BROWSERS", "")
    if configured.isdigit() and int(configured) > 0:
        return int(configured)
    cap = cap or capacity()
    by_cpu = int(cap.cores)
    by_memory = int((cap.memory_bytes / GB - 1) / CHROME_PAGE_GB)
    return max(2, min(by_cpu, by_memory, BROWSER_MAX_RECOMMENDED))


# -- usage sampling ---------------------------------------------------------------------
class _LinuxSampler:
    def __init__(self) -> None:
        self._last = self._cpu_times()

    @staticmethod
    def _cpu_times() -> tuple[int, int] | None:
        line = (_read("/proc/stat") or "").splitlines()[:1]
        if not line:
            return None
        values = [int(v) for v in line[0].split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)   # idle + iowait
        return sum(values), idle

    def cpu(self) -> float | None:
        now = self._cpu_times()
        last, self._last = self._last, now
        if not now or not last or now[0] == last[0]:
            return None
        total, idle = now[0] - last[0], now[1] - last[1]
        return max(0.0, min(100.0, 100.0 * (total - idle) / total))

    @staticmethod
    def received_bytes() -> int | None:
        """Bytes received on all network interfaces except loopback (in Docker: this container)."""
        total = 0
        for line in (_read("/proc/net/dev") or "").splitlines()[2:]:
            name, _, data = line.partition(":")
            if name.strip() != "lo" and data.split():
                total += int(data.split()[0])
        return total or None

    @staticmethod
    def memory() -> float | None:
        limit = _cgroup_memory_limit()
        current = _read("/sys/fs/cgroup/memory.current")
        if limit and current and current.isdigit():
            return 100.0 * int(current) / limit
        info = _meminfo()
        if info.get("MemTotal") and "MemAvailable" in info:
            return 100.0 * (1 - info["MemAvailable"] / info["MemTotal"])
        return None


class _MacSampler:
    def __init__(self) -> None:
        self.cores = os.cpu_count() or 1
        self.total = int(_sysctl("hw.memsize") or 0)
        self.interface = self._default_interface()

    @staticmethod
    def _default_interface() -> str | None:
        """The interface internet traffic leaves through (counting others would double-count Docker's)."""
        try:
            out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"interface:\s*(\S+)", out)
        return match[1] if match else None

    def received_bytes(self) -> int | None:
        if not self.interface:
            return None
        try:
            out = subprocess.run(["netstat", "-ibn", "-I", self.interface], capture_output=True, text=True,
                                 timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            if "<Link#" in line:
                return int(line.split()[-5])       # columns end with: Ibytes Opkts Oerrs Obytes Coll
        return None

    def cpu(self) -> float | None:
        try:
            out = subprocess.run(["ps", "-A", "-o", "%cpu="], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        used = sum(float(v) for v in out.split() if re.match(r"^\d+(\.\d+)?$", v))
        return min(100.0, used / self.cores)

    def memory(self) -> float | None:
        try:
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        size = int((re.search(r"page size of (\d+)", out) or [0, 4096])[1])
        pages = {m[1]: int(m[2]) for m in re.finditer(r"^(.+?):\s+(\d+)\.", out, re.M)}
        used = sum(pages.get(k, 0) for k in ("Pages active", "Pages wired down", "Pages occupied by compressor"))
        return 100.0 * used * size / self.total if self.total else None


def _sampler():
    return _MacSampler() if platform.system() == "Darwin" else _LinuxSampler()


class MachineMonitor:
    """Samples CPU and memory every ``interval_s`` seconds in a background thread.

        with MachineMonitor() as monitor:
            ...run the test...
        monitor.summary()   # {"cores": .., "cpuMax": .., "busyShare": .., "samples": [...]}
    """

    def __init__(self, interval_s: float = 1.0, sampler_factory: Callable[[], Any] = _sampler,
                 clock: Callable[[], float] = time.monotonic):
        self.interval_s = interval_s
        self._sampler_factory = sampler_factory
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # (seconds, cpu %, memory %, download Mbit/s)
        self.samples: list[tuple[float, float | None, float | None, float | None]] = []

    def __enter__(self) -> "MachineMonitor":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        sampler = self._sampler_factory()
        started = self._clock()
        received = getattr(sampler, "received_bytes", lambda: None)

        def loop() -> None:
            last_bytes, last_time = received(), self._clock()
            while not self._stop.wait(self.interval_s):
                try:
                    now, now_bytes = self._clock(), received()
                    mbps = None
                    if now_bytes is not None and last_bytes is not None and now > last_time:
                        mbps = max(0, now_bytes - last_bytes) * 8 / 1_000_000 / (now - last_time)
                    last_bytes, last_time = now_bytes, now
                    self.samples.append((round(now - started, 1), sampler.cpu(), sampler.memory(), mbps))
                except Exception:  # noqa: BLE001 - monitoring must never break a run
                    pass

        self._thread = threading.Thread(target=loop, name="machine-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)

    def summary(self) -> dict[str, Any]:
        return summarize(self.samples, capacity())


def summarize(samples: list[tuple], cap: Capacity) -> dict[str, Any]:
    samples = [tuple(s) + (None,) * (4 - len(s)) for s in samples]     # older samples had no network
    cpu = [s[1] for s in samples if s[1] is not None]
    memory = [s[2] for s in samples if s[2] is not None]
    net = [s[3] for s in samples if s[3] is not None]
    ordered = sorted(cpu)
    peak = max(net) if net else None
    return {
        **cap.to_dict(),
        "recommendedBrowsers": recommended_browsers(cap),
        "cpuMax": round(max(cpu), 1) if cpu else None,
        "cpuP90": round(ordered[int(0.9 * (len(ordered) - 1))], 1) if cpu else None,
        "busyShare": round(sum(1 for c in cpu if c >= CPU_BUSY) / len(cpu), 2) if cpu else None,
        "memoryMax": round(max(memory), 1) if memory else None,
        "netMaxMbps": round(peak, 1) if peak is not None else None,
        "netAvgMbps": round(sum(net) / len(net), 1) if net else None,
        # Share of the run spent close to the highest download speed: a flat top means a ceiling.
        "netNearMaxShare": round(sum(1 for n in net if n >= NET_NEAR_MAX * peak) / len(net), 2) if peak else None,
        "samples": [[t, _round(c), _round(m), _round(n, 1)] for t, c, m, n in samples],
    }


def _round(value: float | None, digits: int = 0) -> float | None:
    return None if value is None else round(value, digits) if digits else round(value)


def overloaded(machine: dict[str, Any] | None) -> bool:
    """CPU saturated for at least a quarter of the run, or memory nearly full."""
    if not machine:
        return False
    return (machine.get("busyShare") or 0) >= 0.25 or (machine.get("memoryMax") or 0) >= MEMORY_FULL
