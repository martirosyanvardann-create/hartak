/**
 * HTTP / API response-time test for any URL. No browser: k6 sends the
 * request itself and measures how the server answers.
 * Requires current k6. No npm dependencies.
 *
 * VUS parallel users share exactly ITERATIONS requests, all starting
 * together, so different URLs get the same number of requests.
 *
 * Per request (from k6's own timings):
 *   connect  — opening the TCP connection
 *   tls      — TLS (https) handshake
 *   ttfb     — waiting for the first byte of the answer (server time)
 *   receive  — downloading the answer
 *   total    — all of the above plus sending the request
 * Each user keeps its connection open between requests (like a real API
 * client), so connect and TLS are usually 0 after a user's first request.
 *
 * A request passes when its status matches EXPECT_STATUS (default 2xx,
 * after redirects; also 200 or a list like 200,301) and no network error.
 *
 * Examples:
 *   k6 run -e BASE_URL=https://hartak.am/ -e VUS=20 -e ITERATIONS=200 api-test.js
 *   API_METHOD=POST API_BODY='{"q":"test"}' \
 *     API_HEADERS='{"Content-Type":"application/json"}' \
 *     k6 run -e BASE_URL=https://example.com/api/search api-test.js
 *
 * Env: BASE_URL, VUS (default 10), ITERATIONS (default 100),
 * API_METHOD (default GET), API_HEADERS (JSON object), API_BODY,
 * EXPECT_STATUS (default 2xx), TIMEOUT_MS (default 30000), MAX_DURATION (default 10m),
 * GRACEFUL_STOP (default 30s), REPORT_DIR (set by the pageload tool).
 */

import http from 'k6/http';
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

const BASE_URL = __ENV.BASE_URL || 'https://hartak.am/';
const VUS = integerEnv('VUS', 10);
const ITERATIONS = integerEnv('ITERATIONS', 100);
const TIMEOUT_MS = integerEnv('TIMEOUT_MS', 30000);
const METHOD = (__ENV.API_METHOD || 'GET').toUpperCase();
const BODY = __ENV.API_BODY || null;
const REPORT_DIR = __ENV.REPORT_DIR || '';
const EXPECT_STATUS = __ENV.EXPECT_STATUS || '2xx';
const EXPECTED = parseStatusSpec(EXPECT_STATUS);

const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'];

if (!/^https?:\/\//i.test(BASE_URL)) {
  throw new Error('BASE_URL must be an http(s) URL');
}

if (!METHODS.includes(METHOD)) {
  throw new Error(`API_METHOD must be one of ${METHODS.join(', ')}`);
}

if (VUS > ITERATIONS) {
  throw new Error(
    `VUS (${VUS}) cannot be more than ITERATIONS (${ITERATIONS}): ` +
    'each user needs at least one request'
  );
}

let HEADERS = {};

if (__ENV.API_HEADERS) {
  try {
    HEADERS = JSON.parse(__ENV.API_HEADERS);
  } catch (error) {
    throw new Error(`API_HEADERS must be a JSON object: ${error}`);
  }
}

// Timings recorded per request: [visit field, metric name, label].
const TIMINGS = [
  ['connectMs', 'api_connect_ms', 'Connect'],
  ['tlsMs', 'api_tls_ms', 'TLS'],
  ['ttfbMs', 'api_ttfb_ms', 'TTFB'],
  ['receiveMs', 'api_receive_ms', 'Receive'],
  ['totalMs', 'api_total_ms', 'Total'],
];

const trends = Object.fromEntries(
  TIMINGS.map(([field, metric]) => [field, new Trend(metric, true)])
);
const completedRequests = new Counter('api_completed_requests');
const failRate = new Rate('api_fail_rate');

export const options = {
  scenarios: {
    api: {
      executor: 'shared-iterations',
      vus: VUS,
      iterations: ITERATIONS,
      maxDuration: __ENV.MAX_DURATION || '10m',
      gracefulStop: __ENV.GRACEFUL_STOP || '30s',
    },
  },

  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)'],

  tags: {
    mode: 'api',
    method: METHOD,
  },

  thresholds: {
    checks: ['rate==1'],
    api_fail_rate: ['rate==0'],
    api_completed_requests: [`count==${ITERATIONS}`],
  },
};

