// Run with: node tests/ui_analysis_coverage_state.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const listeners = new Map();
const stores = new Map();
const components = new Map();
const pending = new Map();
const Alpine = {
  store(name, value) {
    if (arguments.length === 2) stores.set(name, value);
    return stores.get(name);
  },
  data(name, factory) { components.set(name, factory); },
};
const api = {
  get(url) {
    if (url === '/info') return Promise.resolve({ version: 'test' });
    if (url.startsWith('/pipeline/runs/latest')) return Promise.resolve({ plan: null });
    if (url.startsWith('/pipeline/analysis/coverage')) {
      const sid = new URLSearchParams(url.split('?')[1]).get('session_id');
      return new Promise((resolve, reject) => pending.set(sid, { resolve, reject }));
    }
    throw new Error(`Unexpected request: ${url}`);
  },
};
const context = {
  Alpine, api,
  document: { addEventListener(name, callback) { listeners.set(name, callback); } },
  window: { addEventListener() {} },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  setTimeout, clearTimeout, console,
};
const source = fs.readFileSync(path.join(__dirname, '..', 'donedatahoarder',
  'web', 'static', 'app.js'), 'utf8');
vm.runInNewContext(source, context);
listeners.get('alpine:init')();

(async () => {
  const session = stores.get('session');
  const panel = components.get('pipeline')();
  session.current_session_id = 'A';
  const first = panel.checkRunPlan();
  assert.equal(panel.analysisCoverage, null);
  pending.get('A').resolve({ total_indexed: 1 });
  await first;
  assert.equal(panel.analysisCoverage.total_indexed, 1);

  session.current_session_id = 'B';
  const second = panel.checkRunPlan();
  assert.equal(panel.analysisCoverage, null);
  pending.get('B').resolve({ total_indexed: 2 });
  await second;
  assert.equal(panel.analysisCoverage.total_indexed, 2);

  session.current_session_id = 'A';
  const staleSuccess = panel.loadAnalysisCoverage();
  session.current_session_id = 'B';
  pending.get('A').resolve({ total_indexed: 99 });
  await staleSuccess;
  assert.equal(panel.analysisCoverage.total_indexed, 2);

  session.current_session_id = 'A';
  const staleFailure = panel.loadAnalysisCoverage();
  session.current_session_id = 'B';
  pending.get('A').reject(new Error('old request failed'));
  await staleFailure;
  assert.equal(panel.analysisCoverage.total_indexed, 2);

  session.current_session_id = null;
  await panel.checkRunPlan();
  assert.equal(panel.analysisCoverage, null);
  console.log('Analysis coverage follows the active session');
})().catch(error => { console.error(error); process.exitCode = 1; });
