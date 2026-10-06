// Offline smoke of the actual Desktop render functions. No HTTP/model calls.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync('desktop/static/app.js', 'utf8');
function section(start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert(from >= 0 && to > from);
  return source.slice(from, to);
}
const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, {textContent: '', innerHTML: '', className: ''});
  return nodes.get(id);
};
const event = {notification_id: 'nt_' + 'a'.repeat(32),
  title: 'SIRA needs owner access: web search', body: 'Review access settings.',
  category: 'owner_access_required', status: 'pending', urgency: 'normal',
  source_kind: 'access_request', source_request_id: 'ar_' + 'b'.repeat(32),
  created_at: '2026-09-28T00:00:00Z'};
const notificationData = {total: 1, events: [event], latest: event};
const context = {$, overview: null, researchStatus: null,
  api: async path => {assert.equal(path, '/api/notifications'); return notificationData;},
  escapeText: value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;'),
  humanTime: value => value || '—',
  setPill: (el, value) => {el.textContent = value;},
  detailRows: rows => rows.map(([key, value]) => `${key}: ${value}`).join('; '),
  renderTimeline: () => {}, renderResearch: () => {}, renderHealth: () => {}, renderProviders: () => {},
  renderResearchStatus: () => {},
  renderPromotions: () => {}, runtimeIsOn: () => false};
vm.createContext(context);
vm.runInContext(section('function renderNotificationPreview(notifications)', '\nfunction renderChatStatus(data)'), context);
vm.runInContext(section('async function refreshNotifications()', '\nfunction renderNotificationPreview(notifications)'), context);
vm.runInContext(section('function renderMemory(data)', '\nfunction renderHealth(data)'), context);
vm.runInContext(section('function renderCore(core)', '\nfunction renderMemory(data)'), context);
vm.runInContext(section('function renderOverview(data)', '\nfunction renderCore(core)'), context);
vm.runInContext(section('async function refreshResearchStatus()', '\nfunction stopResearchPolling()'), context);
const core = {runtime: {generation: 69, effective_state: 'running', state_health: 'ok'},
  workers: {}, learning: {goals: []}, knowledge: {verified_count: 4},
  providers: {}, gaps: [], capabilities: [{name: 'research', state: 'partially_demonstrated',
    reason: 'verified_claim_evidence_not_general_research_mastery', evidence_refs: ['claim.tree']}],
  pending_work: []};
const snapshot = {runtime: core.runtime, core, release: {}, memory: {healthy: true,
  counts: {memories: 40, occurrences: 42, transitions: 2}},
  notifications: {total: 1, latest: event}, git: {}, activity: []};
context.renderMemory(snapshot);
context.renderOverview(snapshot);
assert.equal($('notification-count').textContent, 1);
assert($('dashboard-notification').innerHTML.includes('Review access settings.'));
assert($('memory-detail').innerHTML.includes('Persistent Memory (experience/context): 40'));
assert($('memory-detail').innerHTML.includes('Verified Knowledge (independently checked claims): 4'));
assert($('core-research-capability').textContent.includes('partially_demonstrated'));
assert($('core-research-capability').textContent.includes('broader research capability is not demonstrated'));
(async () => {
  await context.refreshNotifications();
  assert.equal($('notification-count').textContent, 1);
  assert($('notification-list').innerHTML.includes('Review access settings.'));
  assert($('notification-list').innerHTML.includes('owner_access_required'));
  context.overview.core.capabilities[0] = {name: 'research', state: 'unverified',
    reason: 'no_current_two_host_verified_research_claim'};
  context.api = async path => {assert.equal(path, '/api/research/status'); return {
    jobs: [{job_id: 'r1', verification: {status: 'verified_multi_evidence'}}]};};
  await context.refreshResearchStatus();
  assert($('core-research-capability').textContent.includes('Verified research-job evidence exists'));
  assert(!$('core-research-capability').textContent.includes('demonstrated'));
  notificationData.total = 0;
  notificationData.events = [];
  notificationData.latest = null;
  context.api = async path => {assert.equal(path, '/api/notifications'); return notificationData;};
  await context.refreshNotifications();
  assert.equal($('notification-count').textContent, 0);
  assert($('notification-list').innerHTML.includes('No owner notifications recorded.'));
  assert.equal($('dashboard-notification').innerHTML, 'No owner notifications recorded.');
  console.log(JSON.stringify({notification_count: 1, detail_visible: true,
    empty_state: true, persistent_memory: 40, verified_knowledge: 4,
    research_capability: 'partially_demonstrated', api_requests: 0,
    model_requests: 0}));
})().catch(error => {console.error(error); process.exitCode = 1;});
