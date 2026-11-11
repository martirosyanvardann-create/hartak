"""Command line.

    pageload                     GUI on http://127.0.0.1:8000 (same as "pageload serve")
    pageload serve --api-only    HTTP API only, no GUI
    pageload run URL [URL2] ...  one run without a server; prints results and exits
    pageload report FOLDER       rebuild a run's HTML report

``python3 app.py ...`` and ``python3 -m pageload ...`` do the same.
Exit codes of "run": 0 all passed, 1 failed/hung visits or over budget,
2 bad input, 130 stopped with Ctrl+C.
"""

from __future__ import annotations

import argparse
import os
import json
import logging
import sys
import threading
import time
import webbrowser
from typing import Any

from . import __version__
from .config import WAIT_MODES, RunConfig, ValidationError
from .formatting import host_of, ms
from .job import Job, JobState
from .paths import reports_dir
from .report import build_report
from .server import create_server

COMMANDS = ("serve", "run", "report")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # "pageload" and "pageload --api-only" mean "serve".
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "serve")
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return args.handler(args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pageload", description="Compare website load and API response times with k6.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="start the GUI and HTTP API (default)")
    serve.add_argument("--host", default=os.environ.get("PAGELOAD_HOST", "127.0.0.1"),
                       help="address to listen on (default 127.0.0.1; 0.0.0.0 in Docker)")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PAGELOAD_PORT", "8000")))
    serve.add_argument("--no-browser", action="store_true", help="don't open the GUI in a browser")
    serve.add_argument("--api-only", action="store_true", help="serve only the HTTP API, no GUI")
    serve.set_defaults(handler=cmd_serve)

    run = commands.add_parser("run", help="run a test without a server and print the results")
    run.add_argument("url")
    run.add_argument("compare_url", nargs="?")
    run.add_argument("--vus", type=int, default=5, help="parallel users (default 5)")
    run.add_argument("--visits", type=int, default=30, help="visits/requests per URL (default 30)")
    run.add_argument("--wait-until", choices=WAIT_MODES, default="load")
    run.add_argument("--api", action="store_true", help="API test: plain HTTP requests, no browser")
    run.add_argument("--method", default="GET", help="HTTP method for --api (default GET)")
    run.add_argument("--header", action="append", default=[], metavar="'Name: value'",
                     help="request header for --api (repeatable)")
    run.add_argument("--body", help="request body for --api")
    run.add_argument("--json", action="store_true", help="print the full result as JSON")
    run.set_defaults(handler=cmd_run)

    report = commands.add_parser("report", help="rebuild the HTML report of a run folder")
    report.add_argument("folder")
    report.set_defaults(handler=cmd_report)
    return parser


# -- serve -----------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    server = create_server(args.port, reports_dir(), host=args.host, api_only=args.api_only)
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    url = f"http://{shown_host}:{args.port}/"
    if args.api_only:
        print(f"Page Load Compare API running at {url}api  (Ctrl+C to quit)")
    else:
        print(f"Page Load Compare running at {url}  (Ctrl+C to quit)")
        if not args.no_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.manager.stop()
        server.server_close()
    return 0


# -- run ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = RunConfig.from_dict({
            "url": args.url, "compareUrl": args.compare_url,
            "vus": args.vus, "iterations": args.visits, "waitUntil": args.wait_until,
            "apiOnly": args.api, "method": args.method,
            "headers": "\n".join(args.header), "body": args.body,
        })
    except ValidationError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    for warning in config.warnings():
        print(f"Warning: {warning}", file=sys.stderr)

    directory = reports_dir()
    directory.mkdir(parents=True, exist_ok=True)
    job = Job(config, directory)
    worker = threading.Thread(target=job.run, daemon=True)
    worker.start()

    # Progress goes to stderr, so --json output on stdout stays clean.
    last = None
    try:
        while worker.is_alive():
            line = "  ".join(f"{host_of(t.url)} {job.progress(i)}/{config.iterations}"
                             for i, t in enumerate(job.targets))
            if line != last:
                print(f"[{round(time.time() - job.started):>4}s] {line}", file=sys.stderr, flush=True)
                last = line
            worker.join(2)
    except KeyboardInterrupt:
        print("\nStopping…", file=sys.stderr)
        job.stop()
        worker.join(30)
        return 130

    status = job.status()
    if job.state is JobState.ERROR:
        print(f"Error: {status['message']}", file=sys.stderr)
        return 2
    print(json.dumps(status, indent=2, default=str) if args.json else "\n" + format_result(status))
    return 0 if status["ok"] else 1


