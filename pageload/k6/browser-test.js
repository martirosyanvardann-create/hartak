/**
 * Full page load measurement for any website.
 * Requires current k6 + Chrome/Chromium. No npm dependencies.
 *
 * No site-specific checks: a visit passes when the page returns the
 * expected HTTP status and the browser reports the page as fully loaded.
 *
 * WAIT_UNTIL decides what "fully loaded" means (built-in k6 load states):
 *   load        — (default) the browser "load" event: HTML, scripts, CSS,
 *                 images and fonts are downloaded and the page is built.
 *   networkidle — "load", then no network requests for 500ms. Also catches
 *                 content that loads after the load event (lazy data, APIs).
 *                 Adds at least 500ms per visit and may time out on pages
 *                 that never stop polling.
 *
 * Per visit, timings come from the browser's own Performance APIs:
 *   ttfb, fcp, lcp, dom (DOMContentLoaded), load (load event end),
 *   fully loaded (load event or last network request, whichever is later),
 *   plus request count and transferred bytes.
 *
 * Profiles:
 * smoke    — one visit
 * baseline — 30 sequential visits (ITERATIONS)
 * parallel — 30 visits (ITERATIONS) shared by 5 parallel users (VUS);
 *            same visit count on every site, so results are comparable
 * load     — parallel users (VUS) repeating visits for DURATION;
 *            a slower site gets fewer visits
 *
 * Usually started by the pageload tool (GUI, API or "pageload run"), which
 * also shares one Chrome between users and builds the HTML report.
 * Directly with k6:
 *   k6 run browser-test.js
 *   k6 run -e BASE_URL=https://dev.hartak.am/ -e PROFILE=baseline browser-test.js
 *   k6 run -e WAIT_UNTIL=networkidle browser-test.js
 *
 * Optional env: BASE_URL (default https://hartak.am/), WAIT_UNTIL, LOCALE,
 * EXPECT_STATUS (200, 2xx or 200,301; default 200), VIEWPORT_WIDTH / VIEWPORT_HEIGHT
 * (default 1440x900), TIMEOUT_MS (default 30000), SCREENSHOT_ON_FAIL=1,
 * ENFORCE_SLO=1 with LOAD_P95_MS (default 5000),
 * MAX_DURATION (run time limit, e.g. 5m), GRACEFUL_STOP (default 30s).
 */

import { browser } from 'k6/browser';
import { check } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

// Expected status: "200", "2xx", or a list like "200,301" / "2xx,304".
function parseStatusSpec(spec) {
  const parts = String(spec).split(',').map((p) => p.trim().toLowerCase()).filter(Boolean);
  if (!parts.length || !parts.every((p) => /^[1-5]xx$/.test(p) || /^[1-5]\d\d$/.test(p))) {
    throw new Error(`EXPECT_STATUS must look like 200, 2xx or 200,301 (got "${spec}")`);
  }
  return parts;
}

function statusMatches(status, parts) {
  return parts.some((p) => (p.endsWith('xx') ? Math.floor(status / 100) === Number(p[0]) : status === Number(p)));
}

function integerEnv(name, fallback, minimum = 1) {
  const value = Number(__ENV[name] ?? fallback);

  if (!Number.isInteger(value) || value < minimum) {
    throw new Error(`${name} must be an integer >= ${minimum}`);
  }

  return value;
}

const PROFILE = __ENV.PROFILE || 'smoke';
const BASE_URL = __ENV.BASE_URL || 'https://hartak.am/';
const WAIT_UNTIL = __ENV.WAIT_UNTIL || 'load';
const LOCALE = __ENV.LOCALE || undefined;
const EXPECT_STATUS = __ENV.EXPECT_STATUS || '200';
const EXPECTED = parseStatusSpec(EXPECT_STATUS);
const TIMEOUT_MS = integerEnv('TIMEOUT_MS', 30000);
const SCREENSHOT_ON_FAIL = __ENV.SCREENSHOT_ON_FAIL === '1';
const DEBUG_STEPS = __ENV.DEBUG_STEPS === '1';

