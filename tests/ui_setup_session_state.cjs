// Run with: node tests/ui_setup_session_state.cjs
// Exercises the real app.js Alpine store and Setup component without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const listeners = new Map();
const windowListeners = new Map();
const saved = new Map([
  ['datahoarder_analyze_model', 'gemma3:12b'],
  ['datahoarder_propose_model', 'gemma3:12b'],
  ['datahoarder_backend', 'old-backend'],
  ['datahoarder_workers', '4'],
]);
const stores = new Map();
const components = new Map();
const Alpine = {
  store(name, value) {
    if (arguments.length === 2) stores.set(name, value);
    return stores.get(name);
  },
  data(name, factory) { components.set(name, factory); },
};
const context = {
  Alpine,
  api: { get: async () => ({ version: 'test' }) },
  document: { addEventListener(name, callback) { listeners.set(name, callback); } },
  window: {
    addEventListener(name, callback) { windowListeners.set(name, callback); },
    dispatchEvent(event) { windowListeners.get(event.type)?.(event); },
  },
  localStorage: {
    getItem(name) { return saved.get(name) ?? null; },
    setItem(name, value) { saved.set(name, String(value)); },
    removeItem(name) { saved.delete(name); },
  },
  CustomEvent: class { constructor(type) { this.type = type; } },
  setTimeout,
  clearTimeout,
  console,
};
const source = fs.readFileSync(path.join(__dirname, '..', 'donedatahoarder',
  'web', 'static', 'app.js'), 'utf8');
vm.runInNewContext(source, context);
listeners.get('alpine:init')();

(async () => {
  const setup = components.get('setup')();
  setup.loadOllamaStatus = async () => {};
  setup.loadInstalledModels = async () => {};
  setup.loadDbPath = async () => {};
  setup.$watch = () => {};
  assert.equal(setup.selectedAnalyzeModel, 'gemma3:12b');
  await setup.init();
  stores.get('session').loadFrom({
    id: 'synthetic-review', root_path: 'C:/Synthetic Collection',
    backend: 'ollama', model: 'gemma4:26b',
    analyze_model: 'gemma4:26b', propose_model: 'gemma4:26b',
    workers: 1, preferred_language: 'leave_as_is', relate_scope: 'per_directory',
  });
  assert.equal(setup.selectedAnalyzeModel, 'gemma4:26b');
  assert.equal(setup.selectedProposeModel, 'gemma4:26b');
  assert.equal(setup.selectedBackend, 'ollama');
  assert.equal(setup.selectedWorkers, 1);
  assert.equal(setup.selectedFolder, 'C:/Synthetic Collection');
  console.log('Setup reflects loaded session immediately');
})().catch(error => { console.error(error); process.exitCode = 1; });
