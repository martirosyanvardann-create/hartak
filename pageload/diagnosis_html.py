"""HTML for the "Possible reasons" section (used by the run report and the downloaded summary)."""

from __future__ import annotations

import html
from typing import Any

from .diagnosis import readable_body

_LEVEL_TEXT = {"critical": "Critical", "warning": "Warning", "info": "Note"}

STYLE = """
<style>
.dx { --dx-bad: var(--bad, #c7362f); --dx-warn: #b7791f; --dx-info: var(--accent, #2f6fdb);
  --dx-ok: var(--ok, #1f8a4c); --dx-border: var(--border, #e3e2de); --dx-muted: var(--muted, #6b6b66);
  --dx-surface: var(--surface, #fff); }
@media (prefers-color-scheme: dark) { .dx { --dx-warn: #e0a548; } }
.dx-head { padding: 12px 14px; border-radius: 10px; font-weight: 600; margin-bottom: 12px;
  background: color-mix(in srgb, var(--dx-bad) 12%, transparent); color: var(--dx-bad); }
.dx-head.ok { background: color-mix(in srgb, var(--dx-ok) 12%, transparent); color: var(--dx-ok); }
.dx-finding { background: var(--dx-surface); border: 1px solid var(--dx-border); border-left: 4px solid var(--dx-info);
  border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }
.dx-finding.critical { border-left-color: var(--dx-bad); } .dx-finding.warning { border-left-color: var(--dx-warn); }
.dx-title { font-weight: 650; font-size: 14.5px; }
.dx-tag { display: inline-block; font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 999px;
  margin-right: 6px; vertical-align: 1px; border: 1px solid currentColor; }
.dx-tag.critical { color: var(--dx-bad); } .dx-tag.warning { color: var(--dx-warn); } .dx-tag.info { color: var(--dx-info); }
.dx-tag.side { color: var(--dx-muted); }
.dx-finding p { margin: 6px 0; }
.dx-cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 4px 20px; }
.dx-cols h4 { margin: 8px 0 2px; font-size: 12px; color: var(--dx-muted); text-transform: uppercase; letter-spacing: .03em; }
.dx-cols ul, .dx-evidence { margin: 0; padding-left: 18px; font-size: 13px; }
.dx-evidence { color: var(--dx-muted); }
.dx-evidence li, .dx-cols li { margin: 2px 0; word-break: break-word; }
.dx-timeline { width: 100%; height: 150px; display: block; }
.dx-pass { fill: var(--dx-ok); opacity: .75; } .dx-fail { fill: var(--dx-bad); opacity: .8; }
.dx-axis { display: flex; justify-content: space-between; color: var(--dx-muted); font-size: 11.5px; margin-top: 4px; }
.dx-legend { color: var(--dx-muted); font-size: 12px; margin-top: 6px; }
.dx-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin: 0 4px 0 10px; vertical-align: -1px; }
.dx-machine { width: 100%; height: 70px; display: block; margin-top: 6px; }
.dx-cpu { fill: none; stroke: var(--dx-bad); stroke-width: 2; vector-effect: non-scaling-stroke; }
.dx-mem { fill: none; stroke: var(--dx-info); stroke-width: 2; vector-effect: non-scaling-stroke; }
.dx-limit { stroke: var(--dx-muted); stroke-dasharray: 4 4; vector-effect: non-scaling-stroke; }
.dx table td.wrap { white-space: normal; word-break: break-word; min-width: 220px; }
.dx-note { color: var(--dx-muted); font-size: 12.5px; }
</style>
"""


