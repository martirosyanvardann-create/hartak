"""Statistics and comparisons. Pure functions: no files, no processes.

This module is the only place for the comparison rules ("3.2× slower", the
result sentence, the performance budget). The GUI, the command line and the
HTTP API all show what these functions return.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from .formatting import host_of
from .modes import TestMode


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolated percentile (same method as k6)."""
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    low, high = math.floor(k), math.ceil(k)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def describe(values: Sequence[float]) -> dict[str, float | None]:
    """Median, p90, p95, average, min and max of a list of numbers."""
    return {
        "med": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "avg": sum(values) / len(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def _values(visits: Iterable[dict[str, Any]], key: str) -> list[float]:
    return [visit[key] for visit in visits if visit.get(key) is not None]


def target_stats(visits: list[dict[str, Any]], summary: dict[str, Any], mode: TestMode) -> dict[str, Any]:
    """Statistics for one URL's visits."""
    timings = {t.field: {"label": t.label, **describe(_values(visits, t.field))} for t in mode.timings}
    runtime = summary.get("totalRuntimeMs")
    passed = sum(1 for visit in visits if visit.get("passed"))
    return {
        "visits": len(visits),
        "passed": passed,
        "failed": len(visits) - passed,
        "runtimeMs": runtime,
        "visitsPerMin": len(visits) / (runtime / 60000) if runtime else None,
        "requestsMed": percentile(_values(visits, "requests"), 50),
        "transferMed": percentile(_values(visits, "transferBytes"), 50),
        "timings": timings,
        "errors": [{"visit": v.get("visit"), "error": v.get("error")} for v in visits if v.get("error")][:20],
    }


def diff(a: float | None, b: float | None) -> dict[str, Any]:
    """How ``b`` did compared with ``a``; lower is better.

    Always shows the real difference: "3% slower", "3.2× slower",
    "the same" only when exactly equal, "—" when not comparable.
    """
    if a is None or b is None or a == 0:
        return {"verdict": None, "ratio": None, "text": "—"}
    if a == b:
        return {"verdict": "same", "ratio": 1.0, "text": "the same"}
    ratio = b / a

    def pct(x: float) -> str:
        return f"{max(1, round(x * 100))}%"

    if ratio > 1:
        text = f"{ratio:.1f}× slower" if ratio >= 2 else f"{pct(ratio - 1)} slower"
        return {"verdict": "slower", "ratio": ratio, "text": text}
    inverse = a / b if b else math.inf
    text = f"{inverse:.1f}× faster" if b and inverse >= 2 else f"{pct(1 - ratio)} faster"
    return {"verdict": "faster", "ratio": ratio, "text": text}


def _size_diff(a: float | None, b: float | None) -> dict[str, Any]:
    result = diff(a, b)
    result["text"] = result["text"].replace("slower", "larger").replace("faster", "smaller")
    return result


def result_summary(host_a: str, host_b: str, verdicts: list[str | None]) -> str:
    """One sentence about what actually happened, naming both sites."""
    counts = {"slower": 0, "faster": 0, "same": 0}
    for verdict in verdicts:
        if verdict:
            counts[verdict] += 1
    compared = sum(counts.values())
    if not compared:
        return "Not enough data to compare the timings."
    measured = "" if compared == len(verdicts) else " measured"
    if counts["slower"] == compared:
        return f"{host_b} was slower than {host_a} on all {compared}{measured} timings (median)."
    if counts["faster"] == compared:
        return f"{host_b} was faster than {host_a} on all {compared}{measured} timings (median)."
    parts = [f"{word} on {counts[key]}" for key, word in
             (("slower", "slower"), ("faster", "faster"), ("same", "the same")) if counts[key]]
    listed = ", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 1 else parts[0]
    return f"Compared with {host_a}, {host_b} was {listed} of {compared}{measured} timings (median)."


def compare(url_a: str, stats_a: dict[str, Any], url_b: str, stats_b: dict[str, Any],
            mode: TestMode) -> dict[str, Any]:
    """Side-by-side comparison of two URLs: rows, headline and result sentence."""
    host_a, host_b = host_of(url_a), host_of(url_b)
    rows = []
    for timing in mode.timings:
        x, y = stats_a["timings"][timing.field], stats_b["timings"][timing.field]
        rows.append({
            "metric": timing.field,
            "label": timing.label,
            "a": {"median": x["med"], "p95": x["p95"]},
            "b": {"median": y["med"], "p95": y["p95"]},
            "median": diff(x["med"], y["med"]),
            "p95": diff(x["p95"], y["p95"]),
        })
    extra_rows = [
        {"metric": "runtimeMs", "label": "Run time",
         "a": {"value": stats_a["runtimeMs"]}, "b": {"value": stats_b["runtimeMs"]},
         "value": diff(stats_a["runtimeMs"], stats_b["runtimeMs"])},
        {"metric": "transferMed", "label": "Response size" if mode.name == "api" else "Transferred",
         "a": {"value": stats_a["transferMed"]}, "b": {"value": stats_b["transferMed"]},
         "value": _size_diff(stats_a["transferMed"], stats_b["transferMed"])},
    ]

    main = next(row for row in rows if row["metric"] == mode.main_metric)
    verdict = main["median"]
    if verdict["verdict"] == "same":
        headline = f"{host_b} is as fast as {host_a} {mode.action}."
    elif verdict["verdict"]:
        headline = f"{host_b} is {verdict['text']} than {host_a} {mode.action}."
    else:
        headline = f"{host_b} and {host_a} cannot be compared."

    return {
        "a": host_a,
        "b": host_b,
        "headline": headline,
        "summary": result_summary(host_a, host_b, [row["median"]["verdict"] for row in rows]),
        "main": {"a": main["a"]["median"], "b": main["b"]["median"], **verdict},
        "rows": rows + extra_rows,
    }


def budget_check(stats: dict[str, Any] | None, mode: TestMode, limit_ms: int) -> dict[str, Any] | None:
    """Performance budget: the main timing's p95 must be at most ``limit_ms``."""
    if not limit_ms or not stats:
        return None
    p95 = stats["timings"][mode.main_metric]["p95"]
    return {"limitMs": limit_ms, "p95Ms": p95, "passed": p95 is not None and p95 <= limit_ms}
