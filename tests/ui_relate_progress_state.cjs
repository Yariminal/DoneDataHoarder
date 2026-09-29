// Run with: node tests/ui_relate_progress_state.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const listeners = new Map();
const stores = new Map();
const components = new Map();
let stream;
const web = path.join(__dirname, '..', 'donedatahoarder', 'web');
const Alpine = {
  store(name, value) {
    if (arguments.length === 2) stores.set(name, value);
    return stores.get(name);
  },
  data(name, factory) { components.set(name, factory); },
};
const snapshot = { phase: 'grouping', heartbeat: true,
  updated_utc: '2026-01-01T00:00:00Z', heartbeat_utc: '2026-01-01T00:00:02Z',
  directories_done: 2, directory_index: 3, chunk_done: 0, chunk_active: 1,
  chunk_total: 2, total: null, groups: 4, llm_groups: 3, backstop_groups: 1 };
const context = {
  Alpine,
  api: { get: async url => {
    if (url === '/info') return { version: 'test' };
    assert.equal(url, '/pipeline/jobs/active');
    return { job_id: 'job-1', job_type: 'relate', session_id: 'A',
      state: 'running', progress: snapshot };
  } },
  document: { addEventListener(name, callback) { listeners.set(name, callback); } },
  window: { addEventListener() {} },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  EventSource: class {
    constructor() { this.closed = false; stream = this; }
    close() { this.closed = true; }
  },
  setTimeout, clearTimeout, console,
};
vm.runInNewContext(fs.readFileSync(path.join(web, 'static', 'app.js'), 'utf8'), context);
listeners.get('alpine:init')();

(async () => {
  const panel = components.get('pipeline')();
  stores.get('session').current_session_id = 'A';
  await panel.checkActiveJob();
  assert.equal(panel.relateProgress.directories_done, 2);
  assert.equal(panel.relateProgress.groups, 4);
  assert.equal(panel.relateProgress.updated_utc, snapshot.updated_utc);
  assert.equal(panel.relateProgress.chunk_active, 1);
  let completed = null;
  panel._onJobComplete = async (type, data) => { completed = { type, data }; };
  stream.onmessage({ data: JSON.stringify({ phase: 'directory_complete',
    done: 1, directories_done: 1, groups: 1 }) });
  assert.equal(stream.closed, false);
  assert.equal(panel.activeJobId, 'job-1');
  assert.equal(panel.relateProgress.directories_done, 1);
  stream.onmessage({ data: JSON.stringify({ phase: 'directory_complete',
    done: 2, directories_done: 2, groups: 2 }) });
  assert.equal(stream.closed, false);
  assert.equal(completed, null);
  stream.onmessage({ data: JSON.stringify({ done: true, directories_done: 2,
    directories: 2, total: 2, groups: 2 }) });
  assert.equal(stream.closed, true);
  assert.equal(completed.type, 'relate');
  assert.equal(completed.data.directories_done, 2);

  const html = fs.readFileSync(path.join(web, 'templates', 'index.html'), 'utf8');
  const directoryExpr = html.match(/x-text="([^"]*Directories processed:[^"]*)"/)[1];
  const renderDirectories = relateProgress => vm.runInNewContext(directoryExpr,
    { relateProgress });
  assert.equal(renderDirectories(snapshot), 'Directories processed: 2 (total unknown)');
  const complete = { done: true, directories_done: 3, directories: 3,
    groups: 5, total: 3 };
  assert.equal(renderDirectories(complete), 'Directories: 3 / 3');
  console.log('Relation progress retains measured counts through heartbeat and completion');
})().catch(error => { console.error(error); process.exitCode = 1; });