def render(diag: dict[str, Any] | None, noun: str = "visits") -> str:
    """The whole section; empty string if there is no diagnosis."""
    if not diag:
        return ""
    e = html.escape
    findings = diag.get("findings") or []
    serious = any(f["level"] != "info" for f in findings)
    parts = [STYLE, '<section class="dx">',
             f'<div class="dx-head{"" if serious else " ok"}">{e(diag.get("headline") or "")}</div>']
    if findings:
        parts.append('<p class="dx-note">What the test saw, the most likely reasons and what to check on the '
                     'server to confirm them. The test only sees what a visitor sees: the server\'s own logs show '
                     'the actual cause.</p>')
    for f in findings:
        evidence = "".join(f"<li>{e(x)}</li>" for x in f.get("evidence") or [])
        reasons = "".join(f"<li>{e(x)}</li>" for x in f.get("reasons") or [])
        checks = "".join(f"<li>{e(x)}</li>" for x in f.get("checks") or [])
        cols = ""
        if reasons or checks:
            cols = ('<div class="dx-cols">'
                    + (f"<div><h4>Possible reasons</h4><ul>{reasons}</ul></div>" if reasons else "")
                    + (f"<div><h4>What to check</h4><ul>{checks}</ul></div>" if checks else "")
                    + "</div>")
        parts.append(
            f'<div class="dx-finding {e(f["level"])}">'
            f'<div><span class="dx-tag {e(f["level"])}">{_LEVEL_TEXT.get(f["level"], f["level"])}</span>'
            f'<span class="dx-tag side">{e(f["side"])}</span><span class="dx-title">{e(f["title"])}</span></div>'
            f'<p>{e(f["detail"])}</p>'
            + (f'<ul class="dx-evidence">{evidence}</ul>' if evidence else "")
            + cols + "</div>")

    parts.append(_machine(diag.get("machine")))
    parts.append(_timeline(diag.get("timeline") or {}, noun))
    parts.append(_categories(diag.get("categories") or []))
    parts.append(_answers(diag.get("answers") or []))
    parts.append(_resources(diag.get("resources") or {}, noun))
    for note in diag.get("notes") or []:
        parts.append(f'<p class="dx-note">ⓘ {e(note)}</p>')
    parts.append("</section>")
    return "".join(parts)


