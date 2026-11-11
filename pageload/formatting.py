"""Human-readable numbers, shared by the command line and the HTML report."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse


def ms(value: float | None) -> str:
    """1234.5 -> "1235 ms"; values of 10 s or more as seconds."""
    if value is None:
        return "—"
    return f"{value / 1000:.2f} s" if value >= 10000 else f"{round(value)} ms"


def size(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} MB"
    return f"{value / 1024:.0f} KB"


def duration(value_ms: float | None) -> str:
    """Run time: "1m 12s" or "45s"."""
    if value_ms is None:
        return "—"
    seconds = round(value_ms / 1000)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def local_time(iso: str | None) -> str:
    """ISO timestamp -> local "HH:MM:SS"."""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return iso or "—"


def host_of(url: str) -> str:
    return urlparse(url).hostname or "site"
