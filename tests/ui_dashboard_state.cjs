// Run with: node tests/ui_dashboard_state.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const listeners = new Map();
const stores = new Map();
const components = new Map();
const pending = new Map();
function defer(url) {
  return new Promise((resolve, reject) => {
    const queue = pending.get(url) || [];
    queue.push({ resolve, reject });
    pending.set(url, queue);
  });
}
function answer(url, value) {
  const request = pending.get(url)?.shift();
  assert.ok(request, `Missing request for ${url}`);
  request.resolve(value);
}
function fail(url) {
  const request = pending.get(url)?.shift();
  assert.ok(request, `Missing request for ${url}`);
  request.reject(new Error('service unavailable'));
}
const Alpine = {
  store(name, value) {
    if (arguments.length === 2) stores.set(name, value);
    return stores.get(name);
  },
  data(name, factory) { components.set(name, factory); },
};
const api = {
  get(url) { return url === '/info' ? Promise.resolve({ version: 'test' }) : defer(url); },
  post(url) { return defer(`POST ${url}`); },
};
const context = {
  Alpine, api,
  document: { addEventListener(name, callback) { listeners.set(name, callback); } },
  window: { addEventListener() {} },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  setTimeout() { return 0; }, clearTimeout() {}, setInterval, clearInterval, console,
};
const web = path.join(__dirname, '..', 'donedatahoarder', 'web');
vm.runInNewContext(fs.readFileSync(path.join(web, 'static', 'app.js'), 'utf8'), context);
listeners.get('alpine:init')();

