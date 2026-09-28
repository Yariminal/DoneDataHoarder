// Run with: node tests/ui_nonexact_review.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const listeners = new Map();
const stores = new Map();
const components = new Map();
const posts = [];
const shell = { inert: false, setAttribute() { this.inert = true; },
  removeAttribute() { this.inert = false; } };
let focused = 0;
const reviewButton = { focus() { focused += 1; } };
const returnButton = { isConnected: true, focus() { focused += 1; } };
const details = new Map([
  [10, { id: 10, path: 'C:/collection/candidate.png', mime_type: 'image/png',
    size_bytes: 200, ai_description: 'Candidate image' }],
  [20, { id: 20, path: 'C:/collection/keeper.pdf', mime_type: 'application/pdf',
    size_bytes: 300, ai_description: 'Keeper document' }],
]);
const Alpine = {
  store(name, value) {
    if (arguments.length === 2) stores.set(name, value);
    return stores.get(name);
  },
  data(name, factory) { components.set(name, factory); },
};
const api = {
  async get(url) {
    if (url.startsWith('/files/')) return details.get(Number(url.split('/').pop()));
    return { version: 'test' };
  },
  async post(url, body) { posts.push({ url, body }); return { status: 'approved' }; },
};
const context = {
  Alpine, api,
  document: {
    activeElement: returnButton,
    addEventListener(name, callback) { listeners.set(name, callback); },
    querySelector(selector) {
      if (selector === '.app-shell') return shell;
      if (selector === '.duplicate-review-dialog button.btn-outline') return reviewButton;
      return null;
    },
  },
  window: { addEventListener() {} },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  setTimeout() { return 0; }, clearTimeout() {}, console,
};
const web = path.join(__dirname, '..', 'donedatahoarder', 'web');
vm.runInNewContext(fs.readFileSync(path.join(web, 'static', 'app.js'), 'utf8'), context);
listeners.get('alpine:init')();

(async () => {
  const session = stores.get('session');
  session.current_session_id = 'review-session';
  const review = components.get('proposalReview')();
  review.$nextTick = callback => callback();
  const near = {
    id: 91, status: 'pending', proposal_type: 'mark_duplicate', file_id: 10,
    file_path: 'C:/collection/candidate.png', mime_type: 'image/png',
    duplicate_evidence: { group_id: 30, type: 'perceptual', keeper_id: 20,
      keeper_path: 'C:/collection/keeper.pdf', distance_to_keeper: 5 },
  };
  review.proposals = [near];
  await review.requestApproval(near);
  assert.equal(posts.length, 0, 'opening the comparison must not approve');
  assert.equal(review.duplicateReview.duplicate_evidence.keeper_id, 20);
  assert.equal(review.duplicateReviewFiles.keeper.mime_type, 'application/pdf');
  assert.equal(shell.inert, true, 'background must be inert while reviewing');
  review.cancelDuplicateReview();
  assert.equal(posts.length, 0, 'cancelling must leave the candidate pending');
  assert.equal(shell.inert, false);
  assert.ok(focused >= 2, 'focus enters modal and returns to the row');

  await review.requestApproval(near);
  session.current_session_id = 'different-session';
  await review.approveReviewedDuplicate();
  assert.equal(posts.length, 0, 'a changed session must not approve');
  session.current_session_id = 'review-session';

  await review.requestApproval(near);
  await review.approveReviewedDuplicate();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/proposals/91/approve');
  assert.equal(posts[0].body.session_id, 'review-session');
  assert.equal(posts[0].body.expected_duplicate_group_id, 30);
  assert.equal(posts[0].body.expected_keeper_id, 20);
  assert.equal(posts[0].body.expected_keeper_path, 'C:/collection/keeper.pdf');
  assert.equal(review.proposals[0].status, 'approved');

  const unavailable = { ...near, id: 92, status: 'pending',
    duplicate_evidence: { type: 'semantic', keeper_id: null } };
  await review.requestApproval(unavailable);
  assert.equal(review.duplicateReview, null);
  assert.equal(posts.length, 1, 'missing keeper comparison must fail closed');

  const exact = { ...near, id: 93, status: 'pending',
    duplicate_evidence: { type: 'exact', keeper_id: 20,
      keeper_path: 'C:/collection/keeper.png' } };
  review.proposals.push(exact);
  await review.requestApproval(exact);
  assert.equal(posts.length, 2, 'exact-hash approvals retain the direct flow');

  const html = fs.readFileSync(path.join(web, 'templates', 'index.html'), 'utf8');
  assert.match(html, /@click="requestApproval\(p\)"/);
  assert.match(html, /@click="approveReviewedDuplicate\(\)"/);
  assert.match(html, /duplicateReviewFiles\.candidate\.path/);
  assert.match(html, /duplicateReviewFiles\.keeper\.path/);
  assert.match(html, /duplicateReviewFiles\.keeper\.mime_type/);
  assert.match(html, /hasImage\(p\.duplicate_evidence\.keeper_id, p\.duplicate_evidence\.keeper_mime_type\)/);
  assert.match(html, /x-teleport="body"/);
  assert.match(html, /@keydown\.tab="trapDialogTab\(\$event\)"/);
  assert.match(html, /unverified similarity comparison/i);
  assert.match(html, /candidate for trash on commit/i);
  console.log('Nonexact approval requires paired review and an explicit second action');
})().catch(error => { console.error(error); process.exitCode = 1; });