function headerValue(response, name) {
  const wanted = name.toLowerCase();

  for (const key of Object.keys(response.headers || {})) {
    if (key.toLowerCase() === wanted) {
      return response.headers[key];
    }
  }

  return null;
}

export default function () {
  const startedAt = Date.now();
  const visit = {
    visit: exec.scenario.iterationInTest + 1,
    vu: __VU,
    iteration: __ITER,
    startedAt: new Date().toISOString(),
    url: BASE_URL,
    method: METHOD,
    httpStatus: null,
    connectMs: null,
    tlsMs: null,
    ttfbMs: null,
    receiveMs: null,
    totalMs: null,
    requests: 1,
    transferBytes: null,
    passed: false,
    error: null,
    durationMs: null,
  };

  const response = http.request(METHOD, BASE_URL, BODY, {
    headers: HEADERS,
    timeout: `${TIMEOUT_MS}ms`,
    tags: { name: BASE_URL },
  });

  const t = response.timings;
  visit.httpStatus = response.status;
  visit.connectMs = t.connecting;
  visit.tlsMs = t.tls_handshaking;
  visit.ttfbMs = t.waiting;
  visit.receiveMs = t.receiving;
  visit.totalMs = t.blocked + t.connecting + t.tls_handshaking + t.sending + t.waiting + t.receiving;

  const length = Number(headerValue(response, 'Content-Length'));
  visit.transferBytes = Number.isFinite(length) && length > 0
    ? length
    : (response.body ? response.body.length : 0);

  const statusOK = statusMatches(response.status, EXPECTED);
  visit.passed = check(response, {
    [`HTTP ${EXPECT_STATUS}`]: () => statusOK && !response.error,
  });

  if (!visit.passed) {
    visit.error = response.error
      ? `request: ${response.error}`
      : `request: HTTP status ${response.status}, expected ${EXPECT_STATUS}`;
    console.error(`Request failed (VU ${__VU}, iteration ${__ITER}): ${visit.error}`);
  } else {
    for (const [field] of TIMINGS) {
      trends[field].add(visit[field]);
    }
  }

  failRate.add(!visit.passed);
  completedRequests.add(1);
  visit.durationMs = Date.now() - startedAt;

  if (REPORT_DIR) {
    console.log(`VISIT ${JSON.stringify(visit)}`);
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

export function handleSummary(data) {
  const stats = ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)'];
  const lines = [''];
  const timingStats = {};

  lines.push(`URL: ${BASE_URL}   Mode: API (${METHOD})   Users: ${VUS}   Requests: ${ITERATIONS}`);
  lines.push('');
  lines.push(`${'Timing'.padEnd(12)}${stats.map((s) => s.padStart(9)).join('')}`);

  for (const [field, metric, label] of TIMINGS) {
    const values = (data.metrics[metric] || {}).values || {};
    timingStats[field] = values;
    lines.push(`${label.padEnd(12)}${stats.map((s) => formatMs(values[s]).padStart(9)).join('')}`);
  }

  const done = ((data.metrics.api_completed_requests || {}).values || {}).count || 0;
  const failRateValue = ((data.metrics.api_fail_rate || {}).values || {}).rate || 0;
  const failed = Math.round(done * failRateValue);

  lines.push('');
  lines.push(`Requests: ${done - failed} passed, ${failed} failed`);
  lines.push(`Total runtime: ${formatDuration(data.state.testRunDurationMs)}`);
  lines.push('');

  const output = { stdout: lines.join('\n') };

  if (REPORT_DIR) {
    output[`${REPORT_DIR}/summary.json`] = JSON.stringify({
      mode: 'api',
      method: METHOD,
      url: BASE_URL,
      profile: 'api',
      waitUntil: null,
      viewport: null,
      totalRuntimeMs: data.state.testRunDurationMs,
      expectedVisits: ITERATIONS,
      timings: timingStats,
    }, null, 2);
  }

  return output;
}