function step(name) {
  if (DEBUG_STEPS) {
    console.log(`STEP vu=${__VU} iter=${__ITER} ${name} t=${Date.now()}`);
  }
}

// Set by the pageload tool. When set, the run also writes summary.json there,
// and each visit is logged as a "VISIT {json}" line for the report.
const REPORT_DIR = __ENV.REPORT_DIR || '';

if (!['load', 'networkidle'].includes(WAIT_UNTIL)) {
  throw new Error('WAIT_UNTIL must be load or networkidle');
}

if (!/^https?:\/\//i.test(BASE_URL)) {
  throw new Error('BASE_URL must be an http(s) URL');
}

const VIEWPORT = {
  width: integerEnv('VIEWPORT_WIDTH', 1440),
  height: integerEnv('VIEWPORT_HEIGHT', 900),
};

// Timings recorded per visit: [visit field, metric name, label].
const TIMINGS = [
  ['ttfbMs', 'page_ttfb_ms', 'TTFB'],
  ['fcpMs', 'page_fcp_ms', 'FCP'],
  ['lcpMs', 'page_lcp_ms', 'LCP'],
  ['domMs', 'page_dom_loaded_ms', 'DOMContentLoaded'],
  ['loadMs', 'page_load_ms', 'Load'],
  ['fullyLoadedMs', 'page_fully_loaded_ms', 'Fully loaded'],
];

const trends = Object.fromEntries(
  TIMINGS.map(([field, metric]) => [field, new Trend(metric, true)])
);
const completedVisits = new Counter('page_completed_visits');
const pageFailRate = new Rate('page_fail_rate');

const profiles = {
  smoke: {
    executor: 'shared-iterations',
    vus: 1,
    iterations: 1,
    maxDuration: '2m',
  },

  baseline: {
    executor: 'shared-iterations',
    vus: 1,
    iterations: integerEnv('ITERATIONS', 30),
    maxDuration: __ENV.MAX_DURATION || '30m',
  },

  // Fixed number of visits shared by parallel users, so different
  // sites get exactly the same number of page loads.
  parallel: {
    executor: 'shared-iterations',
    vus: integerEnv('VUS', 5),
    iterations: integerEnv('ITERATIONS', 30),
    // Safety limit: a visit that hangs inside the browser can't block the run.
    maxDuration: __ENV.MAX_DURATION || '10m',
  },

  load: {
    executor: 'constant-vus',
    vus: integerEnv('VUS', 3),
    duration: __ENV.DURATION || '3m',
  },
};

if (!profiles[PROFILE]) {
  throw new Error('PROFILE must be smoke, baseline, parallel or load');
}

// Run exactly the requested users: never lower the count silently.
if (PROFILE === 'parallel' && profiles.parallel.vus > profiles.parallel.iterations) {
  throw new Error(
    `VUS (${profiles.parallel.vus}) cannot be more than ITERATIONS ` +
    `(${profiles.parallel.iterations}): each user needs at least one visit`
  );
}

const thresholds = {
  checks: ['rate==1'],
  page_fail_rate: ['rate==0'],
  page_completed_visits: [
    PROFILE === 'load'
      ? 'count>0'
      : `count==${profiles[PROFILE].iterations}`,
  ],
};

// Optional starter budget, not an agreed SLO.
if (__ENV.ENFORCE_SLO === '1') {
  thresholds.page_fully_loaded_ms = [
    `p(95)<${integerEnv('LOAD_P95_MS', 5000)}`,
  ];
}

export const options = {
  scenarios: {
    page: {
      ...profiles[PROFILE],
      gracefulStop: __ENV.GRACEFUL_STOP || '30s',
      options: {
        browser: {
          type: 'chromium',
        },
      },
    },
  },

  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)'],

  tags: {
    profile: PROFILE,
    wait_until: WAIT_UNTIL,
    viewport: `${VIEWPORT.width}x${VIEWPORT.height}`,
  },

  thresholds,
};

