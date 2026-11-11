"""Downloads: one URL's run, or a whole run/comparison, as a ZIP.

Run ZIP (one URL)                     Job ZIP (one or two URLs)
  <run folder>/report.html              comparison.html  (or summary.html for one URL)
  <run folder>/visits.csv               comparison.csv   (or timings.csv)
  <run folder>/console.log              result.json      (everything the GUI shows)
  <run folder>/summary.json             <run folder A>/…  (as in the run ZIP)
  <run folder>/screenshots/…            <run folder B>/…

Ids and folder names come from URLs, so they are checked strictly and the
files must lie inside the reports folder.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from . import diagnosis, diagnosis_html
from .formatting import duration, host_of, ms, size
from .job import job_result_path
from .modes import get_mode
from .results import CONSOLE_LOG, load_run

_JOB_ID = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
_RUN_FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
VISITS_CSV = "visits.csv"


# -- locating things ---------------------------------------------------------------
def find_job_result(reports_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Saved result of a finished run, or None."""
    if not _JOB_ID.match(job_id or ""):
        return None
    path = job_result_path(reports_dir, job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def find_run_dir(reports_dir: Path, folder: str) -> Path | None:
    """A run folder inside the reports folder, or None."""
    if not _RUN_FOLDER.match(folder or "") or ".." in folder:
        return None
    root = reports_dir.resolve()
    run_dir = (root / folder).resolve()
    if run_dir.parent != root or not (run_dir / CONSOLE_LOG).is_file():
        return None
    return run_dir


# -- CSV ---------------------------------------------------------------------------------
def visits_csv(run_dir: Path) -> str:
    """One row per visit/request, ready for Excel or Numbers."""
    data = load_run(run_dir)
    mode = get_mode(data.summary.get("mode"))
    columns = (["visit", "vu", "startedAt", "url", "httpStatus"]
               + [t.field for t in mode.timings]
               + ["requests", "transferBytes", "durationMs", "passed", "failureType", "error",
                  "documentStatus", "server", "serverAnswer"]
               + (["method"] if mode.name == "api" else ["failedRequestCount", "screenshot"]))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for visit in data.visits:
        row = {key: _csv_value(visit.get(key)) for key in columns}
        kind = diagnosis.classify(visit)
        row["failureType"] = diagnosis.CATEGORIES[kind].label if kind else ""
        row["server"] = (visit.get("responseHeaders") or {}).get("server") or ""
        row["serverAnswer"] = diagnosis.readable_body(visit.get("bodySnippet"))
        writer.writerow(row)
    return out.getvalue()


def run_diagnosis(run_dir: Path) -> dict[str, Any]:
    data = load_run(run_dir)
    return diagnosis.diagnose(data.visits, data.summary, mode=get_mode(data.summary.get("mode")),
                              other_lines=data.other_lines)


def problems_csv(items: list[tuple[str, dict[str, Any] | None]]) -> str:
    """Possible reasons, one row per finding: [(url, diagnosis), ...]."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["url", "level", "side", "title", "detail", "evidence", "possible reasons", "what to check"])
    for url, diag in items:
        for f in (diag or {}).get("findings", []):
            writer.writerow([url, f["level"], f["side"], f["title"], f["detail"], " | ".join(f["evidence"]),
                             " | ".join(f["reasons"]), " | ".join(f["checks"])])
    return out.getvalue()


def _csv_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 1)
    if value is None:
        return ""
    return value


def results_csv(status: dict[str, Any]) -> str:
    """The comparison table (two URLs) or the timings table (one URL)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    comparison = status.get("comparison")
    if comparison:
        a, b = comparison["a"], comparison["b"]
        writer.writerow(["metric", "label", f"{a} median ms", f"{b} median ms", f"{b} vs {a} (median)",
                         f"{a} p95 ms", f"{b} p95 ms", f"{b} vs {a} (p95)"])
        for row in comparison["rows"]:
            if "median" in row:
                writer.writerow([row["metric"], row["label"], _csv_value(row["a"]["median"]), _csv_value(row["b"]["median"]),
                                 row["median"]["text"], _csv_value(row["a"]["p95"]), _csv_value(row["b"]["p95"]),
                                 row["p95"]["text"]])
            else:
                writer.writerow([row["metric"], row["label"], _csv_value(row["a"]["value"]), _csv_value(row["b"]["value"]),
                                 row["value"]["text"], "", "", ""])
        return out.getvalue()

    writer.writerow(["url", "metric", "label", "median ms", "p90 ms", "p95 ms", "average ms", "min ms", "max ms"])
    for target in status.get("targets", []):
        stats = target.get("stats")
        if not stats:
            continue
        for field, label in status.get("timings", []):
            t = stats["timings"][field]
            writer.writerow([target["url"], field, label] + [_csv_value(t[k]) for k in ("med", "p90", "p95", "avg", "min", "max")])
    return out.getvalue()


# -- standalone summary page ----------------------------------------------------------
def summary_html(status: dict[str, Any]) -> str:
    """A self-contained page with the result (no app needed to open it)."""
    e = html.escape
    comparison = status.get("comparison")
    noun, main_label = status.get("noun", "visits"), status.get("mainLabel", "")
    title = (f"{comparison['b']} vs {comparison['a']}" if comparison
             else " · ".join(host_of(t["url"]) for t in status.get("targets", [])) or "Run")

    cards = []
    for i, target in enumerate(status.get("targets", [])):
        stats = target.get("stats")
        side = "b" if i else "a"
        body = f'<div class="problem">{e(target["problem"])}</div>' if target.get("problem") else ""
        if stats:
            main = stats["timings"][status["mainMetric"]]
            budget = target.get("budget")
            body = (
                f'<dl><dt>{e(main_label)} · median</dt><dd>{ms(main["med"])}</dd>'
                f'<dt>{e(main_label)} · p95</dt><dd>{ms(main["p95"])}</dd>'
                f'<dt>Passed</dt><dd>{stats["passed"]} / {target["total"]}</dd>'
                f'<dt>Server response · median</dt><dd>{ms(stats["timings"]["ttfbMs"]["med"])}</dd>'
                f'<dt>Run time</dt><dd>{duration(stats["runtimeMs"])}</dd>'
                f'<dt>Size · median</dt><dd>{size(stats["transferMed"])}</dd></dl>'
                + (f'<div class="{"ok" if budget["passed"] else "problem"}">Budget: p95 {ms(budget["p95Ms"])} '
                   f'{"≤" if budget["passed"] else ">"} {ms(budget["limitMs"])}</div>' if budget else "")
                + body
                + f'<p class="files">Details: <code>{e(target["runFolder"])}/report.html</code></p>'
            )
        cards.append(f'<section class="card {side}"><h2>{e(host_of(target["url"]))}</h2>'
                     f'<p class="url">{e(target["url"])}</p>{body}</section>')

    if comparison:
        a, b = e(comparison["a"]), e(comparison["b"])
        rows = "".join(
            f'<tr class="{"main" if row["metric"] == status["mainMetric"] else ""}"><td>{e(row["label"])}</td>'
            f'<td>{ms(row["a"]["median"])}</td><td>{ms(row["b"]["median"])}</td>'
            f'<td class="{row["median"]["verdict"] or "same"}">{e(row["median"]["text"])}</td>'
            f'<td>{ms(row["a"]["p95"])}</td><td>{ms(row["b"]["p95"])}</td>'
            f'<td class="{row["p95"]["verdict"] or "same"}">{e(row["p95"]["text"])}</td></tr>'
            for row in comparison["rows"] if "median" in row
        )
        table = (f'<table><thead><tr><th>Timing</th><th>{a} median</th><th>{b} median</th><th>{b} is…</th>'
                 f'<th>{a} p95</th><th>{b} p95</th><th>{b} is…</th></tr></thead><tbody>{rows}</tbody></table>')
        verdict = (f'<div class="verdict"><b>{e(comparison["headline"])}</b><br>'
                   f'Result: {e(comparison["summary"])}</div>')
    else:
        rows = ""
        for target in status.get("targets", []):
            stats = target.get("stats")
            for field, label in (status.get("timings") or []) if stats else []:
                t = stats["timings"][field]
                rows += (f'<tr class="{"main" if field == status["mainMetric"] else ""}"><td>{e(label)}</td>'
                         + "".join(f"<td>{ms(t[k])}</td>" for k in ("med", "p90", "p95", "avg", "min", "max")) + "</tr>")
        table = ("<table><thead><tr><th>Timing</th><th>Median</th><th>p90</th><th>p95</th><th>Average</th>"
                 f"<th>Min</th><th>Max</th></tr></thead><tbody>{rows}</tbody></table>") if rows else ""
        verdict = ""

    settings = status.get("settings") or {}
    meta = [
        ("Run", status.get("id", "—")),
        ("Mode", "API · " + (status.get("method") or "GET") if status.get("mode") == "api" else "Browser"),
        ("Users", status.get("vus")),
        (noun.capitalize() + " per URL", status.get("iterations")),
        ("State", status.get("state")),
        ("Timeout", f'{settings.get("timeoutSec", 30)} s'),
    ]
    if status.get("mode") != "api":
        meta.append(("Screen", f'{settings.get("viewportWidth")}×{settings.get("viewportHeight")}'))
    if settings.get("budgetP95Ms"):
        meta.append(("Budget p95", f'{settings["budgetP95Ms"]} ms'))
    meta_html = "".join(f"<span>{e(str(k))} <b>{e(str(v))}</b></span>" for k, v in meta)
    warnings = "".join(f'<div class="warn">⚠ {e(w)}</div>' for w in status.get("warnings") or [])

    reasons = "".join(
        f'<h2 class="reasons">Possible reasons · {e(host_of(t["url"]))}</h2>'
        + diagnosis_html.render(t["stats"]["diagnosis"], noun)
        for t in status.get("targets", [])
        if (t.get("stats") or {}).get("diagnosis") and t["stats"]["diagnosis"].get("findings"))
    return _SUMMARY_TEMPLATE.format(title=e(title), meta=meta_html, warnings=warnings, verdict=verdict,
                                    cards="".join(cards), table=table, reasons=reasons)


_SUMMARY_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Page Load Compare</title>
<style>
:root {{ --navy: #0b1f3a; --a: #1e40af; --b: #d97706; --text: #0f172a; --muted: #5b6b82; --border: #dce2ec;
  --bg: #f3f5f9; --good: #15803d; --bad: #b91c1c; --warn: #92400e; --warn-soft: #fef3c7; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system, "Segoe UI", sans-serif; }}
header {{ background: var(--navy); color: #fff; padding: 20px 16px; }}
header h1 {{ margin: 0; font-size: 20px; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 20px 16px 40px; }}
.meta {{ display: flex; flex-wrap: wrap; gap: 4px 18px; color: #c9d5ea; margin-top: 6px; font-size: 13px; }}
.meta b {{ color: #fff; }}
.verdict {{ background: #fff; border: 1px solid var(--border); border-left: 4px solid var(--navy); border-radius: 10px; padding: 14px 16px; margin-bottom: 16px; }}
.warn {{ background: var(--warn-soft); color: var(--warn); border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }}
.cards {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr)); }}
.card {{ background: #fff; border: 1px solid var(--border); border-top: 4px solid var(--a); border-radius: 10px; padding: 14px 16px; }}
.card.b {{ border-top-color: var(--b); }}
.card h2 {{ margin: 0; font-size: 16px; }}
.url {{ margin: 2px 0 10px; color: var(--muted); font-size: 12px; word-break: break-all; }}
dl {{ display: grid; grid-template-columns: auto 1fr; gap: 4px 16px; margin: 0; }}
dt {{ color: var(--muted); }} dd {{ margin: 0; font-weight: 600; text-align: right; }}
.problem {{ margin-top: 10px; color: var(--bad); font-weight: 600; }}
.ok {{ margin-top: 10px; color: var(--good); font-weight: 600; }}
.files {{ margin: 10px 0 0; color: var(--muted); font-size: 12px; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; margin-top: 16px; border: 1px solid var(--border); font-variant-numeric: tabular-nums; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: right; }}
th:first-child, td:first-child {{ text-align: left; }}
th {{ font-size: 12px; color: var(--muted); }}
tr.main td {{ font-weight: 700; background: #f8f9fc; }}
td.slower {{ color: var(--bad); }} td.faster {{ color: var(--good); }}
.table-wrap {{ overflow-x: auto; }}
h2.reasons {{ font-size: 16px; margin: 28px 0 10px; }}
.dx h3 {{ font-size: 14px; margin: 18px 0 8px; }}
.dx table {{ margin-top: 0; }} .dx td, .dx th {{ text-align: left; }} .dx td.n, .dx th.n {{ text-align: right; }}
.dx .desc {{ color: var(--muted); font-size: 12px; }} .dx .note {{ color: var(--bad); font-size: 12px; }}
.dx .panel {{ background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 10px; }}
</style>
</head>
<body>
<header><h1>{title}</h1><div class="meta">{meta}</div></header>
<main>
{warnings}
{verdict}
<div class="cards">{cards}</div>
<div class="table-wrap">{table}</div>
{reasons}
</main>
</body>
</html>
"""


# -- ZIP files ------------------------------------------------------------------------
def _add_run_folder(archive: zipfile.ZipFile, run_dir: Path) -> None:
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            archive.write(path, f"{run_dir.name}/{path.relative_to(run_dir).as_posix()}")
    archive.writestr(f"{run_dir.name}/{VISITS_CSV}", visits_csv(run_dir))
    archive.writestr(f"{run_dir.name}/problems.csv", problems_csv([(run_dir.name, run_diagnosis(run_dir))]))


def run_zip(run_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        _add_run_folder(archive, run_dir)
    return buffer.getvalue()


def job_zip(status: dict[str, Any], reports_dir: Path) -> bytes:
    compare = bool(status.get("comparison"))
    for target in status.get("targets", []):     # results saved before diagnoses existed
        stats = target.get("stats")
        run_dir = find_run_dir(reports_dir, target.get("runFolder", ""))
        if stats and "diagnosis" not in stats and run_dir:
            stats["diagnosis"] = run_diagnosis(run_dir)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("comparison.html" if compare else "summary.html", summary_html(status))
        archive.writestr("comparison.csv" if compare else "timings.csv", results_csv(status))
        archive.writestr("result.json", json.dumps(status, indent=2, default=str))
        archive.writestr("problems.csv", problems_csv(
            [(t["url"], (t.get("stats") or {}).get("diagnosis")) for t in status.get("targets", [])]))
        for target in status.get("targets", []):
            run_dir = find_run_dir(reports_dir, target.get("runFolder", ""))
            if run_dir:
                _add_run_folder(archive, run_dir)
    return buffer.getvalue()


# -- file names ------------------------------------------------------------------------
def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "-", text).strip("-") or "site"


def job_zip_name(status: dict[str, Any]) -> str:
    hosts = [_safe(host_of(t["url"])) for t in status.get("targets", [])]
    joined = "-vs-".join(reversed(hosts)) if len(hosts) == 2 else (hosts[0] if hosts else "run")
    kind = "comparison" if len(hosts) == 2 else "result"
    return f"pageload-{kind}-{joined}-{status.get('id', 'run')}.zip"


def run_zip_name(run_dir: Path) -> str:
    return f"pageload-run-{_safe(run_dir.name)}.zip"
