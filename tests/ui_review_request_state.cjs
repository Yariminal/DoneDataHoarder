// Run with: node tests/ui_review_request_state.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const listeners = new Map(), stores = new Map(), components = new Map(), pending = new Map();
const posts = [];
let confirmCount = 0;
function defer(url) {
  return new Promise((resolve, reject) => {
    const queue = pending.get(url) || [];
    queue.push({ resolve, reject }); pending.set(url, queue);
  });
}
function answer(url, value) {
  const request = pending.get(url)?.shift();
  assert.ok(request, `Missing request for ${url}`);
  request.resolve(value);
}
const Alpine = {
  store(name, value) { if (arguments.length === 2) stores.set(name, value); return stores.get(name); },
  data(name, factory) { components.set(name, factory); },
};
const api = {
  get(url) { return defer(url); },
  post(url, body) { posts.push({ url, body }); return Promise.resolve({ status: 'approved' }); },
};
const web = path.join(__dirname, '..', 'donedatahoarder', 'web');
const context = {
  Alpine, api, document: { activeElement: null, addEventListener(name, fn) { listeners.set(name, fn); },
    querySelector() { return null; } },
  window: { addEventListener() {}, async appConfirm() { confirmCount += 1; return true; } },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  setTimeout() { return 0; }, clearTimeout() {}, console,
};
vm.runInNewContext(fs.readFileSync(path.join(web, 'static', 'app.js'), 'utf8'), context);
listeners.get('alpine:init')();
context.window.appConfirm = async () => { confirmCount += 1; return true; };