(async () => {
  const session = stores.get('session');
  const dashboard = components.get('dashboard')();
  session.current_session_id = 'A';
  const statsA = { total_files: 1321, total_size_bytes: 4000,
    by_status: { analyzed: 1, proposed: 781, skipped: 539 },
    proposal_counts: { pending: 869 }, duplicate_groups: 0, by_extension: [] };

  let load = dashboard.load();
  answer('/stats?session_id=A', statsA);
  answer('/pipeline/jobs/active', { job_id: null });
  answer('/pipeline/runs/latest?session_id=A', { plan: null });
  await load;
  assert.equal(dashboard.executionState(), 'Idle');
  assert.equal(dashboard.executionSummary(), 'No active app processing');
  assert.equal(dashboard.reviewWaiting(), '869 suggestions awaiting review');
  assert.deepEqual(Array.from(dashboard.fileStatusDistribution(), x => x.count), [1, 781, 539]);
  assert.equal(dashboard.fileStatusDistribution()[1].share, '59.1%');

  let status = dashboard.loadExecutionStatus();
  answer('/pipeline/jobs/active', { job_id: 'other', session_id: 'B', state: 'running' });
  answer('/pipeline/runs/latest?session_id=A', { plan: null });
  await status;
  assert.equal(dashboard.executionState(), 'Idle', 'another session must not appear active');

  for (const state of ['running', 'paused', 'cancelling']) {
    status = dashboard.loadExecutionStatus();
    answer('/pipeline/jobs/active', { job_id: 'job-A', session_id: 'A', state, job_type: 'analyze' });
    answer('/pipeline/runs/latest?session_id=A', { plan: null });
    await status;
    assert.equal(dashboard.executionState(), state[0].toUpperCase() + state.slice(1));
  }

  status = dashboard.loadExecutionStatus();
  answer('/pipeline/jobs/active', { job_id: null });
  answer('/pipeline/runs/latest?session_id=A', { plan: { state: 'failed' } });
  await status;
  assert.equal(dashboard.executionState(), 'Failed');
  assert.match(dashboard.executionSummary(), /latest managed run failed/i);

  status = dashboard.loadExecutionStatus();
  answer('/pipeline/jobs/active', { job_id: null });
  answer('/pipeline/runs/latest?session_id=A', { plan: { state: 'cancelling' } });
  await status;
  assert.equal(dashboard.executionState(), 'Cancelling');

  for (const planState of ['ready', 'completed']) {
    status = dashboard.loadExecutionStatus();
    answer('/pipeline/jobs/active', { job_id: null });
    answer('/pipeline/runs/latest?session_id=A', { plan: { state: planState } });
    await status;
    assert.equal(dashboard.executionState(), 'Idle');
  }

  status = dashboard.loadExecutionStatus();
  fail('/pipeline/jobs/active');
  answer('/pipeline/runs/latest?session_id=A', { plan: null });
  await status;
  assert.equal(dashboard.executionState(), 'Unavailable');
  status = dashboard.loadExecutionStatus();
  const samePoll = dashboard.loadExecutionStatus();
  assert.equal(pending.get('/pipeline/jobs/active').length, 1,
    'polling must not supersede a slow request for the same session');
  answer('/pipeline/jobs/active', { job_id: null });
  fail('/pipeline/runs/latest?session_id=A');
  await Promise.all([status, samePoll]);
  assert.equal(dashboard.executionState(), 'Unavailable',
    'a missing managed-run response is not proof of idle status');
  status = dashboard.loadExecutionStatus();
  answer('/pipeline/jobs/active', { job_id: 'job-A', session_id: 'A', state: 'paused' });
  fail('/pipeline/runs/latest?session_id=A');
  await status;
  assert.equal(dashboard.executionState(), 'Paused',
    'a live session job remains authoritative when run-plan lookup fails');
  stores.get('app').foregroundOperation = { session_id: 'A', step: 'execute-commit' };
  assert.equal(dashboard.executionState(), 'Running', 'local synchronous action is visible');
  stores.get('app').foregroundOperation = null;

  // A response arriving after switching to B must not replace B's stats or status.
  load = dashboard.load();
  session.current_session_id = 'B';
  const loadB = dashboard.load();
  answer('/stats?session_id=B', { ...statsA, total_files: 2, proposal_counts: { pending: 0 } });
  answer('/pipeline/jobs/active', { job_id: null }); // stale A
  answer('/pipeline/runs/latest?session_id=A', { plan: { state: 'failed' } });
  answer('/pipeline/jobs/active', { job_id: null }); // B
  answer('/pipeline/runs/latest?session_id=B', { plan: null });
  await loadB;
  answer('/stats?session_id=A', statsA);
  await load;
  assert.equal(dashboard.stats.total_files, 2);
  assert.equal(dashboard.executionState(), 'Idle');
  assert.equal(dashboard.reviewWaiting(), '');

  // Pipeline's global active-job endpoint must also remain session scoped.
  const pipeline = components.get('pipeline')();
  let connected = 0;
  let closed = 0;
  pipeline._connectJobStream = () => { connected += 1; };
  session.current_session_id = 'B';
  let reconnect = pipeline.checkActiveJob();
  answer('/pipeline/jobs/active', { job_id: 'A-job', session_id: 'A', state: 'running' });
  await reconnect;
  assert.equal(connected, 0);
  session.current_session_id = 'A';
  reconnect = pipeline.checkActiveJob();
  answer('/pipeline/jobs/active', { job_id: 'A-job', session_id: 'A', state: 'running', job_type: 'analyze' });
  await reconnect;
  assert.equal(connected, 1);
  pipeline._eventSource = { close() { closed += 1; } };
  session.current_session_id = 'B';
  reconnect = pipeline.checkActiveJob();
  answer('/pipeline/jobs/active', { job_id: 'A-job', session_id: 'A', state: 'running' });
  await reconnect;
  assert.equal(pipeline.activeJobId, null);
  assert.equal(closed, 1);

  const retry = pipeline.retryAnalysisErrors();
  answer('POST /pipeline/analyze', { job_id: 'B-retry' });
  await retry;
  assert.equal(pipeline.activeJobSessionId, 'B');
  assert.equal(connected, 2, 'retry stream must be bound to the selected session');
  pipeline._eventSource = { close() { closed += 1; } };
  reconnect = pipeline.checkActiveJob();
  answer('/pipeline/jobs/active', { job_id: null });
  await reconnect;
  assert.equal(pipeline.activeJobId, null, 'a vanished same-session job must clear stale state');
  assert.equal(closed, 2);

  const lateStart = pipeline._startBackgroundJob('enrich', { session_id: 'B' });
  session.current_session_id = 'A';
  answer('POST /pipeline/enrich', { job_id: 'B-late' });
  await lateStart;
  assert.equal(connected, 2, 'a late start response must not attach to another session');

  // Alpine may return a new reactive proxy for a nested store object. Cleanup
  // must compare a stable token, not object identity from the store getter.
  const rawApp = stores.get('app');
  stores.set('app', new Proxy(rawApp, {
    get(target, key) {
      const value = Reflect.get(target, key);
      return key === 'foregroundOperation' && value ? new Proxy(value, {}) : value;
    },
  }));
  pipeline._refreshAfterStep = async () => {};
  session.current_session_id = 'B';
  session.root_path = 'C:/collection';
  let scan = pipeline.runStep('scan');
  assert.equal(stores.get('app').foregroundOperation.step, 'scan');
  answer('POST /pipeline/scan', { new: 1, skipped: 0 });
  await scan;
  assert.equal(stores.get('app').foregroundOperation, null, 'successful scan must clear proxy-wrapped operation');
  assert.equal(stores.get('app').loading, false);

  scan = pipeline.runStep('scan');
  fail('POST /pipeline/scan');
  await scan;
  assert.equal(stores.get('app').foregroundOperation, null, 'failed scan must clear proxy-wrapped operation');
  assert.equal(stores.get('app').loading, false);

  let background = pipeline.runStep('enrich');
  session.current_session_id = 'A';
  answer('POST /pipeline/enrich', { job_id: 'B-late-again' });
  await background;
  assert.equal(stores.get('app').loading, false, 'late background start must clear loading');
  assert.equal(pipeline.running, null);

  session.current_session_id = 'B';
  background = pipeline.runStep('enrich');
  fail('POST /pipeline/enrich');
  await background;
  assert.equal(stores.get('app').loading, false, 'rejected background start must clear loading');
  assert.equal(pipeline.running, null);

  const html = fs.readFileSync(path.join(web, 'templates', 'index.html'), 'utf8');
  assert.match(html, /File status breakdown/);
  assert.match(html, /Share of indexed files/);
  assert.doesNotMatch(html, /Pipeline Progress/);
  console.log('Dashboard separates live processing, review queue, and file states');
})().catch(error => { console.error(error); process.exitCode = 1; });
