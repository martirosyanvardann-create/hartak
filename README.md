# Page Load Compare

Measure and compare how fast websites load, or how fast their API answers, using
[k6](https://k6.io). Run it from a GUI, an HTTP API, or the command line.

Every URL gets **exactly the same number of visits by the same number of parallel
users**, so two sites (for example `hartak.am` and `dev.hartak.am`) can be
compared fairly.

| Mode | What runs | Measures | Recommended users / visits | Maximum |
|---|---|---|---|---|
| **Browser** (default) | a real headless Chrome per visit | TTFB, FCP, LCP, DOM loaded, load, fully loaded | depends on this computer (about 1 per CPU core, max 20) / 1000 | 200 / 100 000 |
| **Hybrid** (browser mode + *Real browsers*) | a few real Chromes measure while the other users load the page with HTTP requests | same as browser, plus the background load | many users: only the real browsers cost CPU | 200 / 100 000 |
| **API only** | plain HTTP requests, no browser | connect, TLS, TTFB, receive, total | 200 / 10 000 | 5000 / 1 000 000 |

Above the recommended numbers a run still starts, with a warning (in the form, the
results, the CLI and the API answer): in browser mode every user is a real Chrome
page, so too many users measure this computer's load as much as the site; in API
mode many users mean heavy traffic for the tested site. The maximums only stop
obvious typos.

## Many users: hybrid mode

In browser mode **every user is a full Chrome page on this computer**: it downloads,
runs and draws the whole page (about one CPU core and 200–400 MB each). With more
users than the computer can handle, pages time out or get slow although the site
is fine — e.g. 30 browser users in Docker on a 10-core Mac: 0 of 30 visits passed,
CPU at 100%, while 1 user and the API test of the same site were fast.

Set **Real browsers** (GUI) / `"browsers"` (API) / `--browsers` (CLI) to a small
number, e.g. 3: those real browsers measure the visits while the other users
request the same page with plain HTTP, each about once every 5 seconds like a
visitor. Same example in hybrid mode: 3 browsers + 27 HTTP users, 30 of 30 passed,
fully loaded 2.6 s, CPU at 39%.

The recommended number of browser users is calculated from this computer's cores
and memory (`PAGELOAD_RECOMMENDED_BROWSERS` overrides it); above it the form shows a
warning with a one-click switch to hybrid mode.

Every run also records **the test computer's own CPU and memory**
(`machine.json`). If it was overloaded, the result says so first: *"The test
computer was overloaded, so these results are not reliable"*, with a CPU / memory
chart in the report.

## Requirements

* Python 3.10+ (standard library only, nothing to `pip install`)
* [k6](https://grafana.com/docs/k6/latest/set-up/install-k6/) — `brew install k6`
* Google Chrome or Chromium (browser mode only)

## Quick start

```bash
python3 app.py              # GUI at http://127.0.0.1:8000 (opens in your browser)
```

Enter a URL, optionally a second URL to compare, the number of parallel users and
visits, then press **Run**. Everything else — API-only mode, method/headers/body,
timeout, time limit, expected status, performance budget, screen size, browser
language, screenshots — is in the form and its **Advanced settings**.

Each run writes a folder under `reports/` with an HTML report (one row per visit),
the raw k6 output and screenshots of failed visits.

### Downloads

* **Download comparison / Download results** (above the results) — one ZIP with a
  standalone `comparison.html` (or `summary.html`), `comparison.csv` (or
  `timings.csv`), `result.json` and every URL's run folder.
* **Download ↓** on each site card — that URL's run: `report.html`, `visits.csv`
  (one row per visit, opens in Excel/Numbers), raw k6 output and screenshots.

Finished runs are saved as `reports/<run id>.json`, so downloads keep working
after the app restarts.

## Docker

No Python, k6 or Chrome needed on your machine — only Docker:

```bash
docker compose up -d --build     # or: make docker-up
```

Open <http://localhost:8000>. Results are written to `./reports` on your machine.
Stop with `docker compose down`.

**Testing a site that runs on your own computer:** inside Docker `localhost` is
the container itself, so use `host.docker.internal` instead, e.g.
`http://localhost:3000` → `http://host.docker.internal:3000`.

Command-line runs work the same way:

```bash
docker compose run --rm pageload run https://hartak.am/ https://dev.hartak.am/ --vus 20 --visits 40
```

The image contains Python, k6 (pinned, `K6_VERSION` build argument) and Chromium.
Chrome in a container needs `--no-sandbox` and more shared memory than Docker's
default, which `Dockerfile` (`CHROME_FLAGS`, `K6_BROWSER_ARGS`) and
`docker-compose.yml` (`shm_size`) set. `GET /healthz` is used as the health check.

| Environment variable | Default | |
|---|---|---|
| `PAGELOAD_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | address the server listens on |
| `PAGELOAD_PORT` | `8000` | port |
| `PAGELOAD_REPORTS_DIR` | `reports/` (`/data/reports` in Docker) | where results are written |
| `CHROME_PATH`, `K6_PATH` | found automatically | Chrome / k6 to use |
| `CHROME_FLAGS` | — | extra flags for the shared Chrome |

## Without the GUI

**HTTP API**

```bash
python3 app.py --api-only   # JSON only, no web page
curl -X POST http://127.0.0.1:8000/api/run -H 'Content-Type: application/json' \
  -d '{"url": "https://hartak.am/", "compareUrl": "https://dev.hartak.am/",
       "vus": 20, "iterations": 100, "apiOnly": true, "wait": true}'
```

| Endpoint | |
|---|---|
| `GET /api` | help, including every field and setting |
| `GET /healthz` | `{"ok": true, "k6": …, "chrome": …}` for health checks |
| `GET /api/config` | modes, limits, labels and setting defaults |
| `POST /api/run` | start a run; `"wait": true` answers when it has finished |
| `GET /api/status` | progress, statistics, comparison and log of the latest run |
| `POST /api/stop` | stop the running run |
| `GET /api/jobs/<id>` | saved result of a finished run (id from `/api/status`) |
| `GET /api/jobs/<id>/download` | ZIP of the whole run or comparison |
| `GET /api/runs/<folder>/download` | ZIP of one URL's run |

**Command line** — runs once, prints the comparison and exits:

```bash
python3 app.py run https://hartak.am/ https://dev.hartak.am/ --vus 20 --visits 40
python3 app.py run https://hartak.am/ --api --vus 50 --visits 500 --json > result.json
python3 app.py run https://hartak.am/ --vus 30 --browsers 3 --visits 30   # hybrid
python3 app.py report reports/<run-folder>      # rebuild a report
```

Exit codes: `0` all passed · `1` failed or hung visits, or over budget · `2` bad input · `130` stopped.

After `make install` (or `pip install .`) the same commands are available as `pageload …`.

## How results are judged

* A visit **passes** when the HTTP status matches the expected status (`200` in
  browser mode, `2xx` in API mode, or e.g. `200,301`) and the page finished loading.
* Visits that run longer than the **time limit** are stopped and reported as
  **hung**; a URL with missing visits is shown as *incomplete* or *failed*, never as done.
* With a **budget**, a URL whose p95 (fully loaded, or total in API mode) is above
  the limit makes the run *not OK*.
* Comparisons always show the real difference (`3% slower`, `3.2× slower`);
  only exactly equal values are “the same”.

## Possible reasons (diagnosis)

When visits fail or are slow, every result explains **what happened, the likely
reasons and what to check on the server**: in the GUI (under each site card), in
each run's `report.html` (*Possible reasons*, with a timeline), in the command line
output and in the downloads (`problems.csv`, plus `failureType`, `server` and
`serverAnswer` columns in `visits.csv`).

It recognises, among others:

| What the test sees | Typical meaning |
|---|---|
| 404 / 401 / 403 / 405 on every request, answered quickly | wrong URL, method or missing token, not a load problem |
| 502 / 503 / 504 | the app behind the proxy crashed or restarted, was overloaded, or too slow |
| timeouts with no answer at all | requests queued: workers busy, blocked event loop, slow database / backend |
| HTML arrived but the page never finished | a script, image or API call of the page hung (listed) |
| the test computer's CPU at 90 %+ / memory full | the test computer, not the site, limited the results |
| HTML arrived quickly, many browser users, pages never finished | most likely the test computer (older runs without machine data) |
| everything fails from some moment until the end | the site went down during the test and did not recover |
| failures only at the start | cold start, or the first burst of users overloaded the server |
| many requests answered together after a long wait | all waited for one shared blocking thing (cache rebuild, one slow call) |
| response time grows with load | not enough workers / CPU, no caching |

To collect the evidence, the k6 scripts record per visit whether the HTML arrived,
the `Server` / `X-Powered-By` headers and the start of the answer on failures, the
page's failed requests and its slowest requests. The test only sees what a visitor
sees; the server's own logs (for the times shown) confirm the actual cause.

## Project layout

```
app.py                  entry point for a source checkout (same as `pageload`)
Dockerfile              image with Python, k6 and Chromium
docker-compose.yml      local Docker setup (port 8000, ./reports, shared memory)
pageload/
  cli.py                commands: serve (default), run, report
  server.py             HTTP routes, JobManager (one run at a time), safe static files
  job.py                Job: runs each URL, state machine, problem diagnosis
  k6.py                 K6Runner: k6 child process, watchdog, stop
  chrome.py             SharedChrome: one headless Chrome per browser run
  relay.py              ConnectionRelay: lets many users connect to Chrome at once
  modes.py              BrowserMode / ApiMode
  config.py             RunConfig, Settings, ApiRequest: all input validation
  analysis.py           percentiles, stats, compare, budget (pure functions)
  diagnosis.py          possible reasons for failures / slowness (rule-based, pure functions)
  machine.py            the test computer: capacity, recommended browsers, CPU / memory monitor
  diagnosis_html.py     the "Possible reasons" section of the reports
  results.py            reading a run folder
  report.py             HTML report
  export.py             downloads: visits/comparison CSV, summary page, ZIPs
  formatting.py         ms / size / duration text
  process.py            stopping a process tree
  k6/                   browser-test.js, api-test.js (the k6 scripts)
  gui/                  index.html, app.css, app.js
tests/                  unit and integration tests (no k6 or Chrome needed)
```

### Design

* **Strategy** — `BrowserMode` and `ApiMode` hold everything that differs between
  the two kinds of test (script, limits, timings, labels, k6 variables). Adding a
  mode means adding one class; nothing else checks `mode == "api"`.
* **Value objects** — `RunConfig`, `Settings` and `ApiRequest` are immutable
  dataclasses validated once in `from_dict()`, collecting *all* problems for one
  clear error message. GUI, API and CLI share them.
* **State machine** — a `Job` moves from `running` to exactly one final state
  (`done`, `problem`, `stopped`, `error`); invalid transitions raise.
* **Dependency injection** — `Job` receives its runner, browser and report builder,
  so tests use fakes instead of k6 and Chrome.
* **Context manager** — `SharedChrome` always closes Chrome, the relay and its
  temporary profile, even on errors or Stop.
* **Single source of truth** — the server calculates comparisons and serves limits
  and labels (`/api/config`); the GUI only renders them.

### Why a shared Chrome and a relay?

k6 normally launches a new Chrome for every visit; with parallel users one of those
launches occasionally hangs forever. Instead one headless Chrome is started per run
and each visit gets a fresh, isolated browser context in it. Chrome's DevTools port
only queues a few new connections, so when many users start at once some were
refused (“connection reset”). The relay accepts every connection immediately and
passes them to Chrome as fast as it can take them — all requested users still start
together.

## Development

```bash
make test        # or: python3 -m unittest discover -s tests -t .
make run         # GUI
make docker-up   # GUI in Docker
make help        # all commands
```

The tests cover validation, statistics and comparisons, test modes, the job state
machine (with fake k6/Chrome), the HTML report, the HTTP server (real requests,
path-traversal checks) and the connection relay (50 simultaneous connections).

The k6 scripts can also be run directly, e.g.
`k6 run -e BASE_URL=https://hartak.am/ -e PROFILE=baseline pageload/k6/browser-test.js`.
