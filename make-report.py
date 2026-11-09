#!/usr/bin/env python3
"""Build an HTML report (one row per visit) from a run-report.sh output folder.

Usage: python3 make-report.py reports/<timestamp>
Reads console.log (VISIT lines) and summary.json, writes report.html.
Standard library only.
"""

import html
import json
import math
import sys
from datetime import datetime
from pathlib import Path

# (visit field, label, short description)
TIMINGS = [
    ("ttfbMs", "TTFB", "first byte from the server"),
    ("fcpMs", "FCP", "first content painted"),
    ("lcpMs", "LCP", "largest content painted"),
    ("domMs", "DOM loaded", "DOMContentLoaded event"),
    ("loadMs", "Load", "browser load event finished"),
    ("fullyLoadedMs", "Fully loaded", "load event or last request, whichever is later"),
]
MAIN = "fullyLoadedMs"

# API mode (apitest.js): plain HTTP requests, no browser.
API_TIMINGS = [
    ("connectMs", "Connect", "opening the TCP connection"),
    ("tlsMs", "TLS", "https handshake"),
    ("ttfbMs", "TTFB", "waiting for the first byte (server time)"),
    ("receiveMs", "Receive", "downloading the answer"),
    ("totalMs", "Total", "whole request, start to finish"),
]
API_MAIN = "totalMs"


def timings_for(mode):
    """(timings, main field, main label) for a run mode."""
    if mode == "api":
        return API_TIMINGS, API_MAIN, "Total"
    return TIMINGS, MAIN, "Fully loaded"


def percentile(values, p):
    """Linear-interpolated percentile, same method as k6."""
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    low, high = math.floor(k), math.ceil(k)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def ms(value):
    return "—" if value is None else f"{round(value)} ms"


def size(value):
    if value is None:
        return "—"
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} MB"
    return f"{value / 1024:.0f} KB"


def duration(value_ms):
    if value_ms is None:
        return "—"
    seconds = round(value_ms / 1000)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def local_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return iso or "—"


def load(run_dir):
    visits = []
    console = run_dir / "console.log"
    if console.exists():
        for line in console.read_text(encoding="utf-8").splitlines():
            if line.startswith("VISIT "):
                try:
                    visits.append(json.loads(line[len("VISIT "):]))
                except json.JSONDecodeError:
                    pass
    visits.sort(key=lambda v: v.get("visit", 0))

    summary_file = run_dir / "summary.json"
    summary = json.loads(summary_file.read_text(encoding="utf-8")) if summary_file.exists() else {}
    return visits, summary