def _timeline(timeline: dict[str, Any], noun: str) -> str:
    buckets = timeline.get("buckets") or []
    if len(buckets) < 2:
        return ""
    e = html.escape
    width, height, pad = 900, 150, 2
    top = max((b["passed"] + b["failed"]) for b in buckets) or 1
    bar_w = (width - pad * 2) / len(buckets)
    bars = []
    for i, b in enumerate(buckets):
        x, w = pad + i * bar_w, max(bar_w - 2, 1)
        hp = b["passed"] / top * (height - pad * 2)
        hf = b["failed"] / top * (height - pad * 2)
        kinds = ", ".join(f"{k} {n}" for k, n in (b.get("kinds") or {}).items())
        ttfb = f", TTFB median {round(b['ttfbMed'])} ms" if b.get("ttfbMed") is not None else ""
        tip = e(f"{b['label']}: {b['passed']} passed, {b['failed']} failed{' (' + kinds + ')' if kinds else ''}{ttfb}")
        y = height - pad - hp
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{hp:.1f}" class="dx-pass"><title>{tip}</title></rect>')
        bars.append(f'<rect x="{x:.1f}" y="{y - hf:.1f}" width="{w:.1f}" height="{hf:.1f}" class="dx-fail"><title>{tip}</title></rect>')
    first, middle, last = buckets[0]["label"], buckets[len(buckets) // 2]["label"], buckets[-1]["label"]
    return (f'<h3>When it happened <span class="dx-note">— {e(noun)} by start time, every {timeline["bucketS"]} s '
            f'(hover a bar for details)</span></h3>'
            f'<div class="panel"><svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="dx-timeline" '
            f'role="img" aria-label="Passed and failed {e(noun)} over time">{"".join(bars)}</svg>'
            f'<div class="dx-axis"><span>{e(first)}</span><span>{e(middle)}</span><span>{e(last)}</span></div>'
            f'<div class="dx-legend"><i class="dx-pass" style="background:var(--dx-ok)"></i>passed'
            f'<i style="background:var(--dx-bad)"></i>failed</div></div>')


def _machine(m: dict[str, Any] | None) -> str:
    """The test computer: capacity, peak load and a CPU / memory chart."""
    if not m or not m.get("samples"):
        return ""
    e = html.escape
    busy = round((m.get("busyShare") or 0) * 100)
    text = (f'{m.get("cores")} cores, {m.get("memoryGb")} GB · CPU max {m.get("cpuMax")}%, '
            f'90 %+ for {busy}% of the run · memory max {m.get("memoryMax")}% · '
            f'recommended browser users: {m.get("recommendedBrowsers")}')
    samples = m["samples"]
    width, height = 900, 70
    end = max(samples[-1][0], 1)

    def line(index: int) -> str:
        points = " ".join(f"{s[0] / end * width:.1f},{height - (s[index] or 0) / 100 * height:.1f}"
                          for s in samples if s[index] is not None)
        return points

    chart = (f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="dx-machine" role="img" '
             f'aria-label="Test computer CPU and memory during the run">'
             f'<line x1="0" x2="{width}" y1="{height * 0.1:.1f}" y2="{height * 0.1:.1f}" class="dx-limit"/>'
             f'<polyline points="{line(1)}" class="dx-cpu"/><polyline points="{line(2)}" class="dx-mem"/></svg>')
    return (f'<h3>Test computer <span class="dx-note">— this computer\'s own load during the run</span></h3>'
            f'<div class="panel"><div class="dx-note">{e(text)}</div>{chart}'
            f'<div class="dx-legend"><i style="background:var(--dx-bad)"></i>CPU'
            f'<i style="background:var(--dx-info)"></i>memory · dashed line: 90%</div></div>')


def _categories(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    e = html.escape
    body = "".join(
        f'<tr><td><b>{e(r["label"])}</b><div class="desc">{e(r["meaning"])}</div></td>'
        f'<td class="n">{r["count"]}</td><td>{e(r["first"])}</td><td>{e(r["last"])}</td>'
        f'<td class="n">{r["users"]}</td><td class="wrap note">{e(r["example"])}</td></tr>' for r in rows)
    return ('<h3>Failures by type</h3><div class="table-wrap"><table><thead><tr><th>Type</th><th class="n">Count</th>'
            '<th>First</th><th>Last</th><th class="n">Users</th><th>Example error</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def _answers(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    e = html.escape
    body = "".join(
        f'<tr><td class="n">{r["status"] or "—"}</td><td>{e(r["server"] or "—")}</td>'
        f'<td>{e(r["contentType"] or "—")}</td><td class="wrap">{e(readable_body(r["body"]) or "—")}</td>'
        f'<td class="n">{r["count"]}</td></tr>' for r in rows)
    return ('<h3>What the server answered on failures</h3><div class="table-wrap"><table><thead><tr>'
            '<th class="n">Status</th><th>Server / powered by</th><th>Content type</th><th>Answer</th>'
            f'<th class="n">Count</th></tr></thead><tbody>{body}</tbody></table></div>')


def _resources(resources: dict[str, Any], noun: str) -> str:
    e = html.escape
    out = ""
    failing = resources.get("failing") or []
    if failing:
        body = "".join(f'<tr><td class="wrap">{e(r["url"])}</td><td>{e(r.get("type") or "—")}</td>'
                       f'<td>{e(r["problem"])}</td><td class="n">{r["visits"]}</td></tr>' for r in failing)
        out += ('<h3>Page resources that failed</h3><div class="table-wrap"><table><thead><tr><th>URL</th>'
                f'<th>Type</th><th>Problem</th><th class="n">{e(noun.capitalize())}</th></tr></thead>'
                f'<tbody>{body}</tbody></table></div>')
    slowest = resources.get("slowest") or []
    if slowest:
        body = "".join(f'<tr><td class="wrap">{e(r["url"])}</td><td class="n">{round(r["medMs"])} ms</td>'
                       f'<td class="n">{round(r["maxMs"])} ms</td><td class="n">{r["visits"]}</td></tr>'
                       for r in slowest)
        out += ('<h3>Slowest page resources</h3><div class="table-wrap"><table><thead><tr><th>URL</th>'
                f'<th class="n">Median</th><th class="n">Max</th><th class="n">{e(noun.capitalize())}</th></tr>'
                f'</thead><tbody>{body}</tbody></table></div>')
    return out
