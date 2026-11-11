'use strict';
/*
 * Page Load Compare GUI.
 *
 * The server is the single source of truth:
 *   GET /api/config  modes, limits, labels, setting defaults and ranges
 *   GET /api/status  progress, statistics and the comparison (rows, headline, summary)
 * This file only collects the form, checks the obvious, and renders what the server returns.
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const STORE_KEY = 'pageLoadCompare.form';
  const SETTING_FIELDS = ['timeoutSec', 'timeLimitSec', 'expectStatus', 'budgetP95Ms', 'screen', 'viewportWidth', 'viewportHeight', 'locale', 'screenshots'];
  const FORM_FIELDS = ['url', 'compareUrl', 'vus', 'iterations', 'waitUntil', 'apiOnly', 'method', 'headers', 'body', ...SETTING_FIELDS, 'resetSettings'];
  // Headers and body may hold tokens, so they are never saved in the browser.
  const SAVED_FIELDS = ['url', 'compareUrl', 'vus', 'iterations', 'waitUntil', 'apiOnly', 'method', ...SETTING_FIELDS];

  let CONFIG = null;          // from GET /api/config
  let SETTING_DEFAULTS = {};  // form values that mean "default"
  let polling = null;
  let lastResultsKey = '';

  // ---------- formatting ----------
  const fmtMs = (v) => (v == null ? '—' : v >= 10000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);
  const fmtSec = (v) => (v == null ? '—' : `${(v / 1000).toFixed(2)} s`);
  const fmtSize = (v) => (v == null ? '—' : v >= 1048576 ? `${(v / 1048576).toFixed(1)} MB` : `${Math.round(v / 1024)} KB`);
  const fmtDur = (ms) => {
    if (ms == null) return '—';
    const s = Math.round(ms / 1000), m = Math.floor(s / 60);
    return m ? `${m}m ${s % 60}s` : `${s}s`;
  };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const hostOf = (u) => { try { return new URL(u).host; } catch { return u; } };
  const verdictClass = (d) => (d && d.verdict) || 'same';
  const modeOf = (api) => CONFIG.modes[api ? 'api' : 'browser'];

  // ---------- form ----------
  function readSettings() {
    const num = (id) => ($(id).value.trim() === '' ? null : Number($(id).value));
    const screen = $('screen').value;
    const [w, h] = screen === 'custom' ? [num('viewportWidth'), num('viewportHeight')] : screen.split('x').map(Number);
    return {
      timeoutSec: num('timeoutSec'),
      timeLimitSec: num('timeLimitSec'),
      expectStatus: $('expectStatus').value.trim(),
      budgetP95Ms: num('budgetP95Ms'),
      viewportWidth: w,
      viewportHeight: h,
      locale: $('locale').value.trim(),
      screenshots: $('screenshots').checked,
    };
  }

  function readForm() {
    return {
      url: $('url').value.trim(),
      compareUrl: $('compareUrl').value.trim(),
      vus: Number($('vus').value),
      iterations: Number($('iterations').value),
      waitUntil: $('waitUntil').value,
      apiOnly: $('apiOnly').checked,
      method: $('method').value,
      headers: $('headers').value,
      body: $('body').value,
      settings: readSettings(),
    };
  }

  const fieldValue = (id) => ($(id).type === 'checkbox' ? $(id).checked : $(id).value);
  function setField(id, value) {
    if ($(id).type === 'checkbox') $(id).checked = value === true;
    else $(id).value = value;
  }

  function changedSettings() {
    return SETTING_FIELDS
      .filter((k) => !['viewportWidth', 'viewportHeight'].includes(k) || $('screen').value === 'custom')
      .filter((k) => String(fieldValue(k)) !== String(SETTING_DEFAULTS[k]));
  }

  // Show the fields and limits of the chosen mode.
  function applyMode() {
    const api = $('apiOnly').checked;
    const mode = modeOf(api);
    $('apiFields').hidden = !api;
    $('waitUntilField').hidden = api;
    $('browserSettings').hidden = api;
    $('iterationsLabel').textContent = `Total ${mode.noun} per URL`;
    $('timeoutLabel').textContent = `Timeout per ${mode.nounSingular} (s)`;
    $('vus').max = mode.limits.vus;
    $('iterations').max = mode.limits.iterations;
    $('expectStatus').placeholder = mode.defaultExpectStatus;
    $('customSize').hidden = $('screen').value !== 'custom';
    const n = changedSettings().length;
    $('advChanged').textContent = n ? `· ${n} changed` : '';
  }

  // Above the recommended users/visits the run is allowed, with a warning.
  // (Same rules as RunConfig.warnings on the server, which reports them again.)
  function formWarnings(f) {
    const mode = modeOf(f.apiOnly);
    const limits = mode.limits;
    const notes = [];
    if (f.vus > limits.recommendedVus && f.vus <= limits.vus) {
      notes.push(`${f.vus} parallel users is more than the recommended ${limits.recommendedVus} for ${mode.kind} tests: ${limits.loadNote}.`);
    }
    if (f.iterations > limits.recommendedIterations && f.iterations <= limits.iterations) {
      notes.push(`${f.iterations} ${mode.noun} per URL is more than the recommended ${limits.recommendedIterations}: the run will take a long time.`);
    }
    return notes;
  }

  function showWarnings(id, notes) {
    $(id).innerHTML = notes.map((n) => `<p>⚠ ${esc(n)}</p>`).join('');
    $(id).hidden = notes.length === 0;
  }

  function settingsSummary(f) {
    const st = f.settings, parts = [];
    const d = CONFIG.settings.defaults;
    if (st.timeoutSec != null) parts.push(`timeout ${st.timeoutSec} s`);
    if (st.timeLimitSec) parts.push(`limit ${st.timeLimitSec} s`);
    if (st.expectStatus) parts.push(`status ${esc(st.expectStatus)}`);
    if (st.budgetP95Ms) parts.push(`budget p95 ≤ ${st.budgetP95Ms} ms`);
    if (!f.apiOnly) {
      if (st.viewportWidth !== d.viewportWidth || st.viewportHeight !== d.viewportHeight) parts.push(`screen ${st.viewportWidth}×${st.viewportHeight}`);
      if (st.locale) parts.push(`language ${esc(st.locale)}`);
      if (!st.screenshots) parts.push('no screenshots');
    }
    return parts.length ? ` Settings: ${parts.join(', ')}.` : '';
  }

  function updateSummary() {
    applyMode();
    const f = readForm();
    const mode = modeOf(f.apiOnly);
    const users = `${f.vus} parallel ${f.vus === 1 ? 'user' : 'users'}`;
    $('formSummary').innerHTML = f.vus && f.iterations
      ? `${f.apiOnly ? `API test (${esc(f.method)}, no browser): each` : 'Each'} URL gets <b>${f.iterations}</b> ${mode.noun} shared by <b>${users}</b>` +
        (f.compareUrl ? ' — URLs run one after the other, so they don’t slow each other down.' : '.') +
        settingsSummary(f)
      : '';
    showWarnings('formWarning', formWarnings(f));
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(Object.fromEntries(SAVED_FIELDS.map((k) => [k, fieldValue(k)]))));
    } catch { /* storage unavailable: the form still works */ }
  }

  function restoreForm() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
      if (saved) {
        SAVED_FIELDS.forEach((k) => {
          if (saved[k] == null) return;
          if (saved[k] === '' && $(k).type !== 'checkbox' && !(k in SETTING_DEFAULTS)) return;
          setField(k, saved[k]);
        });
      }
    } catch { /* ignore bad saved data */ }
    if (!$('url').value) $('url').value = 'https://hartak.am/';
    updateSummary();
  }

  // Quick checks for instant feedback. The server validates everything again
  // (formats such as status codes, language and headers) and its message is shown.
  function validate(f) {
    const urlOk = (u) => /^https?:\/\/[^\s/]+/i.test(u);
    if (!f.url) return 'Enter a URL.';
    if (!urlOk(f.url)) return 'URL must start with http:// or https://';
    if (f.compareUrl && !urlOk(f.compareUrl)) return 'Compare URL must start with http:// or https://';
    const mode = modeOf(f.apiOnly);
    const { vus, iterations } = mode.limits;
    if (!Number.isInteger(f.vus) || f.vus < 1 || f.vus > vus) return `Parallel users must be 1–${vus} (the maximum for ${mode.kind} tests).`;
    if (!Number.isInteger(f.iterations) || f.iterations < 1 || f.iterations > iterations) return `Total ${mode.noun} must be 1–${iterations} (the maximum).`;
    if (f.vus > f.iterations) return `Parallel users (${f.vus}) can’t be more than total ${mode.noun} (${f.iterations}).`;
    for (const [key, [low, high]] of Object.entries(CONFIG.settings.ranges)) {
      const value = f.settings[key];
      if (value != null && (!Number.isInteger(value) || value < low || value > high)) {
        return `${$(key)?.getAttribute('aria-label') || key} must be ${low}–${high}.`;
      }
    }
    return null;
  }

  function showError(msg) {
    $('formError').textContent = msg || '';
    $('formError').hidden = !msg;
  }

  function setRunning(running) {
    FORM_FIELDS.forEach((k) => { $(k).disabled = running; });
    $('runBtn').disabled = running;
    $('runBtn').textContent = running ? 'Running…' : 'Run';
    $('stopBtn').hidden = !running;
  }

  async function startRun(event) {
    event.preventDefault();
    const f = readForm();
    const problem = validate(f);
    showError(problem);
    if (problem) return;
    try {
      const res = await fetch('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(f) });
      const data = await res.json();
      if (!res.ok) return showError(data.error || 'Could not start the run.');
      $('results').hidden = true;
      lastResultsKey = '';
      startPolling();
    } catch {
      showError('Cannot reach the app server. Is it still running?');
    }
  }

  async function stopRun() {
    $('stopBtn').disabled = true;
    try { await fetch('/api/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); } catch { /* retry by clicking again */ }
    $('stopBtn').disabled = false;
  }

  // ---------- status ----------
  function startPolling() {
    clearInterval(polling);
    poll();
    polling = setInterval(poll, 1000);
  }

  async function poll() {
    let data;
    try { data = await (await fetch('/api/status', { cache: 'no-store' })).json(); } catch { return; }

    $('k6Badge').className = `badge ${data.k6 ? 'ok' : 'bad'}`;
    $('k6Badge').textContent = data.k6 ? 'k6 ready' : 'k6 not found';

    const job = data.job;
    if (!job) { setRunning(false); clearInterval(polling); return; }

    const running = job.state === 'running';
    setRunning(running);
    if (!running) clearInterval(polling);
    if (job.state === 'error') showError(job.message);

    $('emptyState').hidden = true;
    renderProgress(job);
    renderLog(job);
    if (job.targets.some((t) => t.stats || t.problem)) renderResults(job);
    else if (!running) $('results').hidden = true;
  }

  function progressTitle(job) {
    const time = fmtDur(job.elapsedMs);
    if (job.state === 'running') return `Running · ${time}`;
    if (job.state === 'stopped') return `Stopped after ${time}`;
    if (job.state === 'problem') return `Finished with problems · ${time}`;
    if (job.state === 'error') return 'Could not run';
    if (job.targets.some((t) => t.budget && !t.budget.passed)) return `Finished · over budget · ${time}`;
    return `Finished in ${time}`;
  }

  function renderProgress(job) {
    $('progressCard').hidden = false;
    $('progressTitle').textContent = progressTitle(job);
    showWarnings('runWarning', job.warnings || []);
    const noun = job.noun;
    $('progressList').innerHTML = job.targets.map((t, i) => {
      const pct = Math.min(100, (t.done / t.total) * 100);
      const label = {
        running: `${t.done} / ${t.total} ${noun}`, waiting: 'waiting', done: `${t.done} / ${t.total} ${noun} · done`,
        stopped: `stopped at ${t.done} / ${t.total}`, error: 'error',
        failed: `⚠ failed · ${t.done} / ${t.total} ${noun}`, incomplete: `⚠ incomplete · ${t.done} / ${t.total} ${noun}`,
      }[t.phase] || t.phase;
      const bad = t.phase === 'failed' || t.phase === 'incomplete';
      const side = i ? 'b' : 'a';
      return `<div>
        <div class="progress-head"><span class="url"><span class="dot ${side} inline"></span>${esc(t.url)}</span><span class="count ${bad ? 'bad' : ''}">${label}</span></div>
        <div class="track" role="progressbar" aria-valuemin="0" aria-valuemax="${t.total}" aria-valuenow="${t.done}"><div class="fill ${side} ${t.phase === 'running' ? 'running' : ''}" style="width:${pct}%"></div></div>
        ${t.problem ? `<div class="problem">${esc(t.problem)}</div>` : ''}
      </div>`;
    }).join('');
  }

  function renderLog(job) {
    $('logBox').hidden = job.log.length === 0;
    $('log').textContent = job.log.join('\n');
  }

  // ---------- results ----------
  function siteCard(t, i, job) {
    const api = job.mode === 'api';
    const mainKey = job.mainMetric;
    const mainLabel = job.mainLabel;
    if (!t.stats) {
      if (!t.problem) return '';
      return `<div class="card site failed">
        <div class="site-head"><span class="name">${esc(hostOf(t.url))}</span></div>
        <div class="problem flush">${esc(t.problem)}</div>
        <p class="muted-note">No results to compare. Open “k6 output” below for details.</p>
      </div>`;
    }
    const s = t.stats;
    const main = s.timings[mainKey];
    const ok = s.failed === 0 && s.visits === t.total;
    const time = api ? fmtMs : fmtSec;
    return `<div class="card site ${i ? 'b' : ''}">
      <div class="site-head">
        <span class="name">${esc(hostOf(t.url))}</span>
        <span class="site-links">
          ${s.report ? `<a href="${esc(s.report)}" target="_blank" rel="noopener">Full report ↗</a>` : ''}
          ${s.download ? `<a href="${esc(s.download)}" download title="ZIP: report, visits.csv, raw k6 output and screenshots of this URL">Download ↓</a>` : ''}
        </span>
      </div>
      <div class="kpis">
        <div class="kpi"><div class="k">${esc(mainLabel)} · median${hint(TIMING_HINT[mainKey])}</div><div class="v">${time(main.med)}</div></div>
        <div class="kpi"><div class="k">${esc(mainLabel)} · p95${hint('p95')}</div><div class="v">${time(main.p95)}</div></div>
        <div class="kpi"><div class="k">${api ? 'Requests' : 'Visits'} passed${hint('passed')}</div><div class="v ${ok ? 'good' : 'bad'}">${s.passed} / ${t.total}</div></div>
        <div class="kpi"><div class="k">Server response · median${hint('ttfb')}</div><div class="v">${fmtMs(s.timings.ttfbMs.med)}</div></div>
        <div class="kpi"><div class="k">Run time${hint('runtime')}</div><div class="v">${fmtDur(s.runtimeMs)}</div></div>
        <div class="kpi"><div class="k">${api ? 'Response size' : 'Transferred'} · median${hint(api ? 'size' : 'transfer')}</div><div class="v">${fmtSize(s.transferMed)}</div></div>
      </div>
      ${t.budget ? `<div class="budget ${t.budget.passed ? 'ok' : 'bad'}">${t.budget.passed ? '✓' : '✗'} Budget: ${esc(mainLabel.toLowerCase())} p95 ${fmtMs(t.budget.p95Ms)} ${t.budget.passed ? '≤' : '>'} ${fmtMs(t.budget.limitMs)}</div>` : ''}
      ${t.problem ? `<div class="problem">${esc(t.problem)}</div>` : ''}
      ${s.diagnosis ? diagnosisBlock(s.diagnosis, s.report)
        : s.errors.length ? `<ul class="errors">${s.errors.map((e) => `<li>${api ? 'Request' : 'Visit'} ${e.visit}: ${esc(e.error)}</li>`).join('')}</ul>` : ''}
    </div>`;
  }

  // "Possible reasons": failures by type, then each finding (click to see reasons and what to check).
  function diagnosisBlock(d, report) {
    const serious = d.findings.filter((f) => f.level !== 'info');
    if (!d.findings.length) return '';
    const chips = d.categories.map((c) => `<span class="chip" title="${esc(c.meaning)} First ${esc(c.first)}, last ${esc(c.last)}.">${esc(c.label)} <b>${c.count}</b></span>`).join('');
    const items = d.findings.map((f) => `
      <details class="finding ${esc(f.level)}">
        <summary><span class="lvl">${esc(f.level === 'info' ? 'note' : f.level)}</span>${esc(f.title)}</summary>
        <p>${esc(f.detail)}</p>
        ${f.evidence.length ? `<ul class="evidence">${f.evidence.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        ${f.reasons.length ? `<div class="sub">Possible reasons</div><ul>${f.reasons.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        ${f.checks.length ? `<div class="sub">What to check</div><ul>${f.checks.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
      </details>`).join('');
    return `<div class="diagnosis">
      <div class="diag-head ${serious.length ? 'bad' : ''}">${serious.length ? 'Possible reasons' : 'Notes'}${report ? ` · <a href="${esc(report)}#reasons" target="_blank" rel="noopener">timeline and details ↗</a>` : ''}</div>
      ${chips ? `<div class="chips">${chips}</div>` : ''}
      ${items}
    </div>`;
  }

  function timingDescriptions(job) {
    return Object.fromEntries(CONFIG.modes[job.mode].timings.map(([field, , description]) => [field, description]));
  }

  function renderComparison(job, cmp) {
    const api = job.mode === 'api';
    const main = cmp.main;
    const descriptions = timingDescriptions(job);
    const badge = (d) => `<span class="diff ${verdictClass(d)}">${esc(d.text)}</span>`;
    const big = main.verdict === 'same' ? '=' : main.verdict ? main.text.split(' ')[0] : '—';

    $('verdict').hidden = false;
    $('verdict').innerHTML = `<div class="big">${esc(big)}</div>
      <div class="text">${esc(cmp.headline)} Median ${fmtMs(main.b)} vs ${fmtMs(main.a)}, with ${job.iterations} ${job.noun} each by ${job.vus} parallel users${api ? `, ${esc(job.method)} without a browser` : ''}.</div>`;

    $('tableTitle').textContent = `${cmp.b} compared with ${cmp.a} · lower times are better`;
    const head = `<thead><tr>
        <th>Timing</th>
        <th class="a">${esc(cmp.a)} · median${hint('median')}</th><th class="b">${esc(cmp.b)} · median${hint('median')}</th><th class="b">${esc(cmp.b)} is…${hint('compare')}</th>
        <th class="a">p95${hint('p95')}</th><th class="b">p95${hint('p95')}</th><th class="b">${esc(cmp.b)} is…${hint('compare')}</th>
      </tr></thead>`;
    const rows = cmp.rows.map((row) => {
      if (row.median) {
        return `<tr class="${row.metric === job.mainMetric ? 'main' : ''}">
          <td>${esc(row.label)}${hint(TIMING_HINT[row.metric])}<span class="desc">${esc(descriptions[row.metric] || '')}</span></td>
          <td>${fmtMs(row.a.median)}</td><td>${fmtMs(row.b.median)}</td><td>${badge(row.median)}</td>
          <td>${fmtMs(row.a.p95)}</td><td>${fmtMs(row.b.p95)}</td><td>${badge(row.p95)}</td>
        </tr>`;
      }
      const isRuntime = row.metric === 'runtimeMs';
      const fmt = isRuntime ? fmtDur : fmtSize;
      const desc = isRuntime ? `all ${job.iterations} ${job.noun}` : `median per ${api ? 'request' : 'visit'}`;
      const hintKey = isRuntime ? 'runtime' : api ? 'size' : 'transfer';
      return `<tr><td>${esc(row.label)}${hint(hintKey)}<span class="desc">${esc(desc)}</span></td>
        <td>${fmt(row.a.value)}</td><td>${fmt(row.b.value)}</td><td>${badge(row.value)}</td><td></td><td></td><td></td></tr>`;
    }).join('');
    $('compareTable').innerHTML = `${head}<tbody>${rows}</tbody>`;
    $('legend').innerHTML = `<span>Result: ${esc(cmp.summary)}</span>`;
  }

  function renderSingle(job, target) {
    const descriptions = timingDescriptions(job);
    $('verdict').hidden = true;
    $('tableTitle').textContent = `Timings · ${hostOf(target.url)}`;
    const head = `<thead><tr><th>Timing</th><th>Median${hint('median')}</th><th>p90${hint('p90')}</th><th>p95${hint('p95')}</th><th>Average${hint('avg')}</th><th>Min${hint('min')}</th><th>Max${hint('max')}</th></tr></thead>`;
    const rows = job.timings.map(([field]) => {
      const x = target.stats.timings[field];
      return `<tr class="${field === job.mainMetric ? 'main' : ''}"><td>${esc(x.label)}${hint(TIMING_HINT[field])}<span class="desc">${esc(descriptions[field] || '')}</span></td>
        <td>${fmtMs(x.med)}</td><td>${fmtMs(x.p90)}</td><td>${fmtMs(x.p95)}</td><td>${fmtMs(x.avg)}</td><td>${fmtMs(x.min)}</td><td>${fmtMs(x.max)}</td></tr>`;
    }).join('');
    $('compareTable').innerHTML = `${head}<tbody>${rows}</tbody>`;
    $('legend').innerHTML = '';
  }

  // One ZIP for the whole run: comparison.html, comparison.csv, result.json and every URL's files.
  function renderDownloadBar(job) {
    const ready = Boolean(job.download) && job.targets.some((t) => t.stats);
    $('resultsBar').hidden = !ready;
    if (!ready) return;
    const compare = Boolean(job.comparison);
    $('downloadAll').href = job.download;
    $('downloadAllLabel').textContent = compare ? 'Download comparison' : 'Download results';
    $('downloadAll').title = compare
      ? 'ZIP: comparison page, comparison.csv, result.json and both runs (reports, visits.csv, screenshots)'
      : 'ZIP: summary page, timings.csv, result.json and the run (report, visits.csv, screenshots)';
    $('resultsBarText').textContent = compare
      ? `${hostOf(job.targets[1].url)} vs ${hostOf(job.targets[0].url)} · run ${job.id}`
      : `${hostOf(job.targets[0].url)} · run ${job.id}`;
  }

  function renderResults(job) {
    const key = JSON.stringify([job.targets.map((t) => [t.stats, t.problem, t.budget]), job.comparison, job.download]);
    if (key === lastResultsKey) return;   // unchanged: keep open hints open
    lastResultsKey = key;
    hideTip();
    $('results').hidden = false;
    renderDownloadBar(job);
    $('sites').innerHTML = job.targets.map((t, i) => siteCard(t, i, job)).join('');
    const withStats = job.targets.filter((t) => t.stats);
    $('tableCard').hidden = withStats.length === 0;
    if (job.comparison) renderComparison(job, job.comparison);
    else if (withStats.length) renderSingle(job, withStats[0]);
    else $('verdict').hidden = true;
  }

  // ---------- hints ----------
  const HINTS = {
    ttfb: ['TTFB · Time to First Byte',
      'How long the server takes to start sending the page after the browser asks for it. Mostly server speed and network distance, before anything can appear on screen.',
      'Good: under 800 ms'],
    fcp: ['FCP · First Contentful Paint',
      'When the first text or image appears on screen. Until then the visitor sees a blank page.',
      'Good: under 1.8 s'],
    lcp: ['LCP · Largest Contentful Paint',
      'When the biggest visible element (main heading, banner or image) is shown. The best single signal that the page looks ready to the visitor.',
      'Good: under 2.5 s'],
    dom: ['DOM loaded · DOMContentLoaded',
      'The HTML is fully read and the page structure is built. Images, fonts and some styles may still be loading.', null],
    load: ['Load · load event',
      'The browser finished downloading everything the page asked for at the start: scripts, CSS, images and fonts. The spinner in the browser tab stops here.', null],
    full: ['Fully loaded',
      'The later of the load event and the last network request finishing, so it also counts data the page loads after the load event. The main number in browser mode.', null],
    runtime: ['Run time',
      'Total wall-clock time to finish all visits for this URL with the chosen number of parallel users. A slower site takes longer for the same number of visits.', null],
    transfer: ['Transferred',
      'Data downloaded in one visit (median). Files from other domains may count as 0 if their server hides the size, so the real total can be higher.', null],
    median: ['Median', 'The middle value: half of the visits were faster, half were slower. Shows what a typical visitor gets.', null],
    p90: ['p90 · 90th percentile', '90% of visits were faster than this, and the slowest 10% were slower. With 30 visits, that is the 3 slowest.', null],
    p95: ['p95 · 95th percentile',
      '95% of visits were faster than this; only the slowest 5% were slower. Shows what unlucky visitors experience. Performance targets usually use p95.', null],
    avg: ['Average', 'Sum of all visit times divided by the number of visits. One very slow visit can pull it up, so the median is usually more reliable.', null],
    min: ['Min', 'The fastest visit in this run.', null],
    max: ['Max', 'The slowest visit in this run. Often random noise, so look at p95 for a steadier "slow" number.', null],
    connect: ['Connect', 'Time to open the network connection to the server. Each user keeps its connection open, so this is usually 0 after its first request.', null],
    tls: ['TLS', 'Time for the https security handshake. Like Connect, usually only on a user’s first request.', null],
    receive: ['Receive', 'Time to download the answer after the first byte arrived. Grows with response size and network speed.', null],
    total: ['Total',
      'The whole request from start to finish: waiting for a connection, connect, TLS, sending, waiting for the server (TTFB) and downloading. The main number in API mode.', null],
    size: ['Response size', 'Size of the answer (median), from the Content-Length header or the received body.', null],
    passed: ['Passed',
      'Visits or requests that got the expected HTTP status and finished, out of all you asked for. Ones that failed or hung count as not passed.', null],
    compare: ['Comparison badge', 'How the second URL did against the first for that row: “2.0× slower” means it took twice as long.', null],
    setTimeout: ['Timeout', 'How long one visit (browser) or request (API) may take before it counts as failed. Empty = 30 seconds.', null],
    setLimit: ['Time limit per URL',
      'Maximum time for all visits to one URL. Visits still running then are stopped and reported as hung. Empty = automatic, based on users, visits and timeout.', null],
    setStatus: ['Expected HTTP status',
      'Which answers count as success: an exact code (200), a class (2xx = any 200–299) or a list (200,301). Empty = 200 in browser mode, 2xx in API mode.', null],
    setBudget: ['Performance budget',
      'Optional goal for the main number (fully loaded, or total in API mode): if its p95 is above this many milliseconds, the URL is marked as over budget and the run is not OK. Empty = no budget.', null],
    setScreen: ['Screen size', 'Browser window size used for every visit. Pages can load different images and layouts on mobile and desktop, so timings may differ.', null],
    setLocale: ['Browser language',
      'Language the browser reports to the site (Accept-Language), e.g. hy-AM for Armenian or en-US for English. Empty = Chrome default.', null],
  };
  const TIMING_HINT = {
    ttfbMs: 'ttfb', fcpMs: 'fcp', lcpMs: 'lcp', domMs: 'dom', loadMs: 'load', fullyLoadedMs: 'full',
    connectMs: 'connect', tlsMs: 'tls', receiveMs: 'receive', totalMs: 'total',
  };

  function hint(key) {
    const h = HINTS[key];
    return h ? `<span class="hint" tabindex="0" role="button" aria-label="What is ${esc(h[0])}?" data-hint="${key}">i</span>` : '';
  }

  let tipAnchor = null;

  function showTip(el) {
    const h = HINTS[el.dataset.hint];
    if (!h) return;
    const tip = $('tip');
    tip.innerHTML = `<b>${esc(h[0])}</b>${esc(h[1])}${h[2] ? `<span class="good-value">${esc(h[2])}</span>` : ''}`;
    tip.hidden = false;
    if (tipAnchor && tipAnchor !== el) tipAnchor.setAttribute('aria-expanded', 'false');
    tipAnchor = el;
    el.setAttribute('aria-expanded', 'true');

    // Below the icon, or above if there is no room; always inside the window.
    const r = el.getBoundingClientRect();
    const t = tip.getBoundingClientRect();
    const gap = 8, edge = 12;
    const left = Math.max(edge, Math.min(r.left + r.width / 2 - t.width / 2, window.innerWidth - t.width - edge));
    let top = r.bottom + gap;
    if (top + t.height > window.innerHeight - edge) top = r.top - t.height - gap;
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(edge, top)}px`;
  }

  function hideTip() {
    $('tip').hidden = true;
    if (tipAnchor) tipAnchor.setAttribute('aria-expanded', 'false');
    tipAnchor = null;
  }

  function bindHints() {
    document.addEventListener('mouseover', (e) => { const el = e.target.closest('.hint'); if (el) showTip(el); });
    document.addEventListener('mouseout', (e) => {
      const el = e.target.closest('.hint');
      if (el && !el.contains(e.relatedTarget)) hideTip();
    });
    document.addEventListener('focusin', (e) => { const el = e.target.closest('.hint'); if (el) showTip(el); });
    document.addEventListener('focusout', (e) => { if (e.target.closest('.hint')) hideTip(); });
    // Tap on touch screens opens a hint; tapping anywhere else closes it.
    document.addEventListener('click', (e) => { const el = e.target.closest('.hint'); if (el) showTip(el); else hideTip(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') hideTip();
      if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('.hint')) { e.preventDefault(); showTip(e.target); }
    });
    window.addEventListener('scroll', hideTip, { passive: true, capture: true });
    window.addEventListener('resize', hideTip);
  }

  // ---------- start ----------
  async function init() {
    try {
      CONFIG = await (await fetch('/api/config', { cache: 'no-store' })).json();
    } catch {
      showError('Cannot reach the app server. Is it still running?');
      return;
    }
    const d = CONFIG.settings.defaults;
    // Empty number fields mean "use the server default".
    SETTING_DEFAULTS = {
      timeoutSec: '', timeLimitSec: '', expectStatus: '', budgetP95Ms: '',
      screen: `${d.viewportWidth}x${d.viewportHeight}`,
      viewportWidth: String(d.viewportWidth), viewportHeight: String(d.viewportHeight),
      locale: d.locale, screenshots: d.screenshots,
    };

    $('runForm').addEventListener('input', updateSummary);
    $('runForm').addEventListener('change', updateSummary);
    $('runForm').addEventListener('submit', startRun);
    $('stopBtn').addEventListener('click', stopRun);
    $('resetSettings').addEventListener('click', () => {
      SETTING_FIELDS.forEach((k) => setField(k, SETTING_DEFAULTS[k]));
      updateSummary();
    });
    bindHints();
    restoreForm();
    startPolling();
  }

  init();
})();