def build(run_dir):
    visits, summary = load(run_dir)
    e = html.escape
    api = summary.get("mode") == "api"
    timings, main, main_label = timings_for(summary.get("mode"))
    noun = "requests" if api else "visits"

    passed = sum(1 for v in visits if v.get("passed"))
    expected = summary.get("expectedVisits") or len(visits)
    missing = max(0, expected - len(visits))
    main_values = [v[main] for v in visits if v.get(main) is not None]
    max_main = max(main_values) if main_values else 1

    # Stats table: one row per timing.
    stat_rows = []
    for field, label, desc in timings:
        values = [v[field] for v in visits if v.get(field) is not None]
        cells = "".join(
            f'<td class="n">{ms(x)}</td>'
            for x in (
                percentile(values, 50),
                percentile(values, 90),
                percentile(values, 95),
                sum(values) / len(values) if values else None,
                min(values) if values else None,
                max(values) if values else None,
            )
        )
        cls = ' class="main"' if field == main else ""
        stat_rows.append(f'<tr{cls}><td><b>{label}</b><div class="desc">{desc}</div></td>{cells}</tr>')

    requests = [v["requests"] for v in visits if v.get("requests") is not None]
    transfer = [v["transferBytes"] for v in visits if v.get("transferBytes") is not None]

    # Bar chart: fully loaded time per visit.
    chart = ""
    if visits:
        width, height, pad = 900, 160, 4
        bar_w = max((width - pad * 2) / len(visits), 1)
        bars = []
        for i, v in enumerate(visits):
            x = pad + i * bar_w
            w = max(bar_w - 1, 0.5)
            if v.get(main) is None:
                bars.append(f'<rect x="{x:.1f}" y="{pad}" width="{w:.1f}" height="{height - pad * 2}" '
                            f'class="bar-fail"><title>Visit {v["visit"]}: failed</title></rect>')
                continue
            h = (v[main] / max_main) * (height - pad * 2)
            bars.append(f'<rect x="{x:.1f}" y="{height - pad - h:.1f}" width="{w:.1f}" height="{h:.1f}" '
                        f'class="bar"><title>Visit {v["visit"]}: {round(v[main])} ms</title></rect>')
        chart = (f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="chart" '
                 f'role="img" aria-label="{main_label} time per visit">{"".join(bars)}</svg>')

    timing_heads = "".join(f'<th data-type="num" title="{e(desc)}">{label}</th>' for _, label, desc in timings)

    rows = []
    for v in visits:
        timing_cells = []
        for field, _, _ in timings:
            value = v.get(field)
            data_v = value if value is not None else ""
            if field == main and value is not None:
                bar = f'<span class="mini" style="width:{(value / max_main) * 100:.0f}%"></span>'
                timing_cells.append(f'<td class="n" data-v="{data_v}"><div class="ready">{bar}<span>{ms(value)}</span></div></td>')
            else:
                timing_cells.append(f'<td class="n" data-v="{data_v}">{ms(value)}</td>')
        note = e(v.get("error") or "")
        if v.get("screenshot"):
            note += f' <a href="../../{e(v["screenshot"])}">screenshot</a>'
        result = '<span class="pill ok-bg">Pass</span>' if v.get("passed") else '<span class="pill bad-bg">Fail</span>'
        rows.append(
            f'<tr><td class="n">{v.get("visit")}</td>'
            f'<td class="n">{v.get("vu")}</td>'
            f'<td>{e(local_time(v.get("startedAt")))}</td>'
            f'<td class="n">{v.get("httpStatus") if v.get("httpStatus") is not None else "—"}</td>'
            f'{"".join(timing_cells)}'
            f'<td class="n">{v.get("requests") if v.get("requests") is not None else "—"}</td>'
            f'<td class="n" data-v="{v.get("transferBytes") or ""}">{size(v.get("transferBytes"))}</td>'
            f'<td class="n" data-v="{v.get("durationMs") or ""}">{ms(v.get("durationMs"))}</td>'
            f'<td>{result}</td><td class="note">{note}</td></tr>'
        )

    url = str(summary.get("url") or (visits[0].get("url") if visits else "—"))
    started = local_time(visits[0]["startedAt"]) if visits else "—"
    all_ok = passed == expected and visits and not missing
    missing_note = (
        f'<div class="warn">{missing} of {expected} {noun} did not finish (they hung or were stopped by the '
        f'time limit) and are not in these results.</div>' if missing else ""
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{"API Response Report" if api else "Page Load Report"}</title>
<style>
:root {{
  --bg: #f7f7f5; --surface: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --border: #e3e2de;
  --accent: #2f6fdb; --ok: #1f8a4c; --bad: #c7362f; --ok-soft: #e3f4ea; --bad-soft: #fbe5e3;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #161615; --surface: #1f1f1d; --text: #ecebe7; --muted: #9b9a94; --border: #34332f;
    --accent: #6b9cf0; --ok: #4cc27f; --bad: #f0675f; --ok-soft: #1c3527; --bad-soft: #3d1f1d;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
main {{ max-width: 1280px; margin: 0 auto; padding: 32px 16px 48px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; word-break: break-all; }}
h2 {{ font-size: 15px; margin: 32px 0 12px; }}
.meta {{ color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 18px; }}
.meta b {{ color: var(--text); font-weight: 600; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 20px; }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }}
.card .label {{ color: var(--muted); font-size: 12px; }}
.card .value {{ font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }}
.card.result .value {{ color: {"var(--ok)" if all_ok else "var(--bad)"}; }}
.panel {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px; }}
.chart {{ width: 100%; height: 160px; display: block; }}
.bar {{ fill: var(--accent); }} .bar-fail {{ fill: var(--bad); opacity: .5; }}
.table-wrap {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; }}
table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left; white-space: nowrap; }}
th {{ font-size: 12px; color: var(--muted); font-weight: 600; background: var(--surface); }}
#visits th {{ cursor: pointer; user-select: none; }}
#visits th:hover {{ color: var(--text); }}
tr:last-child td {{ border-bottom: 0; }}
td.n, th.n {{ text-align: right; }}
tr.main td {{ background: color-mix(in srgb, var(--accent) 8%, transparent); }}
.desc {{ color: var(--muted); font-size: 12px; }}
td.note {{ white-space: normal; color: var(--bad); font-size: 12px; min-width: 200px; }}
td.note a {{ color: var(--accent); }}
.pill {{ padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
.ok-bg {{ background: var(--ok-soft); color: var(--ok); }} .bad-bg {{ background: var(--bad-soft); color: var(--bad); }}
.ready {{ position: relative; min-width: 100px; }}
.ready span:last-child {{ position: relative; font-weight: 600; }}
.mini {{ position: absolute; left: 0; top: 2px; bottom: 2px; background: var(--accent); opacity: .15; border-radius: 3px; }}
.empty {{ padding: 24px; color: var(--muted); }}
.warn {{ margin-top: 16px; padding: 10px 14px; border-radius: 10px; background: var(--bad-soft); color: var(--bad); font-weight: 600; }}
</style>
</head>
<body>
<main>
  <h1>{e(url)}</h1>
  <div class="meta">
    <span>Run <b>{e(run_dir.name)}</b></span>
    <span>Started <b>{e(started)}</b></span>
    <span>Profile <b>{e(str(summary.get("profile", "—")))}</b></span>
    {f'<span>Mode <b>API · {e(str(summary.get("method") or "GET"))} (no browser)</b></span>' if api else
     f'<span>Wait until <b>{e(str(summary.get("waitUntil", "—")))}</b></span><span>Viewport <b>{e(str(summary.get("viewport", "—")))}</b></span>'}
  </div>

  <div class="cards">
    <div class="card result"><div class="label">{noun.capitalize()} passed</div><div class="value">{passed} / {expected}</div></div>
    <div class="card"><div class="label">Total runtime</div><div class="value">{duration(summary.get("totalRuntimeMs"))}</div></div>
    <div class="card"><div class="label">{main_label} · median</div><div class="value">{ms(percentile(main_values, 50))}</div></div>
    <div class="card"><div class="label">{main_label} · p95</div><div class="value">{ms(percentile(main_values, 95))}</div></div>
    {"" if api else f'<div class="card"><div class="label">Requests · median</div><div class="value">{round(percentile(requests, 50)) if requests else "—"}</div></div>'}
    <div class="card"><div class="label">{"Response size" if api else "Transferred"} · median</div><div class="value">{size(percentile(transfer, 50))}</div></div>
  </div>

  {missing_note}

  <h2>Timings</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Timing</th><th class="n">Median</th><th class="n">p90</th><th class="n">p95</th>
        <th class="n">Average</th><th class="n">Min</th><th class="n">Max</th></tr></thead>
      <tbody>{"".join(stat_rows)}</tbody>
    </table>
  </div>

  <h2>{main_label} time per {"request" if api else "visit"}</h2>
  <div class="panel">{chart or '<div class="empty">No visits recorded.</div>'}</div>

  <h2>{noun.capitalize()} <span style="color:var(--muted);font-weight:400">— click a column to sort</span></h2>
  <div class="table-wrap">
    <table id="visits">
      <thead><tr>
        <th data-type="num">#</th><th data-type="num">VU</th><th data-type="text">Started</th>
        <th data-type="num">HTTP</th>{timing_heads}
        <th data-type="num">Requests</th><th data-type="num">{"Size" if api else "Transferred"}</th>
        <th data-type="num">Visit time</th><th data-type="text">Result</th><th data-type="text">Error</th>
      </tr></thead>
      <tbody>{"".join(rows) or f'<tr><td colspan="{10 + len(timings)}" class="empty">No visits recorded.</td></tr>'}</tbody>
    </table>
  </div>
</main>
<script>
document.querySelectorAll('#visits th').forEach((th, col) => {{
  let asc = true;
  th.addEventListener('click', () => {{
    const body = th.closest('table').tBodies[0];
    const num = th.dataset.type === 'num';
    const key = (row) => {{
      const cell = row.cells[col];
      const raw = cell.dataset.v ?? cell.textContent.trim();
      return num ? (raw === '' || isNaN(parseFloat(raw)) ? Infinity : parseFloat(raw)) : raw;
    }};
    [...body.rows].sort((a, b) => {{
      const x = key(a), y = key(b);
      return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1);
    }}).forEach((row) => body.appendChild(row));
    asc = !asc;
  }});
}});
</script>
</body>
</html>
"""
    out = run_dir / "report.html"
    out.write_text(page, encoding="utf-8")
    print(f"Report: {out}  ({passed}/{expected} {noun} passed{f', {missing} did not finish' if missing else ''})")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 make-report.py reports/<timestamp>")
    build(Path(sys.argv[1]))