// Injected into every page before its own scripts run. Records LCP as it
// happens, so reading timings later is synchronous (no async callbacks
// inside page.evaluate that could leave a visit waiting forever).
const LCP_RECORDER = `
  window.__pageLcp = null;
  try {
    new PerformanceObserver(function (list) {
      var entries = list.getEntries();
      if (entries.length) {
        window.__pageLcp = entries[entries.length - 1].startTime;
      }
    }).observe({ type: 'largest-contentful-paint', buffered: true });
  } catch (error) {}
`;

// Runs in the browser after the page is loaded. Reads the browser's own
// Performance APIs; all times are ms since navigation start.
const readPageTimings = () => {
  const nav = performance.getEntriesByType('navigation')[0];
  const resources = performance.getEntriesByType('resource');
  const fcp = performance
    .getEntriesByType('paint')
    .find((entry) => entry.name === 'first-contentful-paint');

  const lastResourceEnd = resources.reduce(
    (latest, entry) => Math.max(latest, entry.responseEnd),
    0
  );

  const bytes =
    (nav ? nav.transferSize : 0) +
    resources.reduce((sum, entry) => sum + (entry.transferSize || 0), 0);

  return {
    ttfbMs: nav ? nav.responseStart : null,
    fcpMs: fcp ? fcp.startTime : null,
    lcpMs: typeof window.__pageLcp === 'number' ? window.__pageLcp : null,
    domMs: nav ? nav.domContentLoadedEventEnd : null,
    loadMs: nav && nav.loadEventEnd > 0 ? nav.loadEventEnd : null,
    fullyLoadedMs: nav ? Math.max(nav.loadEventEnd, lastResourceEnd) : null,
    requests: resources.length + 1,
    transferBytes: bytes,
  };
};

export default async function () {
  let context;
  let page;
  let passed = false;
  // Which step was running: 'navigation' or 'timings'.
  let stage = 'navigation';

  // One record per visit, logged at the end for the HTML report.
  const visit = {
    visit: exec.scenario.iterationInTest + 1,
    vu: __VU,
    iteration: __ITER,
    startedAt: new Date().toISOString(),
    url: BASE_URL,
    httpStatus: null,
    ttfbMs: null,
    fcpMs: null,
    lcpMs: null,
    domMs: null,
    loadMs: null,
    fullyLoadedMs: null,
    requests: null,
    transferBytes: null,
    passed: false,
    error: null,
    screenshot: null,
    durationMs: null,
  };
  const startedAt = Date.now();

  completedVisits.add(0);

  try {
    step('newContext');
    context = await browser.newContext({
      viewport: VIEWPORT,
      locale: LOCALE,
    });

    step('addInitScript');
    await context.addInitScript(LCP_RECORDER);
    step('newPage');
    page = await context.newPage();
    step('goto');

    // One explicit wait: k6 resolves goto when the page reaches WAIT_UNTIL.
    const response = await page.goto(BASE_URL, {
      waitUntil: WAIT_UNTIL,
      timeout: TIMEOUT_MS,
    });

    visit.httpStatus = response ? response.status() : 0;

    const statusOK = check(visit.httpStatus, {
      [`HTTP ${EXPECT_STATUS}`]: (status) => statusMatches(status, EXPECTED),
    });

    if (!statusOK) {
      throw new Error(
        `HTTP status ${visit.httpStatus}, expected ${EXPECT_STATUS}`
      );
    }

    stage = 'timings';
    step('evaluate');
    Object.assign(visit, await page.evaluate(readPageTimings));

    const loaded = check(visit.loadMs, {
      'page fully loaded': (value) => typeof value === 'number' && value > 0,
    });

    if (!loaded) {
      throw new Error('Browser did not report a finished load event');
    }

    for (const [field] of TIMINGS) {
      if (typeof visit[field] === 'number') {
        trends[field].add(visit[field]);
      }
    }

    passed = true;
  } catch (error) {
    console.error(
      `Visit failed (VU ${__VU}, iteration ${__ITER}): ${error}`
    );
    visit.error = `${stage}: ${error}`;

    if (page && SCREENSHOT_ON_FAIL) {
      // Saved inside the run folder; the report links to it relatively.
      const name = `screenshots/fail-vu${__VU}-iter${__ITER}.png`;
      const path = REPORT_DIR ? `${REPORT_DIR}/${name}` : name;

      try {
        await page.screenshot({ path, fullPage: true });
        visit.screenshot = name;
      } catch (screenshotError) {
        console.error(`Screenshot failed: ${screenshotError}`);
      }
    }
  } finally {
    try {
      if (page) {
        step('page.close');
        await page.close();
      }
    } catch (error) {
      console.error(`Page cleanup failed: ${error}`);
    }

    try {
      if (context) {
        step('context.close');
        await context.close();
      }
    } catch (error) {
      console.error(`Context cleanup failed: ${error}`);
    }

    step('done');
    // Counts navigation failures too (e.g. timeouts before any check ran).
    check(passed, { 'visit completed': (value) => value });
    pageFailRate.add(!passed);
    completedVisits.add(1);

    visit.passed = passed;
    visit.durationMs = Date.now() - startedAt;

    if (REPORT_DIR) {
      console.log(`VISIT ${JSON.stringify(visit)}`);
    }
  }
}

