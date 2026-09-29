// Run with: node tests/ui_photo_review.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const listeners = new Map(), stores = new Map(), components = new Map(), posts = [];
const context = {
  Alpine: {
    store(name, value) { if (arguments.length === 2) stores.set(name, value); return stores.get(name); },
    data(name, factory) { components.set(name, factory); },
  },
  document: { addEventListener(name, callback) { listeners.set(name, callback); }, querySelector() { return null; } },
  window: { addEventListener() {} },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  api: { async get() { return {}; }, async post(url, body) { posts.push({ url, body }); return {}; } },
  setTimeout() { return 0; }, clearTimeout() {}, console,
};
const web = path.join(__dirname, '..', 'donedatahoarder', 'web');
vm.runInNewContext(fs.readFileSync(path.join(web, 'static', 'app.js'), 'utf8'), context);
listeners.get('alpine:init')();
const full = { is_photo: true, status: 'complete', width: 6000, height: 4000, fields: {} };
const rich = { is_photo: true, status: 'complete', width: 3000, height: 2000,
  fields: { capture_time: '2020-06-07T12:30:00', camera_model: '<Camera>', lens: '50 mm' } };
const quality = { status: 'tradeoff', candidate: rich, keeper: full, requires_review: true,
  reasons: ['Larger pixels and richer capture metadata are in different files.'],
  candidate_unique_fields: ['capture_time', 'camera_model', 'lens'], conflicting_fields: [] };
assert.match(context.window.photoMetadataText(full), /No valid capture metadata found/);
assert.match(context.window.photoMetadataText(rich), /3000×2000 px · 6.0 MP/);
assert.match(context.window.photoMetadataText(rich), /camera model: <Camera>/);
for (const status of ['partial', 'unavailable', 'unsupported', 'unknown']) {
  const text = context.window.photoMetadataText({ status, fields: {} });
  assert.match(text, /absence is not established/);
  assert.doesNotMatch(text, /No valid capture metadata found/);
}
const text = context.window.photoQualityText(quality);
assert.match(text, /PHOTO TRADEOFF · Keep both for review/);
assert.match(text, /Only candidate retains lens: 50 mm/);
assert.match(text, /24.0 MP/);
assert.match(text, /dimensions alone do not establish image quality/);
const conflicting = { ...quality, keeper: { ...full, fields: { capture_time: '2021-01-01T00:00:00' } }, conflicting_fields: ['capture_time'] };
assert.match(context.window.photoQualityText(conflicting), /candidate 2020-06-07T12:30:00; keeper 2021-01-01T00:00:00/);
const review = components.get('proposalReview')();
const combined = review.duplicateEvidenceText({ type: 'exact', exact_bytes: true, photo_quality: quality });
assert.match(combined, /SHA-256 hashes match/);
assert.match(combined, /PHOTO TRADEOFF/);
const duplicates = components.get('duplicates')();
assert.match(duplicates.evidenceText({ dupe_type: 'perceptual' }, { distance_to_keeper: 4, photo_quality: quality }), /PHOTO TRADEOFF/);
const template = fs.readFileSync(path.join(web, 'templates', 'index.html'), 'utf8');
assert.match(template, /x-text="window.photoMetadataText\(selectedFile.photo_metadata\)"/);
assert.match(template, /x-text="window.photoMetadataText\(duplicateReviewFiles.candidate.photo_metadata\)"/);
assert.doesNotMatch(template, /x-html="[^"]*photo(Metadata|Quality)/);
(async () => {
  stores.get('session').current_session_id = 'photos';
  review._sessionId = 'photos';
  review.proposals = [{ id: 5, status: 'pending', review_token: 'displayed-photo-evidence-token' }];
  review._loadedFilters = JSON.stringify([review.page, review.perPage, review.statusFilter, review.typeFilter, review.search, review.minConfidence]);
  await review.approve(5);
  assert.equal(posts[0].body.expected_review_token, 'displayed-photo-evidence-token');
  duplicates._sessionId = 'photos';
  duplicates.groups = [{ id: 9, keep_file_id: 1, files: [{ id: 1 }, { id: 2 }] }];
  duplicates.load = async () => {};
  await duplicates.setKeeper(9, 2);
  assert.equal(posts[1].body.expected_keeper_id, 1);
  assert.equal(posts[1].body.keep_file_id, 2);
  console.log('Photo evidence preserves metadata tradeoffs, unknowns, text safety, and displayed review tokens');
})().catch(error => { console.error(error); process.exitCode = 1; });