def format_result(status: dict[str, Any]) -> str:
    """Plain-text result of a finished run."""
    out = [f"⚠ {warning}" for warning in status.get("warnings", [])]
    if out:
        out.append("")
    for target in status["targets"]:
        host, stats = host_of(target["url"]), target["stats"]
        if stats:
            main = stats["timings"][status["mainMetric"]]
            out.append(f"{host:<24} {stats['passed']}/{target['total']} passed   "
                       f"{status['mainLabel'].lower()} median {ms(main['med'])}   p95 {ms(main['p95'])}   "
                       f"run time {round((stats['runtimeMs'] or 0) / 1000)} s")
        else:
            out.append(f"{host:<24} no results")
        if target.get("budget"):
            budget = target["budget"]
            mark = "✓" if budget["passed"] else "✗"
            out.append(f"{'':<24} {mark} budget: p95 {ms(budget['p95Ms'])} vs {ms(budget['limitMs'])}")
        if target["problem"]:
            out.append(f"{'':<24} ⚠ {target['problem']}")

    comparison = status.get("comparison")
    if comparison:
        a, b = comparison["a"], comparison["b"]
        out += ["", f"{'Timing':<16}{a + ' median':>26}{b + ' median':>26}   {b} is…"]
        for row in comparison["rows"]:
            if "median" in row:
                out.append(f"{row['label']:<16}{ms(row['a']['median']):>26}{ms(row['b']['median']):>26}"
                           f"   {row['median']['text']}")
        out += ["", comparison["headline"], f"Result: {comparison['summary']}"]
    elif len(status["targets"]) == 1 and status["targets"][0]["stats"]:
        timings = status["targets"][0]["stats"]["timings"]
        out += ["", f"{'Timing':<16}{'median':>10}{'p90':>10}{'p95':>10}{'min':>10}{'max':>10}"]
        for field, label in status["timings"]:
            out.append(f"{label:<16}" + "".join(f"{ms(timings[field][k]):>10}" for k in ("med", "p90", "p95", "min", "max")))

    for target in status["targets"]:
        diag = (target.get("stats") or {}).get("diagnosis")
        findings = [f for f in (diag or {}).get("findings", []) if f["level"] != "info"]
        if not findings:
            continue
        out += ["", f"Possible reasons · {host_of(target['url'])}"]
        if diag["categories"]:
            out.append("  " + " · ".join(f"{c['label']} ×{c['count']}" for c in diag["categories"]))
        for finding in findings:
            out.append(f"  {'✗' if finding['level'] == 'critical' else '!'} {finding['title']}")
            out.append(f"      {finding['detail']}")
            out += [f"      - {reason}" for reason in finding["reasons"][:3]]
        out.append("  (all reasons, what to check and a timeline are in the report)")

    reports = [t["stats"]["reportPath"] for t in status["targets"] if t["stats"] and t["stats"].get("reportPath")]
    if reports:
        out.append("")
        out += [f"Report: {path}" for path in reports]
    return "\n".join(out)


# -- report --------------------------------------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    from pathlib import Path

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Error: {folder} is not a folder", file=sys.stderr)
        return 2
    print(f"Report: {build_report(folder)}")
    return 0