function formatMs(value) {
  return typeof value === 'number' ? `${Math.round(value)}ms` : 'n/a';
}

function formatDuration(ms) {
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;

  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

// Prints only: page timings, checks, visits and total runtime.
export function handleSummary(data) {
  const lines = [''];
  const timingStats = {};

  lines.push(`URL: ${BASE_URL}   Profile: ${PROFILE}   Wait until: ${WAIT_UNTIL}`);
  lines.push('');
  lines.push(
    `${'Timing'.padEnd(18)}${['avg', 'min', 'med', 'max', 'p(90)', 'p(95)']
      .map((s) => s.padStart(9))
      .join('')}`
  );

  for (const [field, metric, label] of TIMINGS) {
    const values = (data.metrics[metric] || {}).values || {};
    timingStats[field] = values;

    lines.push(
      `${label.padEnd(18)}${['avg', 'min', 'med', 'max', 'p(90)', 'p(95)']
        .map((s) => formatMs(values[s]).padStart(9))
        .join('')}`
    );
  }

  lines.push('');
  lines.push('Checks');

  const checks = (data.root_group && data.root_group.checks) || [];

  for (const item of checks) {
    const total = item.passes + item.fails;
    const mark = item.fails === 0 ? '✓' : '✗';

    lines.push(`  ${mark} ${item.name}: ${item.passes}/${total}`);
  }

  const visits = ((data.metrics.page_completed_visits || {}).values || {}).count || 0;
  const failRate = ((data.metrics.page_fail_rate || {}).values || {}).rate || 0;
  const failed = Math.round(visits * failRate);

  lines.push('');
  lines.push(`Visits: ${visits - failed} passed, ${failed} failed`);
  lines.push(`Total runtime: ${formatDuration(data.state.testRunDurationMs)}`);
  lines.push('');

  const output = { stdout: lines.join('\n') };

  if (REPORT_DIR) {
    output[`${REPORT_DIR}/summary.json`] = JSON.stringify({
      mode: 'browser',
      url: BASE_URL,
      profile: PROFILE,
      waitUntil: WAIT_UNTIL,
      viewport: `${VIEWPORT.width}x${VIEWPORT.height}`,
      totalRuntimeMs: data.state.testRunDurationMs,
      // Requested visits (fixed-count profiles); missing ones hung or never ran.
      expectedVisits: PROFILE === 'load' ? null : profiles[PROFILE].iterations,
      timings: timingStats,
    }, null, 2);
  }

  return output;
}
