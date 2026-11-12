"""HTML report for one run folder: one row per visit/request.

    python3 -m pageload.report reports/<run-folder>
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

from . import diagnosis, diagnosis_html
from .analysis import describe, load_stats, percentile
from .formatting import duration, local_time, size
from .modes import get_mode
from .results import LOAD_DIR, REPORT_HTML, load_run


def background_load(run_dir: Path) -> dict | None:
    """Hybrid mode: statistics of the background HTTP load users, if this run had them."""
    if not (run_dir / LOAD_DIR).is_dir():
        return None
    visits = load_run(run_dir / LOAD_DIR).visits
    return load_stats(visits, len({v.get("vu") for v in visits})) if visits else None


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{round(value)} ms"


def build_report(run_dir: Path) -> Path:
    """Write ``report.html`` into ``run_dir`` and return its path."""
    data = load_run(run_dir)
    visits, summary = data.visits, data.summary
    load = background_load(run_dir)
    mode = get_mode(summary.get("mode"))
    api = mode.name in ("api", "burst")      # no browser: no viewport, no page requests card
    e = html.escape
    main, main_label, noun = mode.main_metric, mode.main_label, mode.noun

    passed = sum(1 for v in visits if v.get("passed"))
    expected = summary.get("expectedVisits") or len(visits)
    missing = max(0, expected - len(visits))
    main_values = [v[main] for v in visits if v.get(main) is not None]
    max_main = max(main_values) if main_values else 1
    requests = [v["requests"] for v in visits if v.get("requests") is not None]
    transfer = [v["transferBytes"] for v in visits if v.get("transferBytes") is not None]

    stat_rows = []
    for timing in mode.timings:
        stats = describe([v[timing.field] for v in visits if v.get(timing.field) is not None])
        cells = "".join(f'<td class="n">{_ms(stats[k])}</td>' for k in ("med", "p90", "p95", "avg", "min", "max"))
        cls = ' class="main"' if timing.field == main else ""
        stat_rows.append(f'<tr{cls}><td><b>{timing.label}</b><div class="desc">{timing.description}</div></td>{cells}</tr>')

    chart = _chart(visits, main, main_label, max_main)
    timing_heads = "".join(f'<th data-type="num" title="{e(t.description)}">{t.label}</th>' for t in mode.timings)
    rows = "".join(_visit_row(v, mode, max_main) for v in visits)

    url = str(summary.get("url") or (visits[0].get("url") if visits else "—"))
    started = local_time(visits[0]["startedAt"]) if visits else "—"
    all_ok = passed == expected and visits and not missing
    missing_note = (
        f'<div class="warn">{missing} of {expected} {noun} did not finish (they hung or were stopped by the '
        f'time limit) and are not in these results.</div>' if missing else ""
    )
    mode_meta = (
        '<span>Mode <b>Burst: separate users at the same moment (no browser)</b></span>' if mode.name == "burst" else
        f'<span>Mode <b>API · {e(str(summary.get("method") or "GET"))} (no browser)</b></span>' if api else
        f'<span>Wait until <b>{e(str(summary.get("waitUntil") or "—"))}</b></span>'
        + (f'<span>Hybrid <b>{summary.get("vus")} browsers + {load["users"]} HTTP load users</b></span>'
           if load else "") +
        f'<span>Viewport <b>{e(str(summary.get("viewport") or "—"))}</b></span>'
    )
    requests_card = "" if api else (
        f'<div class="card"><div class="label">Requests · median</div>'
        f'<div class="value">{round(percentile(requests, 50)) if requests else "—"}</div></div>'
    )

    burst_file = run_dir / "burst.json"
    if mode.name == "burst" and burst_file.exists():
        # Rounds with pauses in between: the burst table says more than a run-wide diagnosis.
        diag_section = (diagnosis_html.render_burst(json.loads(burst_file.read_text(encoding="utf-8")))
                        + f'<section class="dx">{diagnosis_html.machine_section(data.machine)}</section>')
    else:
        diag = diagnosis.diagnose(visits, summary, mode=mode, other_lines=data.other_lines, machine=data.machine,
                                  load=load)
        diag_section = diagnosis_html.render(diag, noun)

    titles = {"api": "API Response Report", "burst": "Burst Test Report"}
    page = _TEMPLATE.format(
        diagnosis=diag_section,
        reasons_title="Bursts" if mode.name == "burst" else "Possible reasons",
        title=titles.get(mode.name, "Page Load Report"),
        result_color="var(--ok)" if all_ok else "var(--bad)",
        url=e(url), run=e(run_dir.name), started=e(started),
        profile=e(str(summary.get("profile") or "—")), mode_meta=mode_meta,
        noun_title=noun.capitalize(), passed=passed, expected=expected,
        runtime=duration(summary.get("totalRuntimeMs")), main_label=main_label,
        main_med=_ms(percentile(main_values, 50)), main_p95=_ms(percentile(main_values, 95)),
        requests_card=requests_card, size_label="Response size" if api else "Transferred",
        size_med=size(percentile(transfer, 50)), missing_note=missing_note,
        stat_rows="".join(stat_rows), noun_singular=mode.noun_singular,
        chart=chart or '<div class="empty">No visits recorded.</div>', timing_heads=timing_heads,
        size_head="Size" if api else "Transferred",
        rows=rows or f'<tr><td colspan="{10 + len(mode.timings)}" class="empty">No visits recorded.</td></tr>',
    )
    out = run_dir / REPORT_HTML
    out.write_text(page, encoding="utf-8")
    return out


def _chart(visits: list[dict], main: str, main_label: str, max_main: float) -> str:
    if not visits:
        return ""
    width, height, pad = 900, 160, 4
    bar_w = max((width - pad * 2) / len(visits), 1)
    bars = []
    for i, v in enumerate(visits):
        x, w = pad + i * bar_w, max(bar_w - 1, 0.5)
        if v.get(main) is None:
            bars.append(f'<rect x="{x:.1f}" y="{pad}" width="{w:.1f}" height="{height - pad * 2}" '
                        f'class="bar-fail"><title>#{v.get("visit")}: failed</title></rect>')
            continue
        h = (v[main] / max_main) * (height - pad * 2)
        bars.append(f'<rect x="{x:.1f}" y="{height - pad - h:.1f}" width="{w:.1f}" height="{h:.1f}" '
                    f'class="bar"><title>#{v.get("visit")}: {round(v[main])} ms</title></rect>')
    return (f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="chart" '
            f'role="img" aria-label="{main_label} time per visit">{"".join(bars)}</svg>')


def _visit_row(v: dict, mode, max_main: float) -> str:
    e = html.escape
    cells = []
    for timing in mode.timings:
        value = v.get(timing.field)
        data_v = value if value is not None else ""
        if timing.field == mode.main_metric and value is not None:
            bar = f'<span class="mini" style="width:{(value / max_main) * 100:.0f}%"></span>'
            cells.append(f'<td class="n" data-v="{data_v}"><div class="ready">{bar}<span>{_ms(value)}</span></div></td>')
        else:
            cells.append(f'<td class="n" data-v="{data_v}">{_ms(value)}</td>')
    note = e(v.get("error") or "")
    if v.get("screenshot"):
        note += f' <a href="{e(v["screenshot"])}">screenshot</a>'
    result = '<span class="pill ok-bg">Pass</span>' if v.get("passed") else '<span class="pill bad-bg">Fail</span>'
    status = v.get("httpStatus")
    requests = v.get("requests")
    return (
        f'<tr><td class="n">{v.get("visit")}</td><td class="n">{v.get("vu")}</td>'
        f'<td>{e(local_time(v.get("startedAt")))}</td>'
        f'<td class="n">{status if status is not None else "—"}</td>{"".join(cells)}'
        f'<td class="n">{requests if requests is not None else "—"}</td>'
        f'<td class="n" data-v="{v.get("transferBytes") or ""}">{size(v.get("transferBytes"))}</td>'
        f'<td class="n" data-v="{v.get("durationMs") or ""}">{_ms(v.get("durationMs"))}</td>'
        f'<td>{result}</td><td class="note">{note}</td></tr>'
    )


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
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
h3 {{ font-size: 13.5px; margin: 20px 0 8px; }}
.meta {{ color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 18px; }}
.meta b {{ color: var(--text); font-weight: 600; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 20px; }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }}
.card .label {{ color: var(--muted); font-size: 12px; }}
.card .value {{ font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }}
.card.result .value {{ color: {result_color}; }}
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
  <h1>{url}</h1>
  <div class="meta">
    <span>Run <b>{run}</b></span>
    <span>Started <b>{started}</b></span>
    <span>Profile <b>{profile}</b></span>
    {mode_meta}
  </div>

  <div class="cards">
    <div class="card result"><div class="label">{noun_title} passed</div><div class="value">{passed} / {expected}</div></div>
    <div class="card"><div class="label">Total runtime</div><div class="value">{runtime}</div></div>
    <div class="card"><div class="label">{main_label} · median</div><div class="value">{main_med}</div></div>
    <div class="card"><div class="label">{main_label} · p95</div><div class="value">{main_p95}</div></div>
    {requests_card}
    <div class="card"><div class="label">{size_label} · median</div><div class="value">{size_med}</div></div>
  </div>

  {missing_note}

  <h2 id="reasons">{reasons_title}</h2>
  {diagnosis}

  <h2>Timings</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Timing</th><th class="n">Median</th><th class="n">p90</th><th class="n">p95</th>
        <th class="n">Average</th><th class="n">Min</th><th class="n">Max</th></tr></thead>
      <tbody>{stat_rows}</tbody>
    </table>
  </div>

  <h2>{main_label} time per {noun_singular}</h2>
  <div class="panel">{chart}</div>

  <h2>{noun_title} <span style="color:var(--muted);font-weight:400">— click a column to sort</span></h2>
  <div class="table-wrap">
    <table id="visits">
      <thead><tr>
        <th data-type="num">#</th><th data-type="num">VU</th><th data-type="text">Started</th>
        <th data-type="num">HTTP</th>{timing_heads}
        <th data-type="num">Requests</th><th data-type="num">{size_head}</th>
        <th data-type="num">Visit time</th><th data-type="text">Result</th><th data-type="text">Error</th>
      </tr></thead>
      <tbody>{rows}</tbody>
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


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: python3 -m pageload.report <run-folder>", file=sys.stderr)
        return 2
    out = build_report(Path(args[0]))
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