(async () => {
  const session = stores.get('session'), app = stores.get('app');
  const files = components.get('fileBrowser')(), review = components.get('proposalReview')();
  files.$nextTick = fn => fn(); review.$nextTick = fn => fn();
  session.current_session_id = 'A';
  let old = review.load();
  const oldUrl = '/proposals?page=1&per_page=50&status=pending&session_id=A';
  session.current_session_id = 'B'; review.onSessionChange();
  assert.equal(review.proposals.length, 0);
  assert.equal(review.total, 0);
  assert.equal(review.statusFilter, 'pending');
  assert.equal(posts.length, 0);
  answer(oldUrl, { items: [{ id: 1, status: 'pending' }], total: 1 });
  await old;
  assert.equal(review.proposals.length, 0, 'old session response must not render');

  let current = review.load();
  const bUrl = '/proposals?page=1&per_page=50&status=pending&session_id=B';
  review.typeFilter = 'move';
  let filtered = review.load();
  answer(bUrl, { items: [{ id: 2, status: 'pending' }], total: 1 });
  await current;
  assert.equal(review.proposals.length, 0, 'old filter response must not render');
  const moveUrl = '/proposals?page=1&per_page=50&status=pending&proposal_type=move&session_id=B';
  answer(moveUrl, { items: [{ id: 3, status: 'pending', proposal_type: 'move' }], total: 1 });
  await filtered;
  assert.equal(review.proposals[0].id, 3);
  await review.approve(2);
  assert.equal(posts.length, 0, 'nonvisible proposal must not be acted on');
  review.search = 'changed';
  await review.reject(3);
  assert.equal(posts.length, 0, 'changed filters must invalidate actions before reload');
  await review.bulkApprove();
  assert.equal(confirmCount, 0, 'unloaded filters must not reach bulk confirmation');
  review.search = '';
  let bulk = review.bulkApprove();
  const bulkUrl = '/proposals?session_id=B&status=pending&min_confidence=0.8&proposal_type=move&per_page=1';
  review.bulkConfidence = 90;
  answer(bulkUrl, { total: 1 });
  await bulk;
  assert.equal(confirmCount, 0, 'changed threshold must not reach bulk confirmation');
  assert.equal(posts.length, 0);
  review.currentResult = 'pending.json';
  let snapshot = review.loadResult();
  answer('/results/load/pending.json?session_id=B&result_type=proposals', {
    session_id: 'B', type: 'proposals', saved_at: 'yesterday',
    data: { items: [{ id: 3, status: 'pending', proposal_type: 'move' }], total: 1 },
  });
  await snapshot;
  assert.equal(review.snapshotMode, true);
  await review.approve(3);
  await review.reject(3);
  assert.equal(posts.length, 0, 'historical pending snapshot must be read-only');
  current = review.load();
  answer(moveUrl, { items: [{ id: 3, status: 'approved', proposal_type: 'move' }], total: 1 });
  await current;
  assert.equal(review.snapshotMode, false);
  await review.approve(3);
  assert.equal(posts.length, 0, 'freshly approved row must not be approved from old snapshot');

  app.reviewQueue = { session_id: 'B', type: 'rename' };
  assert.equal(review.applyReviewQueue(), true);
  assert.equal(review.typeFilter, 'rename');
  assert.equal(review.search, '');
  assert.equal(review.statusFilter, 'pending');
  const renameUrl = '/proposals?page=1&per_page=50&status=pending&proposal_type=rename&session_id=B';
  answer(renameUrl, { items: [], total: 0 });
  await Promise.resolve();
  app.reviewQueue = { session_id: 'A', type: 'move' };
  assert.equal(review.applyReviewQueue(), false, 'stale dashboard link must be discarded');
  assert.equal(app.reviewQueue, null);

  let fileA = files.load();
  const fileAUrl = '/files?page=1&per_page=50&session_id=B';
  session.current_session_id = 'C'; files.onSessionChange();
  answer(fileAUrl, { items: [{ id: 10, path: 'B/file.txt' }], total: 1 });
  await fileA;
  assert.equal(files.files.length, 0, 'old file response must not render');
  let fileC = files.load();
  const fileCUrl = '/files?page=1&per_page=50&session_id=C';
  answer(fileCUrl, { items: [{ id: 11, path: 'C/file.txt' }], total: 1 });
  await fileC;
  files.currentResult = 'files.json';
  snapshot = files.loadResult();
  answer('/results/load/files.json?session_id=C&result_type=files', {
    session_id: 'C', type: 'files', saved_at: 'yesterday',
    data: { items: [{ id: 11, path: 'C/file.txt' }], total: 1 },
  });
  await snapshot;
  await files.viewFile(11);
  assert.equal(files.showModal, false, 'historical files do not open current details');
  fileC = files.load();
  answer(fileCUrl, { items: [{ id: 11, path: 'C/file.txt' }], total: 1 });
  await fileC;
  const duplicates = components.get('duplicates')();
  duplicates.currentResult = 'dupes.json';
  snapshot = duplicates.loadResult();
  answer('/results/load/dupes.json?session_id=C&result_type=duplicates', {
    session_id: 'C', type: 'duplicates', saved_at: 'yesterday',
    data: { items: [{ id: 8, files: [{ id: 11 }] }], total: 1 },
  });
  await snapshot;
  await duplicates.setKeeper(8, 11);
  assert.equal(posts.length, 0, 'historical duplicate group cannot change keeper');
  let detail = files.viewFile(11);
  files.closeModal();
  answer('/files/11', { id: 11, path: 'C/file.txt' });
  await detail;
  assert.equal(files.showModal, false, 'dismissed detail must not reopen late');
  detail = files.viewFile(11);
  const refreshed = files.load();
  answer('/files/11', { id: 11, path: 'C/file.txt' });
  await detail;
  assert.equal(files.showModal, false, 'a list reload must invalidate old detail');
  answer(fileCUrl, { items: [{ id: 11, path: 'C/file.txt' }], total: 1 });
  await refreshed;
  detail = files.viewFile(11);
  session.current_session_id = 'D'; files.onSessionChange();
  answer('/files/11', { id: 11, path: 'C/file.txt' });
  await detail;
  assert.equal(files.selectedFile, null, 'detail must clear on session change');

  review.currentResult = 'racing.json';
  snapshot = review.loadResult();
  session.current_session_id = 'E'; review.onSessionChange(); duplicates.onSessionChange();
  answer('/results/load/racing.json?session_id=D&result_type=proposals', {
    session_id: 'D', type: 'proposals', data: { items: [{ id: 99, status: 'pending' }], total: 1 },
  });
  await snapshot;
  assert.equal(review.snapshotMode, false, 'late saved result from prior session must not render');
  assert.equal(review.proposals.length, 0);
  assert.equal(duplicates.snapshotMode, false);

  const actions = components.get('proposalReview')();
  actions._sessionId = 'E';
  actions._loadedFilters = JSON.stringify([actions.page, actions.perPage, actions.statusFilter,
    actions.typeFilter, actions.search, actions.minConfidence]);
  actions.proposals = [4, 5, 6].map(id => ({ id, status: 'pending', proposal_type: 'rename' }));
  const previousVersion = context.window._dataVersion;
  await actions.approve(4);
  await actions.reject(5);
  actions.startEdit(actions.proposals[2]);
  actions.editValue = 'updated';
  await actions.saveEdit(6);
  assert.equal(context.window._dataVersion, previousVersion + 3,
    'review mutations must invalidate dashboard counts');
  actions.load = async () => {};
  let bulkRun = actions.bulkApprove();
  answer('/proposals?session_id=E&status=pending&min_confidence=0.8&proposal_type=&per_page=1', { total: 1 });
  await bulkRun;
  assert.equal(context.window._dataVersion, previousVersion + 4,
    'bulk review must invalidate dashboard counts');

  api.post = (url, body) => { posts.push({ url, body }); return defer(url); };
  const conflict = components.get('proposalReview')();
  conflict.$nextTick = fn => fn();
  conflict._sessionId = 'E';
  conflict._loadedFilters = JSON.stringify([conflict.page, conflict.perPage,
    conflict.statusFilter, conflict.typeFilter, conflict.search, conflict.minConfidence]);
  conflict.proposals = [{ id: 71, status: 'pending', proposal_type: 'mark_duplicate' }];
  const approval = conflict.approve(71, { expected_keeper_id: 20 });
  const conflictError = Object.assign(
    new Error('Duplicate comparison changed; review this pair again'), { status: 409 });
  pending.get('/proposals/71/approve').shift().reject(conflictError);
  await new Promise(resolve => setImmediate(resolve));
  const refreshUrl = '/proposals?page=1&per_page=50&status=pending&session_id=E';
  assert.equal((pending.get(refreshUrl) || []).length, 1,
    'conflict must request the current review queue');
  assert.equal(conflict.proposals.length, 0, 'stale action disappears during refresh');
  answer(refreshUrl, { items: [{ id: 72, status: 'pending' }], total: 1 });
  await approval;
  assert.deepEqual(conflict.proposals.map(item => item.id), [72]);
  assert.match(app.toasts.at(-1).msg, /Duplicate comparison changed; review this pair again/);

  const late = components.get('proposalReview')();
  late.$nextTick = fn => fn();
  late._sessionId = 'E';
  late._loadedFilters = JSON.stringify([late.page, late.perPage,
    late.statusFilter, late.typeFilter, late.search, late.minConfidence]);
  late.proposals = [{ id: 73, status: 'pending', proposal_type: 'mark_duplicate' }];
  const lateApproval = late.approve(73);
  session.current_session_id = 'F'; late.onSessionChange();
  const toastCount = app.toasts.length;
  pending.get('/proposals/73/approve').shift().reject(conflictError);
  await lateApproval;
  assert.equal(app.toasts.length, toastCount, 'old session conflict must not toast');
  assert.equal((pending.get(refreshUrl) || []).length, 0,
    'old session conflict must not refresh its review queue');
  assert.equal(late.proposals.length, 0);

  console.log('Review and Files discard stale session, filter, and detail responses');
})().catch(error => { console.error(error); process.exitCode = 1; });
