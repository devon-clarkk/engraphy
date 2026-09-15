#!/usr/bin/env node
// Headless verification of every pure module in the desktop app: MCP result
// parsing, argument building, the card view-models, the stats model, and the
// desktop-owned connection / validation / explorer models. Nothing here touches
// electron, the MCP SDK, or the DOM, so it runs in plain Node.
//
// Run: npm test  (compiles the module list in package.json to out-test first).
//
// Two halves:
//   1. The frozen copied client (src/main/client/*). These checks are the
//      extension's own suite, carried over verbatim, and act as the re-sync
//      guard: if a future copy from the extension's webview build changes
//      behaviour, these fail here first.
//   2. The desktop-owned modules (connection / validation / explorerModel /
//      ipcResult), which exist because the desktop app needs states the
//      extension never modelled. See DECISIONS.md §12.
//
// IMPORTANT: a new pure module is only covered once it is added to the explicit
// tsc file list in package.json's `build:test` script. Output goes to out-test/,
// never out/, because electron-builder ships out/**/*.

'use strict';

const assert = require('assert');
const t = require('../out-test/client/toolResult.js');
const w = require('../out-test/client/webviewMessages.js');
const s = require('../out-test/client/statsModel.js');
const c = require('../out-test/connection.js');
const v = require('../out-test/validation.js');
const x = require('../out-test/explorerModel.js');
const r = require('../out-test/ipcResult.js');
const win = require('../out-test/windowState.js');
const m = require('../out-test/messages.js');
const gm = require('../out-test/graphModel.js');
const gh = require('../out-test/graphHarvest.js');
const vc = require('../out-test/versionCheck.js');
const um = require('../out-test/updateModel.js');
const bs = require('../out-test/bootstrap.js');

let passed = 0;
let failed = 0;
function check(name, fn) {
	try {
		fn();
		passed++;
		console.log(`  ok - ${name}`);
	} catch (e) {
		failed++;
		console.log(`  FAIL - ${name}`);
		console.log(String((e && e.stack) || e).split('\n').slice(0, 6).map((l) => '      ' + l).join('\n'));
	}
}
/**
 * Async cases are QUEUED, not run inline. `check` calls its fn synchronously, so
 * an async fn handed to it would return a pending promise, the try/catch would
 * see nothing, and a failing assertion would surface as an unhandled rejection
 * AFTER the summary had already printed "0 failed".
 */
const asyncChecks = [];
function checkAsync(name, fn) {
	asyncChecks.push([name, fn]);
}
async function runAsyncChecks() {
	for (const [name, fn] of asyncChecks) {
		try {
			await fn();
			passed++;
			console.log(`  ok - ${name}`);
		} catch (e) {
			failed++;
			console.log(`  FAIL - ${name}`);
			console.log(String((e && e.stack) || e).split('\n').slice(0, 6).map((l) => '      ' + l).join('\n'));
		}
	}
}

function group(name) {
	console.log(`\n# ${name}`);
}

group('copied client (frozen: re-sync guard)');

// ---- parseToolResult ----
check('prefers structuredContent', () => {
	const out = t.parseToolResult({ structuredContent: { v: 1, nodes: [] }, content: [{ type: 'text', text: '{}' }] });
	assert.deepStrictEqual(out, { v: 1, nodes: [] });
});
check('falls back to JSON text when no structuredContent', () => {
	const out = t.parseToolResult({ content: [{ type: 'text', text: '{"v":1,"results":[]}' }] });
	assert.deepStrictEqual(out, { v: 1, results: [] });
});
check('empty structuredContent falls through to text', () => {
	const out = t.parseToolResult({ structuredContent: {}, content: [{ type: 'text', text: '{"a":1}' }] });
	assert.deepStrictEqual(out, { a: 1 });
});
check('non-JSON text returns { text }', () => {
	const out = t.parseToolResult({ content: [{ type: 'text', text: 'hello' }] });
	assert.deepStrictEqual(out, { text: 'hello' });
});
check('isError with structuredContent null → throws with parsed code', () => {
	assert.throws(
		() => t.parseToolResult({ isError: true, structuredContent: null, content: [{ type: 'text', text: 'ENGRAPHY_VALIDATION: bad scope' }] }),
		(e) => e instanceof t.EngraphyToolError && e.code === 'ENGRAPHY_VALIDATION'
	);
});
check('isError without ENGRAPHY_ code → ENGRAPHY_ERROR', () => {
	assert.throws(
		() => t.parseToolResult({ isError: true, content: [{ type: 'text', text: 'kaboom' }] }),
		(e) => e instanceof t.EngraphyToolError && e.code === 'ENGRAPHY_ERROR' && /kaboom/.test(e.message)
	);
});

// ---- argument builders ----
check('search drops undefined optionals', () => {
	assert.deepStrictEqual(t.buildSearchArgs({ scope: 'all', query: 'x' }), { scope: 'all', query: 'x' });
	assert.deepStrictEqual(
		t.buildSearchArgs({ scope: 's', query: 'q', limit: 10, includeInactive: true, detail: 'summary' }),
		{ scope: 's', query: 'q', limit: 10, include_inactive: true, detail: 'summary' }
	);
});
check('traverse always sends direction', () => {
	const a = t.buildTraverseArgs({ startId: 'n1', direction: 'both' });
	assert.strictEqual(a.start_id, 'n1');
	assert.strictEqual(a.direction, 'both');
});
check('get clamps to 25 ids', () => {
	const ids = Array.from({ length: 40 }, (_, i) => `id${i}`);
	assert.strictEqual(t.buildGetArgs(ids).ids.length, 25);
	assert.strictEqual(t.chunkIds(ids).length, 2);
	assert.strictEqual(t.chunkIds(ids)[1].length, 15);
});
check('inbox builders', () => {
	assert.deepStrictEqual(t.buildInboxListArgs(50), { action: 'list', limit: 50 });
	assert.deepStrictEqual(t.buildInboxDiscardArgs('i1'), { action: 'discard', id: 'i1' });
	assert.deepStrictEqual(
		t.buildInboxPromoteArgs({ id: 'i1', type: 'note', scope: 's', title: 'T', body: 'B' }),
		{ action: 'promote', id: 'i1', type: 'note', scope: 's', title: 'T', body: 'B' }
	);
});
check('resolve_duplicate: merge_into only for merge', () => {
	assert.deepStrictEqual(t.buildResolveDuplicateArgs('p1', 'distinct', 'n9'), {
		pending_id: 'p1',
		resolution: 'distinct',
	});
	assert.deepStrictEqual(t.buildResolveDuplicateArgs('p1', 'merge', 'n9'), {
		pending_id: 'p1',
		resolution: 'merge',
		merge_into: 'n9',
	});
	// merge with no target → merge_into omitted (server will validate)
	assert.deepStrictEqual(t.buildResolveDuplicateArgs('p1', 'merge'), {
		pending_id: 'p1',
		resolution: 'merge',
	});
});

// ---- pending_list parse + expiry (new tool) ----
check('buildPendingListArgs drops undefined', () => {
	assert.deepStrictEqual(t.buildPendingListArgs(50), { limit: 50 });
	assert.deepStrictEqual(t.buildPendingListArgs(), {});
});
check('pendingItemsFrom parses {v,pending:[...]} with STRING payload_preview', () => {
	const server = {
		v: 1,
		pending: [
			{
				id: 'p-1',
				payload_preview: 'My title — a body snippet capped at 280 chars …',
				candidates: [{ id: 'c-1', title: 'Existing node', similarity: 0.96 }],
				expires_at: '2026-08-02T00:00:00Z',
				created_at: '2026-08-01T00:00:00Z',
			},
		],
	};
	const items = t.pendingItemsFrom(server);
	assert.strictEqual(items.length, 1);
	const it = items[0];
	assert.strictEqual(typeof it.payload_preview, 'string');
	assert.strictEqual(it.payload_preview, 'My title — a body snippet capped at 280 chars …');
	assert.strictEqual(it.candidates[0].title, 'Existing node');
	assert.strictEqual(it.candidates[0].similarity, 0.96);
	assert.strictEqual(it.candidates[0].id, 'c-1');
	assert.strictEqual(it.expires_at, '2026-08-02T00:00:00Z');
	assert.strictEqual(it.created_at, '2026-08-01T00:00:00Z');
});
check('pendingItemsFrom tolerates missing pending / candidates', () => {
	assert.deepStrictEqual(t.pendingItemsFrom({ v: 1 }), []);
	const it = t.pendingItemsFrom({ pending: [{ id: 'x', payload_preview: 's' }] })[0];
	assert.deepStrictEqual(it.candidates, []);
	assert.strictEqual(it.expires_at, null);
});
check('isExpired: past expired, future not, null not', () => {
	const now = Date.parse('2026-08-01T12:00:00Z');
	assert.strictEqual(t.isExpired({ expires_at: '2026-08-01T11:59:00Z' }, now), true);
	assert.strictEqual(t.isExpired({ expires_at: '2026-08-01T12:01:00Z' }, now), false);
	assert.strictEqual(t.isExpired({ expires_at: null }, now), false);
});

// ---- webview card view-models (host builds these) ----
check('buildPendingCard maps preview, candidates (pct), expiry', () => {
	const now = Date.parse('2026-08-02T12:00:00Z');
	const vm = w.buildPendingCard(
		{
			id: 'p-1',
			payload_preview: 'A captured fact',
			candidates: [
				{ id: 'c-1', title: 'Existing node', similarity: 0.964 },
				{ id: 'c-2', title: 'Other', similarity: 0.5 },
			],
			expires_at: '2026-08-02T11:00:00Z', // past `now` → expired
			created_at: '2026-08-01T00:00:00Z',
		},
		now
	);
	assert.strictEqual(vm.id, 'p-1');
	assert.strictEqual(vm.preview, 'A captured fact');
	assert.strictEqual(vm.expired, true);
	assert.strictEqual(vm.candidates.length, 2);
	assert.deepStrictEqual(vm.candidates[0], { id: 'c-1', title: 'Existing node', similarityPct: 96 });
	assert.strictEqual(vm.candidates[1].similarityPct, 50);
});
check('buildPendingCard: empty preview → placeholder; future expiry not expired', () => {
	const now = Date.parse('2026-08-02T12:00:00Z');
	const vm = w.buildPendingCard(
		{ id: 'p', payload_preview: '', candidates: [], expires_at: '2026-08-03T00:00:00Z', created_at: null },
		now
	);
	assert.strictEqual(vm.preview, '(pending write)');
	assert.strictEqual(vm.expired, false);
	assert.deepStrictEqual(vm.candidates, []);
});
check('buildInboxCard: preview from object title, payload pretty-printed', () => {
	const vm = w.buildInboxCard({
		id: 'i-1',
		kind: 'note',
		scope: null,
		payload: { title: 'Captured title', extra: 1 },
		created_at: '2026-08-01T00:00:00Z',
	});
	assert.strictEqual(vm.id, 'i-1');
	assert.strictEqual(vm.preview, 'Captured title');
	assert.strictEqual(vm.kind, 'note');
	assert.strictEqual(vm.scope, '(space)'); // null scope → label
	assert.ok(vm.payloadJson.includes('"Captured title"'));
	assert.ok(vm.payloadJson.includes('\n')); // pretty-printed (indented)
});
check('inboxPreview: string payload, then fallback to kind', () => {
	assert.strictEqual(w.inboxPreview({ kind: 'k', payload: 'hello world' }), 'hello world');
	assert.strictEqual(w.inboxPreview({ kind: 'blob', payload: { nope: 1 } }), 'blob item');
});
check('themeKindFromEnum maps all four ColorThemeKind values', () => {
	assert.strictEqual(w.themeKindFromEnum(1), 'light');
	assert.strictEqual(w.themeKindFromEnum(2), 'dark');
	assert.strictEqual(w.themeKindFromEnum(3), 'high-contrast');
	assert.strictEqual(w.themeKindFromEnum(4), 'high-contrast-light');
	assert.strictEqual(w.themeKindFromEnum(999), 'light'); // unknown → safe default
});

// ---- webview→host message parsing (the trust boundary) ----
check('parseWebviewMessage: accepts ready/refresh', () => {
	assert.deepStrictEqual(w.parseWebviewMessage({ type: 'ready' }), { type: 'ready' });
	assert.deepStrictEqual(w.parseWebviewMessage({ type: 'refresh' }), { type: 'refresh' });
});
check('parseWebviewMessage: approve/promote/discard require their id', () => {
	assert.deepStrictEqual(w.parseWebviewMessage({ type: 'approve', pendingId: 'p1' }), {
		type: 'approve',
		pendingId: 'p1',
	});
	assert.deepStrictEqual(w.parseWebviewMessage({ type: 'promote', inboxId: 'i1' }), {
		type: 'promote',
		inboxId: 'i1',
	});
	assert.deepStrictEqual(w.parseWebviewMessage({ type: 'discard', inboxId: 'i1' }), {
		type: 'discard',
		inboxId: 'i1',
	});
});
check('parseWebviewMessage: merge requires BOTH pendingId and mergeInto', () => {
	assert.deepStrictEqual(w.parseWebviewMessage({ type: 'merge', pendingId: 'p1', mergeInto: 'n9' }), {
		type: 'merge',
		pendingId: 'p1',
		mergeInto: 'n9',
	});
	assert.strictEqual(w.parseWebviewMessage({ type: 'merge', pendingId: 'p1' }), null);
	assert.strictEqual(w.parseWebviewMessage({ type: 'merge', mergeInto: 'n9' }), null);
});
check('parseWebviewMessage: rejects unknown type / missing id / non-object', () => {
	assert.strictEqual(w.parseWebviewMessage({ type: 'nope' }), null);
	assert.strictEqual(w.parseWebviewMessage({ type: 'approve' }), null); // missing pendingId
	assert.strictEqual(w.parseWebviewMessage({ type: 'approve', pendingId: '' }), null); // blank
	assert.strictEqual(w.parseWebviewMessage({ type: 'approve', pendingId: 42 }), null); // wrong type
	assert.strictEqual(w.parseWebviewMessage(null), null);
	assert.strictEqual(w.parseWebviewMessage('approve'), null);
	assert.strictEqual(w.parseWebviewMessage(undefined), null);
});

// ---- normalizeServerUrl (307-redirect latency fix) ----
check('normalizeServerUrl adds a trailing slash to the path', () => {
	assert.strictEqual(t.normalizeServerUrl('http://127.0.0.1:8000/mcp'), 'http://127.0.0.1:8000/mcp/');
});
check('normalizeServerUrl is idempotent when already slashed', () => {
	assert.strictEqual(t.normalizeServerUrl('http://127.0.0.1:8000/mcp/'), 'http://127.0.0.1:8000/mcp/');
});
check('normalizeServerUrl preserves query after the slash', () => {
	assert.strictEqual(
		t.normalizeServerUrl('http://127.0.0.1:8000/mcp?x=1'),
		'http://127.0.0.1:8000/mcp/?x=1'
	);
});
check('normalizeServerUrl: empty stays empty (guard still fires)', () => {
	assert.strictEqual(t.normalizeServerUrl(''), '');
	assert.strictEqual(t.normalizeServerUrl('   '), '');
});
check('normalizeServerUrl: non-URL returned as-is (no throw)', () => {
	assert.strictEqual(t.normalizeServerUrl('not a url'), 'not a url');
});
check('normalizeServerUrl: origin-only already ends in slash', () => {
	assert.strictEqual(t.normalizeServerUrl('http://127.0.0.1:8000'), 'http://127.0.0.1:8000/');
});

// ---- promote: static node-type list + payload prefill ----
check('STARTER_NODE_TYPES are exactly the starter pack types', () => {
	assert.deepStrictEqual(
		w.STARTER_NODE_TYPES.map((o) => o.type),
		['note', 'person', 'preference', 'commitment', 'project_ref']
	);
	assert.ok(w.STARTER_NODE_TYPES.every((o) => typeof o.description === 'string' && o.description.length));
});
check('promoteDefaults: object title + body precedence text>message>summary>body', () => {
	assert.deepStrictEqual(w.promoteDefaults({ title: 'T', text: 'X', message: 'Y', summary: 'Z', body: 'B' }), {
		title: 'T',
		body: 'X',
	});
	assert.deepStrictEqual(w.promoteDefaults({ message: 'Y', summary: 'Z', body: 'B' }), { title: '', body: 'Y' });
	assert.deepStrictEqual(w.promoteDefaults({ summary: 'Z', body: 'B' }), { title: '', body: 'Z' });
	assert.deepStrictEqual(w.promoteDefaults({ body: 'B' }), { title: '', body: 'B' });
});
check('promoteDefaults: string payload seeds body only; non-object → empty', () => {
	assert.deepStrictEqual(w.promoteDefaults('just a captured string'), { title: '', body: 'just a captured string' });
	assert.deepStrictEqual(w.promoteDefaults(null), { title: '', body: '' });
	assert.deepStrictEqual(w.promoteDefaults(42), { title: '', body: '' });
	assert.deepStrictEqual(w.promoteDefaults({ nope: 1 }), { title: '', body: '' });
});

// ---- no-server onboarding decision + URL validation ----
check('isConnectionError: connection/auth signatures true, tool errors false', () => {
	assert.ok(w.isConnectionError('fetch failed'));
	assert.ok(w.isConnectionError('connect ECONNREFUSED 127.0.0.1:8000'));
	assert.ok(w.isConnectionError('getaddrinfo ENOTFOUND host'));
	assert.ok(w.isConnectionError('HTTP 401 Unauthorized'));
	assert.ok(w.isConnectionError('403 Forbidden'));
	assert.ok(w.isConnectionError('engraphy.serverUrl is not set.'));
	assert.strictEqual(w.isConnectionError('ENGRAPHY_VALIDATION: bad scope'), false);
	assert.strictEqual(w.isConnectionError('ENGRAPHY_PENDING_EXPIRED'), false);
	assert.strictEqual(w.isConnectionError(''), false);
});
check('computeConnectionState: unconfigured → no-server(unconfigured)', () => {
	assert.deepStrictEqual(
		w.computeConnectionState({ serverConfigured: false, pendingError: null, inboxError: null }),
		{ kind: 'no-server', reason: 'unconfigured' }
	);
});
check('computeConnectionState: both bands connection-failed → no-server(unreachable) with detail', () => {
	const s = w.computeConnectionState({
		serverConfigured: true,
		pendingError: 'fetch failed',
		inboxError: 'connect ECONNREFUSED 127.0.0.1:8000',
	});
	assert.strictEqual(s.kind, 'no-server');
	assert.strictEqual(s.reason, 'unreachable');
	assert.strictEqual(s.detail, 'fetch failed');
});
check('computeConnectionState: one band ok or a tool error → ok (render bands)', () => {
	// only one band errored
	assert.deepStrictEqual(
		w.computeConnectionState({ serverConfigured: true, pendingError: 'fetch failed', inboxError: null }),
		{ kind: 'ok' }
	);
	// both errored but not connection-like (real tool errors)
	assert.deepStrictEqual(
		w.computeConnectionState({
			serverConfigured: true,
			pendingError: 'ENGRAPHY_VALIDATION: x',
			inboxError: 'ENGRAPHY_VALIDATION: y',
		}),
		{ kind: 'ok' }
	);
	// clean
	assert.deepStrictEqual(
		w.computeConnectionState({ serverConfigured: true, pendingError: null, inboxError: null }),
		{ kind: 'ok' }
	);
});
check('isValidServerUrl: http(s) only, rejects junk/empty/other schemes', () => {
	assert.ok(w.isValidServerUrl('http://127.0.0.1:8000/mcp/'));
	assert.ok(w.isValidServerUrl('https://host.example/mcp/'));
	assert.strictEqual(w.isValidServerUrl('127.0.0.1:8000'), false);
	assert.strictEqual(w.isValidServerUrl('ftp://host/x'), false);
	assert.strictEqual(w.isValidServerUrl('not a url'), false);
	assert.strictEqual(w.isValidServerUrl(''), false);
	assert.strictEqual(w.isValidServerUrl('   '), false);
});

// ---- stats model (value dashboard) ----
check('answerRate: answered ÷ asked as whole %, guards 0 asked', () => {
	assert.strictEqual(s.answerRate(146, 200), 73); // demo space
	assert.strictEqual(s.answerRate(102, 138), 74); // demo user (rounds 73.9)
	assert.strictEqual(s.answerRate(0, 0), 0);
	assert.strictEqual(s.answerRate(5, 0), 0);
});
check('normalizeSeries: scales to max; all-zero and empty → zeros', () => {
	assert.deepStrictEqual(s.normalizeSeries([1, 2, 4]), [0.25, 0.5, 1]);
	assert.deepStrictEqual(s.normalizeSeries([0, 0, 0]), [0, 0, 0]);
	assert.deepStrictEqual(s.normalizeSeries([]), []);
	assert.deepStrictEqual(s.normalizeSeries([5]), [1]);
});
check('scopeLabels: space vs user branch off group_by/principal', () => {
	const sp = s.scopeLabels('space', null, 'demo');
	assert.strictEqual(sp.label, 'Whole space');
	assert.ok(/demo/.test(sp.help));
	const us = s.scopeLabels('user', 'devon', 'demo');
	assert.strictEqual(us.label, 'You');
	assert.ok(/devon/.test(us.help));
	// user with null principal still says "You" without an empty paren
	const un = s.scopeLabels('user', null, 'demo');
	assert.strictEqual(un.label, 'You');
	assert.ok(!/\(\)/.test(un.help));
});
check('parseStatsResult: coerces numbers, defaults, group_by/principal fallback', () => {
	const r = s.parseStatsResult({
		space: 'demo',
		group_by: 'bogus',
		range_days: '30',
		totals: { questions_asked: '200', answered: 146 },
		series: [{ date: '2026-08-03', memory_reused: '36' }],
	});
	assert.strictEqual(r.group_by, 'space'); // unknown → space
	assert.strictEqual(r.principal, null);
	assert.strictEqual(r.range_days, 30);
	assert.strictEqual(r.totals.questions_asked, 200);
	assert.strictEqual(r.totals.duplicates_prevented, 0); // missing → 0
	assert.strictEqual(r.series[0].memory_reused, 36);
	assert.deepStrictEqual(s.parseStatsResult(null).series, []); // never throws
});
check('buildStatsView: tiles, hero flags, answer-rate sub, proxy note, spark', () => {
	const result = {
		v: 1,
		space: 'demo',
		group_by: 'space',
		principal: null,
		range_days: 3,
		generated_at: 'now',
		totals: {
			questions_asked: 200,
			answered: 146,
			memory_reused: 262,
			facts_stored: 73,
			duplicates_prevented: 38,
			promotes: 8,
		},
		series: [
			{ date: 'd1', questions_asked: 0, answered: 0, memory_reused: 0, facts_stored: 0, duplicates_prevented: 0, promotes: 0 },
			{ date: 'd2', questions_asked: 100, answered: 73, memory_reused: 131, facts_stored: 36, duplicates_prevented: 19, promotes: 4 },
			{ date: 'd3', questions_asked: 100, answered: 73, memory_reused: 131, facts_stored: 37, duplicates_prevented: 19, promotes: 4 },
		],
	};
	const v = s.buildStatsView(result);
	assert.strictEqual(v.scopeLabel, 'Whole space');
	assert.strictEqual(v.answerRate, 73);
	// first two tiles are the hero value story, in order
	assert.deepStrictEqual(v.tiles.slice(0, 2).map((x) => x.key), ['duplicates_prevented', 'memory_reused']);
	assert.ok(v.tiles.slice(0, 2).every((x) => x.hero === true));
	assert.ok(v.tiles.slice(2).every((x) => x.hero === false));
	const dup = v.tiles.find((x) => x.key === 'duplicates_prevented');
	assert.strictEqual(dup.value, 38);
	const mem = v.tiles.find((x) => x.key === 'memory_reused');
	assert.ok(/proxy/i.test(mem.note)); // honest labeling present
	const answered = v.tiles.find((x) => x.key === 'answered');
	assert.strictEqual(answered.sub, '73% answer rate');
	// sparkline normalized against the metric's own max (memory_reused max 131)
	assert.deepStrictEqual(mem.spark, [0, 1, 1]);
});
check('statsConnectionState: connection error → no-server; ok otherwise', () => {
	assert.strictEqual(s.statsConnectionState(true, 'fetch failed').kind, 'no-server');
	assert.strictEqual(s.statsConnectionState(false, null).kind, 'no-server'); // unconfigured
	assert.deepStrictEqual(s.statsConnectionState(true, null), { kind: 'ok' });
	assert.deepStrictEqual(s.statsConnectionState(true, 'ENGRAPHY_VALIDATION: x'), { kind: 'ok' }); // tool error inline
});
check('parseStatsMessage: validates setRange/setGroup, accepts controls, rejects junk', () => {
	assert.deepStrictEqual(s.parseStatsMessage({ type: 'ready' }), { type: 'ready' });
	assert.deepStrictEqual(s.parseStatsMessage({ type: 'setRange', rangeDays: 14 }), { type: 'setRange', rangeDays: 14 });
	assert.deepStrictEqual(s.parseStatsMessage({ type: 'setGroup', groupBy: 'user' }), { type: 'setGroup', groupBy: 'user' });
	assert.deepStrictEqual(s.parseStatsMessage({ type: 'openWalkthrough' }), { type: 'openWalkthrough' });
	assert.strictEqual(s.parseStatsMessage({ type: 'setRange', rangeDays: 0 }), null);
	assert.strictEqual(s.parseStatsMessage({ type: 'setRange', rangeDays: 2.5 }), null);
	assert.strictEqual(s.parseStatsMessage({ type: 'setGroup', groupBy: 'org' }), null);
	assert.strictEqual(s.parseStatsMessage({ type: 'nope' }), null);
	assert.strictEqual(s.parseStatsMessage(null), null);
});

// ===========================================================================
// Desktop-owned modules
// ===========================================================================

group('connection: error classification');

// The exact strings the MCP SDK 1.30.0 produced against the LIVE Engraphy server
// (0.1.0) and against dead endpoints. Captured empirically, not guessed.
const SDK_401 = Object.assign(new Error('Streamable HTTP error: Error POSTing to endpoint: unauthorized'), {
	code: 401,
});
function fetchFailed(causeCode, causeMsg) {
	return Object.assign(new TypeError('fetch failed'), {
		cause: Object.assign(new Error(causeMsg), { code: causeCode }),
	});
}

check('describeError: the live-server 401 is auth, NOT transport', () => {
	// This is the whole reason the module exists. The SDK's 401 message carries
	// no digits and the copied isConnectionError claims it as a connection
	// failure, which told a user with a healthy server to go start a server.
	assert.strictEqual(w.isConnectionError(SDK_401.message), true, 'precondition: copied predicate claims it');
	const d = c.describeError(SDK_401, '127.0.0.1:8000');
	assert.strictEqual(d.class, 'auth');
	assert.strictEqual(d.code, '401');
	assert.ok(/rejected this token/.test(d.summary));
	assert.ok(/127\.0\.0\.1:8000/.test(d.summary));
});
check('describeError: a 401 with no numeric code still classifies from the word', () => {
	const d = c.describeError(new Error('Error POSTing to endpoint: unauthorized'), 'h');
	assert.strictEqual(d.class, 'auth');
});
check('describeError: 403 / ENGRAPHY_AUTH / ENGRAPHY_ROLE are auth', () => {
	assert.strictEqual(c.describeError(Object.assign(new Error('nope'), { code: 403 })).class, 'auth');
	assert.strictEqual(c.describeError(new Error('ENGRAPHY_AUTH: bad token')).class, 'auth');
	assert.strictEqual(c.describeError(new Error('ENGRAPHY_ROLE: readonly')).class, 'auth');
});
check('describeError: ECONNREFUSED is transport, with the host in the copy', () => {
	const d = c.describeError(fetchFailed('ECONNREFUSED', 'connect ECONNREFUSED 127.0.0.1:9911'), '127.0.0.1:9911');
	assert.strictEqual(d.class, 'transport');
	assert.strictEqual(d.code, 'ECONNREFUSED');
	assert.ok(/Nothing is listening at 127\.0\.0\.1:9911/.test(d.summary));
	// "fetch failed" alone is useless; the cause must survive into the detail.
	assert.ok(/ECONNREFUSED/.test(d.detail));
});
check('describeError: ENOTFOUND / ETIMEDOUT get their own remedy copy', () => {
	assert.ok(/resolve the host name/.test(c.describeError(fetchFailed('ENOTFOUND', 'getaddrinfo ENOTFOUND h'), 'h').summary));
	assert.ok(/did not respond in time/.test(c.describeError(fetchFailed('ETIMEDOUT', 'timeout'), 'h').summary));
});
check('describeError: TLS failures are transport and name the cert', () => {
	const d = c.describeError(fetchFailed('DEPTH_ZERO_SELF_SIGNED_CERT', 'self signed certificate'), 'h');
	assert.strictEqual(d.class, 'transport');
	assert.ok(/TLS certificate/.test(d.summary));
});
check('describeError: a bare "fetch failed" with no cause is still transport', () => {
	const d = c.describeError(new TypeError('fetch failed'), 'h');
	assert.strictEqual(d.class, 'transport');
	assert.strictEqual(d.detail, 'fetch failed');
});
check('describeError: ENGRAPHY_* tool errors stay tool errors (rendered inline)', () => {
	assert.strictEqual(c.describeError(new Error('ENGRAPHY_VALIDATION: bad scope')).class, 'tool');
	assert.strictEqual(c.describeError(new Error('ENGRAPHY_PENDING_EXPIRED: gone')).class, 'tool');
});
check('describeError: unset serverUrl is config, not transport', () => {
	assert.strictEqual(c.describeError(new Error('engraphy.serverUrl is not set.')).class, 'config');
});
check('describeError: accepts strings and non-errors without throwing', () => {
	assert.strictEqual(c.describeError('plain string').class, 'tool');
	assert.strictEqual(typeof c.describeError(null).summary, 'string');
	assert.strictEqual(typeof c.describeError(undefined).detail, 'string');
	assert.strictEqual(typeof c.describeError(42).summary, 'string');
	assert.strictEqual(typeof c.describeError({ nope: true }).summary, 'string');
});
check('hostLabel: host:port, junk passed through, never throws', () => {
	assert.strictEqual(c.hostLabel('http://127.0.0.1:8000/mcp/'), '127.0.0.1:8000');
	assert.strictEqual(c.hostLabel('https://engraphy.example/mcp/'), 'engraphy.example');
	assert.strictEqual(c.hostLabel('not a url'), 'not a url');
	assert.strictEqual(c.hostLabel(''), '');
});

group('connection: panel state');

check('computeConnection: unconfigured wins over everything', () => {
	assert.deepStrictEqual(c.computeConnection({ serverConfigured: false, errors: [] }), { kind: 'unconfigured' });
	assert.deepStrictEqual(c.computeConnection({ serverConfigured: false, errors: [SDK_401, SDK_401] }), {
		kind: 'unconfigured',
	});
});
check('computeConnection: all-auth failures → unauthorized (not "start a server")', () => {
	const st = c.computeConnection({ serverConfigured: true, errors: [SDK_401, SDK_401], host: '127.0.0.1:8000' });
	assert.strictEqual(st.kind, 'unauthorized');
	assert.strictEqual(st.code, '401');
	assert.ok(/rejected this token/.test(st.summary));
});
check('computeConnection: no token set says "requires a token", not "rejected"', () => {
	// Same 401 on the wire, two different situations. Telling someone who never
	// entered a token that theirs was "rejected" sends them hunting a fault that
	// does not exist, and it contradicts the badge, which already says
	// "Token needed".
	const st = c.computeConnection({
		serverConfigured: true,
		errors: [SDK_401, SDK_401],
		host: '127.0.0.1:8000',
		hasToken: false,
	});
	assert.strictEqual(st.kind, 'unauthorized');
	assert.strictEqual(st.hasToken, false);
	assert.ok(/requires a token/.test(st.summary), st.summary);
	assert.ok(!/rejected/.test(st.summary), st.summary);
});
check('computeConnection: token set keeps the "rejected" wording', () => {
	const st = c.computeConnection({
		serverConfigured: true,
		errors: [SDK_401, SDK_401],
		host: '127.0.0.1:8000',
		hasToken: true,
	});
	assert.ok(/rejected this token/.test(st.summary), st.summary);
	assert.strictEqual(st.hasToken, true);
});
check('computeConnection: all-transport failures → unreachable', () => {
	const e = fetchFailed('ECONNREFUSED', 'connect ECONNREFUSED 127.0.0.1:8000');
	const st = c.computeConnection({ serverConfigured: true, errors: [e, e], host: '127.0.0.1:8000' });
	assert.strictEqual(st.kind, 'unreachable');
	assert.ok(/Is the server running/.test(st.summary));
});
check('computeConnection: ONE band failing stays ok (per-band error renders inline)', () => {
	// Preserves the extension's rule. A single band erroring is not a dead server.
	assert.deepStrictEqual(
		c.computeConnection({ serverConfigured: true, errors: [SDK_401, null] }),
		{ kind: 'ok' }
	);
	assert.deepStrictEqual(c.computeConnection({ serverConfigured: true, errors: [null, null] }), { kind: 'ok' });
	assert.deepStrictEqual(c.computeConnection({ serverConfigured: true, errors: [] }), { kind: 'ok' });
});
check('computeConnection: tool errors on both bands stay ok', () => {
	const e = new Error('ENGRAPHY_VALIDATION: x');
	assert.deepStrictEqual(c.computeConnection({ serverConfigured: true, errors: [e, e] }), { kind: 'ok' });
});
check('computeConnection: mixed auth + transport does not claim either', () => {
	const st = c.computeConnection({
		serverConfigured: true,
		errors: [SDK_401, fetchFailed('ECONNREFUSED', 'x')],
	});
	assert.strictEqual(st.kind, 'ok');
});

group('connection: health badge');

const BASE_PROBE = {
	configured: true,
	hasToken: true,
	space: '',
	serverUrl: 'http://127.0.0.1:8000/mcp/',
	info: null,
	authOk: false,
};

check('health: unconfigured', () => {
	const h = c.buildHealthVM({ ...BASE_PROBE, configured: false });
	assert.strictEqual(h.phase, 'unconfigured');
	assert.strictEqual(h.label, 'No server set');
	assert.strictEqual(h.usable, false);
});
check('health: healthz OK but MCP 401 is NOT green (the shipped bug)', () => {
	// /healthz is unauthenticated on Engraphy, so it answers 200 for a server you
	// hold no valid token for. Trusting it alone put a green "Connected" badge
	// over four erroring panels. Reproduced against the live server with no
	// token set; this check is the regression guard.
	const h = c.buildHealthVM({
		...BASE_PROBE,
		info: { status: 'ok', version: '0.1.0', spaces: 4 },
		authOk: false,
		authError: SDK_401,
	});
	assert.notStrictEqual(h.phase, 'connected');
	assert.strictEqual(h.phase, 'unauthorized');
	assert.strictEqual(h.usable, false);
	assert.strictEqual(h.label, 'Token rejected');
	assert.ok(/engraphy-admin token create/.test(h.title));
});
check('health: unauthorized with NO token set says "Token needed"', () => {
	const h = c.buildHealthVM({ ...BASE_PROBE, hasToken: false, info: { status: 'ok' }, authError: SDK_401 });
	assert.strictEqual(h.phase, 'unauthorized');
	assert.strictEqual(h.label, 'Token needed');
	assert.ok(/requires a token/.test(h.title));
});
check('health: authenticated probe OK → connected, with space in the label', () => {
	const h = c.buildHealthVM({
		...BASE_PROBE,
		space: 'team',
		authOk: true,
		info: { status: 'ok', version: '0.1.0', spaces: 4 },
	});
	assert.strictEqual(h.phase, 'connected');
	assert.strictEqual(h.usable, true);
	assert.strictEqual(h.label, 'Connected · team');
	assert.ok(/v0\.1\.0/.test(h.title));
	assert.ok(/4 space\(s\)/.test(h.title));
});
check('health: connected with no space label omits the separator', () => {
	const h = c.buildHealthVM({ ...BASE_PROBE, authOk: true, info: { status: 'ok' } });
	assert.strictEqual(h.label, 'Connected');
});
check('health: transport failure → unreachable with a retry hint', () => {
	const h = c.buildHealthVM({
		...BASE_PROBE,
		healthzError: fetchFailed('ECONNREFUSED', 'connect ECONNREFUSED 127.0.0.1:8000'),
		authError: fetchFailed('ECONNREFUSED', 'connect ECONNREFUSED 127.0.0.1:8000'),
	});
	assert.strictEqual(h.phase, 'unreachable');
	assert.strictEqual(h.label, 'Unreachable');
	assert.ok(/Click to retry/.test(h.title));
	assert.ok(/ECONNREFUSED/.test(h.detail));
});
check('health: server answered but the probe tool errored → degraded, not green', () => {
	const h = c.buildHealthVM({
		...BASE_PROBE,
		info: { status: 'ok' },
		authError: new Error('ENGRAPHY_VALIDATION: nope'),
	});
	assert.strictEqual(h.phase, 'degraded');
	assert.strictEqual(h.usable, false);
});
check('health: the banner line agrees with the badge about the token', () => {
	// The badge, the banner and the panel block are three surfaces describing one
	// fact. They used to disagree: the badge said "Token needed" while the banner
	// and panel said "your token was rejected".
	const noToken = c.buildHealthVM({ ...BASE_PROBE, hasToken: false, info: { status: 'ok' }, authError: SDK_401 });
	assert.strictEqual(noToken.label, 'Token needed');
	assert.ok(/needs a token/.test(noToken.banner), noToken.banner);
	assert.ok(!/rejected/.test(noToken.banner), noToken.banner);

	const badToken = c.buildHealthVM({ ...BASE_PROBE, hasToken: true, info: { status: 'ok' }, authError: SDK_401 });
	assert.strictEqual(badToken.label, 'Token rejected');
	assert.ok(/rejected/.test(badToken.banner), badToken.banner);
});
check('health: every not-usable phase supplies banner copy', () => {
	const probes = [
		{ ...BASE_PROBE, configured: false },
		{ ...BASE_PROBE, authError: SDK_401 },
		{ ...BASE_PROBE, authError: fetchFailed('ECONNREFUSED', 'x') },
		{ ...BASE_PROBE, info: { status: 'ok' }, authError: new Error('ENGRAPHY_VALIDATION: nope') },
	];
	for (const p of probes) {
		const h = c.buildHealthVM(p);
		assert.strictEqual(h.usable, false);
		assert.ok(h.banner && h.banner.length > 0, 'no banner for ' + h.phase);
	}
	// A working connection needs no banner at all.
	assert.strictEqual(c.buildHealthVM({ ...BASE_PROBE, authOk: true }).banner, undefined);
});
check('health: every phase yields a non-empty label and title', () => {
	const probes = [
		{ ...BASE_PROBE, configured: false },
		{ ...BASE_PROBE, authOk: true },
		{ ...BASE_PROBE, authError: SDK_401 },
		{ ...BASE_PROBE, authError: fetchFailed('ECONNREFUSED', 'x') },
		{ ...BASE_PROBE, authError: new Error('weird') },
	];
	for (const p of probes) {
		const h = c.buildHealthVM(p);
		assert.ok(h.label && h.label.length > 0, 'label for ' + h.phase);
		assert.ok(h.title && h.title.length > 0, 'title for ' + h.phase);
		assert.strictEqual(typeof h.usable, 'boolean');
	}
});

group('validation: server URL');

check('validateServerUrl: empty is an error with a concrete example', () => {
	const f = v.validateServerUrl('');
	assert.strictEqual(f.issues[0].level, 'error');
	assert.ok(/127\.0\.0\.1:8000\/mcp\//.test(f.issues[0].message));
	assert.strictEqual(v.validateServerUrl('   ').issues[0].level, 'error');
});
check('validateServerUrl: a bare host:port errors and suggests http://', () => {
	const f = v.validateServerUrl('127.0.0.1:8000/mcp/');
	assert.strictEqual(f.issues[0].level, 'error');
	assert.ok(/adding http:\/\//.test(f.issues[0].message));
});
check('validateServerUrl: non-http scheme errors and names the scheme', () => {
	const f = v.validateServerUrl('ftp://host/x');
	assert.strictEqual(f.issues[0].level, 'error');
	assert.ok(/ftp:/.test(f.issues[0].message));
});
check('validateServerUrl: missing trailing slash is FIXED, not rejected', () => {
	const f = v.validateServerUrl('http://127.0.0.1:8000/mcp');
	assert.strictEqual(f.normalized, 'http://127.0.0.1:8000/mcp/');
	assert.strictEqual(f.issues.length, 1);
	assert.strictEqual(f.issues[0].level, 'warn');
	assert.ok(/307/.test(f.issues[0].message));
});
check('validateServerUrl: a clean local URL has zero issues', () => {
	const f = v.validateServerUrl('http://127.0.0.1:8000/mcp/');
	assert.deepStrictEqual(f.issues, []);
	assert.strictEqual(f.normalized, 'http://127.0.0.1:8000/mcp/');
});
check('validateServerUrl: warns on a path with no mcp segment, and on bare origin', () => {
	assert.ok(v.validateServerUrl('http://127.0.0.1:8000/api/').issues.some((i) => /mcp/.test(i.message)));
	assert.ok(v.validateServerUrl('http://127.0.0.1:8000').issues.some((i) => /server root/.test(i.message)));
});
check('validateServerUrl: plain http to a REMOTE host warns about the token', () => {
	const remote = v.validateServerUrl('http://engraphy.example/mcp/');
	assert.ok(remote.issues.some((i) => i.level === 'warn' && /unencrypted/.test(i.message)));
	// loopback and https are both fine
	assert.ok(!v.validateServerUrl('http://localhost:8000/mcp/').issues.some((i) => /unencrypted/.test(i.message)));
	assert.ok(!v.validateServerUrl('https://engraphy.example/mcp/').issues.some((i) => /unencrypted/.test(i.message)));
});
check('validateServerUrl: embedded whitespace errors instead of silently breaking', () => {
	assert.strictEqual(v.validateServerUrl('http://host /mcp/').issues[0].level, 'error');
});
check('isLocalHost: loopback forms and .local/.localhost suffixes', () => {
	assert.ok(v.isLocalHost('127.0.0.1'));
	assert.ok(v.isLocalHost('localhost'));
	assert.ok(v.isLocalHost('::1'));
	assert.ok(v.isLocalHost('MyBox.local'));
	assert.strictEqual(v.isLocalHost('engraphy.example'), false);
});

group('validation: token and space');

check('validateToken: undefined means "keep the stored token" and never errors', () => {
	const f = v.validateToken(undefined);
	assert.deepStrictEqual(f.issues, []);
	assert.strictEqual(f.normalized, '');
});
check('validateToken: strips a pasted "Bearer " prefix and warns', () => {
	const f = v.validateToken('Bearer abcdef123456');
	assert.strictEqual(f.normalized, 'abcdef123456');
	assert.ok(f.issues.some((i) => /Bearer/.test(i.message)));
});
check('validateToken: strips a whole pasted Authorization header', () => {
	const f = v.validateToken('Authorization: Bearer abcdef123456');
	assert.strictEqual(f.normalized, 'abcdef123456');
});
check('validateToken: trims surrounding whitespace from a terminal copy', () => {
	const f = v.validateToken('  abcdef123456\n');
	assert.strictEqual(f.normalized, 'abcdef123456');
	assert.ok(f.issues.some((i) => /whitespace/.test(i.message)));
});
check('validateToken: interior whitespace is an error (copied too much)', () => {
	const f = v.validateToken('abcdef 123456');
	assert.ok(f.issues.some((i) => i.level === 'error'));
});
check('validateToken: an explicit empty string is the documented clear signal', () => {
	const f = v.validateToken('');
	assert.strictEqual(f.normalized, '');
	assert.ok(!f.issues.some((i) => i.level === 'error'));
});
check('validateToken: a very short token warns but does not block', () => {
	const f = v.validateToken('abc');
	assert.ok(f.issues.some((i) => i.level === 'warn' && /short/.test(i.message)));
	assert.ok(!f.issues.some((i) => i.level === 'error'));
});
check('validateSpace: trims, allows empty, blocks absurd length and line breaks', () => {
	assert.strictEqual(v.validateSpace('  team  ').normalized, 'team');
	assert.deepStrictEqual(v.validateSpace('').issues, []);
	assert.ok(v.validateSpace('x'.repeat(65)).issues.some((i) => i.level === 'error'));
	assert.ok(v.validateSpace('a\nb').issues.some((i) => i.level === 'error'));
});

group('validation: whole form');

check('validateSettingsInput: clean input is ok with nothing changed', () => {
	const res = v.validateSettingsInput({ serverUrl: 'http://127.0.0.1:8000/mcp/', space: 'team' });
	assert.strictEqual(res.ok, true);
	assert.strictEqual(res.changed, false);
	assert.strictEqual(v.firstError(res), null);
});
check('validateSettingsInput: normalization is reported as changed', () => {
	const res = v.validateSettingsInput({ serverUrl: 'http://127.0.0.1:8000/mcp', space: 'team' });
	assert.strictEqual(res.ok, true);
	assert.strictEqual(res.changed, true);
	assert.strictEqual(res.serverUrl.normalized, 'http://127.0.0.1:8000/mcp/');
});
check('validateSettingsInput: any field error blocks the save', () => {
	const res = v.validateSettingsInput({ serverUrl: 'nope', space: '' });
	assert.strictEqual(res.ok, false);
	assert.ok(v.firstError(res));
});
check('validateSettingsInput: warnings alone never block', () => {
	const res = v.validateSettingsInput({ serverUrl: 'http://engraphy.example/api', space: '' });
	assert.strictEqual(res.ok, true);
	assert.ok(res.serverUrl.issues.length > 0);
});

group('explorer model');

check('nodeRefsFromSearch: maps {results:[{node}]} and drops idless rows', () => {
	const nodes = x.nodeRefsFromSearch({
		v: 1,
		results: [
			{ node: { id: 'n1', title: 'T', type: 'note', scope: 'team' }, score: 0.7 },
			{ score: 0.1 },
			{ node: { title: 'no id' } },
		],
	});
	assert.strictEqual(nodes.length, 1);
	assert.deepStrictEqual(nodes[0], { id: 'n1', title: 'T', type: 'note', scope: 'team' });
});
check('nodeRefsFromSearch: missing/garbage envelopes yield [] and never throw', () => {
	assert.deepStrictEqual(x.nodeRefsFromSearch({}), []);
	assert.deepStrictEqual(x.nodeRefsFromSearch(null), []);
	assert.deepStrictEqual(x.nodeRefsFromSearch('nope'), []);
	assert.deepStrictEqual(x.nodeRefsFromSearch({ results: 'nope' }), []);
});
check('toNodeRef: missing title/type/scope get placeholders, not "undefined"', () => {
	const n = x.toNodeRef({ id: 'n1' });
	assert.strictEqual(n.title, '(untitled)');
	assert.strictEqual(n.type, '?');
	assert.strictEqual(n.scope, '?');
});
check('nodeRefsFromTraverse: excludes the start node', () => {
	const nodes = x.nodeRefsFromTraverse(
		{ nodes: [{ id: 'start', title: 'S' }, { id: 'n2', title: 'N' }] },
		'start'
	);
	assert.deepStrictEqual(nodes.map((n) => n.id), ['n2']);
});
check('scopeIdsFrom: strings, {id}, {display_name}, deduped', () => {
	assert.deepStrictEqual(
		x.scopeIdsFrom({ scopes: ['team', { id: 'personal' }, { display_name: 'Ops' }, 'team', {}] }),
		['team', 'personal', 'Ops']
	);
	assert.deepStrictEqual(x.scopeIdsFrom({}), []);
	assert.deepStrictEqual(x.scopeIdsFrom(null), []);
});
check('nodeDetailFrom: real Engraphy get envelope → structured card, not raw JSON', () => {
	// Shape from engraphy/core/get.py.
	const d = x.nodeDetailFrom(
		{
			v: 1,
			nodes: [
				{
					id: 'n_ada',
					type: 'preference',
					scope: 'team',
					title: 'Ada prefers async standups',
					body: 'Written thread by 10am.',
					attrs: { strength: 'hard', zeta: { nested: true } },
					status: 'active',
					author: 'devon',
					created_at: '2026-08-01T00:00:00Z',
					addenda: ['merged from n_old'],
					edges: {
						out: [{ src: 'n_ada', dst: 'n_person', type: 'about' }],
						in: [{ src: 'n_proj', dst: 'n_ada', type: 'mentions' }],
					},
				},
			],
			missing: [],
		},
		'n_ada'
	);
	assert.strictEqual(d.title, 'Ada prefers async standups');
	assert.strictEqual(d.type, 'preference');
	assert.strictEqual(d.status, 'active');
	assert.strictEqual(d.author, 'devon');
	assert.strictEqual(d.body, 'Written thread by 10am.');
	// attrs sorted, non-strings stringified
	assert.deepStrictEqual(d.attrs.map((a) => a.key), ['strength', 'zeta']);
	assert.strictEqual(d.attrs[0].value, 'hard');
	assert.ok(/nested/.test(d.attrs[1].value));
	assert.deepStrictEqual(d.addenda, ['merged from n_old']);
	// edges point at the OTHER end, in both directions
	assert.deepStrictEqual(d.edges, [
		{ type: 'about', otherId: 'n_person', direction: 'out' },
		{ type: 'mentions', otherId: 'n_proj', direction: 'in' },
	]);
});
check('nodeDetailFrom: the dev stub\'s flat links:[{rel,to}] also render as edges', () => {
	const d = x.nodeDetailFrom(
		{ v: 1, nodes: [{ id: 'n_ada', title: 'A', links: [{ rel: 'about', to: 'n_person' }] }] },
		'n_ada'
	);
	assert.deepStrictEqual(d.edges, [{ type: 'about', otherId: 'n_person', direction: 'out' }]);
});
check('nodeDetailFrom: a missing id returns null (existence is information)', () => {
	assert.strictEqual(x.nodeDetailFrom({ v: 1, nodes: [], missing: ['n_gone'] }, 'n_gone'), null);
	assert.strictEqual(x.nodeDetailFrom({}, 'n1'), null);
	assert.strictEqual(x.nodeDetailFrom(null, 'n1'), null);
});
check('nodeDetailFrom: tolerates absent optional fields without "undefined" leaking', () => {
	const d = x.nodeDetailFrom({ nodes: [{ id: 'n1' }] }, 'n1');
	assert.strictEqual(d.title, '(untitled)');
	assert.strictEqual(d.status, null);
	assert.strictEqual(d.author, null);
	assert.strictEqual(d.createdAt, null);
	assert.strictEqual(d.body, '');
	assert.deepStrictEqual(d.attrs, []);
	assert.deepStrictEqual(d.edges, []);
});
check('observedTypesFrom: distinct, sorted, drops the placeholder', () => {
	assert.deepStrictEqual(
		x.observedTypesFrom([
			{ id: '1', title: 'a', type: 'project', scope: 's' },
			{ id: '2', title: 'b', type: 'note', scope: 's' },
			{ id: '3', title: 'c', type: 'note', scope: 's' },
			{ id: '4', title: 'd', type: '?', scope: 's' },
		]),
		['note', 'project']
	);
	assert.deepStrictEqual(x.observedTypesFrom([]), []);
});
check('mergeNodeTypes: the space\'s OWN types lead, starter types stay below', () => {
	// The live case this fixes: that space holds `note` and `project`, while
	// STARTER_NODE_TYPES offers `project_ref`. The promote form was offering a
	// type the space does not use, and the right one only via "Other...".
	const merged = x.mergeNodeTypes(['note', 'project'], w.STARTER_NODE_TYPES);
	const types = merged.map((m) => m.type);
	assert.deepStrictEqual(types.slice(0, 2), ['note', 'project'], 'observed first');
	assert.ok(types.includes('project_ref'), 'starter entries are kept, not replaced');
	assert.strictEqual(new Set(types).size, types.length, 'no duplicates');
	// A type that is BOTH observed and known keeps the starter description.
	const note = merged.find((m) => m.type === 'note');
	assert.strictEqual(note.description, w.STARTER_NODE_TYPES.find((s) => s.type === 'note').description);
	// A type only the space knows about still gets a description.
	const project = merged.find((m) => m.type === 'project');
	assert.ok(project.description.length > 0);
});
check('mergeNodeTypes: with nothing observed it is exactly the starter list', () => {
	assert.deepStrictEqual(x.mergeNodeTypes([], w.STARTER_NODE_TYPES), w.STARTER_NODE_TYPES);
});
check('mergeNodeTypes: ignores blanks and duplicates in the observed list', () => {
	const merged = x.mergeNodeTypes(['note', 'note', '', 'note'], w.STARTER_NODE_TYPES);
	assert.strictEqual(merged.filter((m) => m.type === 'note').length, 1);
	assert.ok(!merged.some((m) => m.type === ''));
});
check('missingIdsFrom: reads the missing list defensively', () => {
	assert.deepStrictEqual(x.missingIdsFrom({ missing: ['a', 'b'] }), ['a', 'b']);
	assert.deepStrictEqual(x.missingIdsFrom({}), []);
	assert.deepStrictEqual(x.missingIdsFrom(null), []);
});

group('renderer -> main message parsing (the trust boundary)');

check('parseConfirmCommand: delegates the shared commands to the frozen parser', () => {
	assert.deepStrictEqual(m.parseConfirmCommand({ type: 'ready' }), { type: 'ready' });
	assert.deepStrictEqual(m.parseConfirmCommand({ type: 'refresh' }), { type: 'refresh' });
	assert.deepStrictEqual(m.parseConfirmCommand({ type: 'approve', pendingId: 'p1' }), {
		type: 'approve',
		pendingId: 'p1',
	});
	assert.deepStrictEqual(m.parseConfirmCommand({ type: 'merge', pendingId: 'p1', mergeInto: 'n9' }), {
		type: 'merge',
		pendingId: 'p1',
		mergeInto: 'n9',
	});
	assert.deepStrictEqual(m.parseConfirmCommand({ type: 'discard', inboxId: 'i1' }), {
		type: 'discard',
		inboxId: 'i1',
	});
});
check('parseConfirmCommand: rejects the same junk the frozen parser rejects', () => {
	assert.strictEqual(m.parseConfirmCommand({ type: 'approve' }), null);
	assert.strictEqual(m.parseConfirmCommand({ type: 'approve', pendingId: '' }), null);
	assert.strictEqual(m.parseConfirmCommand({ type: 'merge', pendingId: 'p1' }), null);
	assert.strictEqual(m.parseConfirmCommand({ type: 'nope' }), null);
	assert.strictEqual(m.parseConfirmCommand(null), null);
	assert.strictEqual(m.parseConfirmCommand('approve'), null);
});
check('parseConfirmCommand: the node type travels as nodeType, never as type', () => {
	// The bug this pins: the renderer posted
	//   { type: 'promoteSubmit', inboxId, type, scope, title, body }
	// where the shorthand `type` (the NODE type) overwrote the message's own
	// `type` discriminator, because an object literal keeps the last value. The
	// message arrived as {type:'note',...}, the host's switch never matched, and
	// Promote silently did nothing: modal closed, panel went busy, no write, no
	// error. Caught by driving the modal in scripts/smoke.js, not by any unit
	// test, which is why the smoke scenario now clicks through it.
	const clobbered = { type: 'promoteSubmit', inboxId: 'i1', scope: 's', title: 'T', body: 'B' };
	clobbered.type = 'note'; // what the old shorthand actually produced
	assert.strictEqual(m.parseConfirmCommand(clobbered), null, 'a clobbered discriminator must not parse');

	const good = m.parseConfirmCommand({
		type: 'promoteSubmit',
		inboxId: 'i1',
		nodeType: 'note',
		scope: 's',
		title: 'T',
		body: 'B',
	});
	assert.deepStrictEqual(good, {
		type: 'promoteSubmit',
		inboxId: 'i1',
		nodeType: 'note',
		scope: 's',
		title: 'T',
		body: 'B',
	});
});
check('parseConfirmCommand: promoteSubmit requires every field except a non-empty body', () => {
	const base = { type: 'promoteSubmit', inboxId: 'i1', nodeType: 'note', scope: 's', title: 'T', body: 'B' };
	for (const missing of ['inboxId', 'nodeType', 'scope', 'title']) {
		const copy = { ...base };
		delete copy[missing];
		assert.strictEqual(m.parseConfirmCommand(copy), null, 'missing ' + missing + ' must not parse');
	}
	// An empty body is legitimate (a title-only memory), but a non-string is not.
	assert.strictEqual(m.parseConfirmCommand({ ...base, body: '' }).body, '');
	assert.strictEqual(m.parseConfirmCommand({ ...base, body: { nope: 1 } }), null);
	assert.strictEqual(m.parseConfirmCommand({ ...base, body: undefined }), null);
});
check('parseConfirmCommand: desktop-only reconnect passes; webview-only promote does not', () => {
	assert.deepStrictEqual(m.parseConfirmCommand({ type: 'reconnect' }), { type: 'reconnect' });
	// `promote` opens the modal locally via an invoke, so it never reaches the
	// host as a fire-and-forget command.
	assert.strictEqual(m.parseConfirmCommand({ type: 'promote', inboxId: 'i1' }), null);
});
check('parseStatsCommand: validates range/group and passes reconnect', () => {
	assert.deepStrictEqual(m.parseStatsCommand({ type: 'setRange', rangeDays: 14 }), {
		type: 'setRange',
		rangeDays: 14,
	});
	assert.deepStrictEqual(m.parseStatsCommand({ type: 'setGroup', groupBy: 'user' }), {
		type: 'setGroup',
		groupBy: 'user',
	});
	assert.deepStrictEqual(m.parseStatsCommand({ type: 'reconnect' }), { type: 'reconnect' });
	assert.strictEqual(m.parseStatsCommand({ type: 'setRange', rangeDays: 0 }), null);
	assert.strictEqual(m.parseStatsCommand({ type: 'setGroup', groupBy: 'org' }), null);
	assert.strictEqual(m.parseStatsCommand({ type: 'nope' }), null);
	assert.strictEqual(m.parseStatsCommand(null), null);
});
check('parseAppCommand: only the two it knows', () => {
	assert.deepStrictEqual(m.parseAppCommand({ type: 'reconnect' }), { type: 'reconnect' });
	assert.deepStrictEqual(m.parseAppCommand({ type: 'refreshAll' }), { type: 'refreshAll' });
	assert.strictEqual(m.parseAppCommand({ type: 'quit' }), null);
	assert.strictEqual(m.parseAppCommand(null), null);
});

group('window state restore');

// Two side-by-side displays, the second one to the right.
const PRIMARY = { x: 0, y: 0, width: 1920, height: 1040 };
const SECOND = { x: 1920, y: 0, width: 1920, height: 1040 };

check('restoreBounds: no saved state → the default size, no position', () => {
	assert.deepStrictEqual(win.restoreBounds(undefined, [PRIMARY]), win.DEFAULT_BOUNDS);
});
check('restoreBounds: a position on a live display is restored', () => {
	const r = win.restoreBounds({ x: 200, y: 120, width: 1200, height: 800 }, [PRIMARY]);
	assert.deepStrictEqual(r, { width: 1200, height: 800, x: 200, y: 120 });
});
check('restoreBounds: a position on an UNPLUGGED display is dropped', () => {
	// The failure this prevents: the window was last closed on a second monitor
	// that is no longer attached, and reopens entirely off-screen with no way to
	// drag it back. Size is kept; only the position is discarded, so the OS
	// centres it.
	const r = win.restoreBounds({ x: 2400, y: 300, width: 1200, height: 800 }, [PRIMARY]);
	assert.deepStrictEqual(r, { width: 1200, height: 800 });
	// Still attached → still restored.
	const both = win.restoreBounds({ x: 2400, y: 300, width: 1200, height: 800 }, [PRIMARY, SECOND]);
	assert.strictEqual(both.x, 2400);
});
check('restoreBounds: a mostly off-screen window still counts if a usable strip overlaps', () => {
	const r = win.restoreBounds({ x: 1850, y: 900, width: 1200, height: 800 }, [PRIMARY]);
	assert.strictEqual(r.x, 1850, 'a window hanging off the right edge is still grabbable');
});
check('restoreBounds: a window fully above the work area is dropped', () => {
	const r = win.restoreBounds({ x: 100, y: -900, width: 1200, height: 800 }, [PRIMARY]);
	assert.strictEqual(r.x, undefined);
});
check('restoreBounds: a tiny saved size is clamped to the app minimum', () => {
	const r = win.restoreBounds({ x: 0, y: 0, width: 10, height: 10 }, [PRIMARY]);
	assert.strictEqual(r.width, win.MIN_WIDTH);
	assert.strictEqual(r.height, win.MIN_HEIGHT);
});
check('restoreBounds: with no displays at all nothing positions (never throws)', () => {
	const r = win.restoreBounds({ x: 100, y: 100, width: 900, height: 700 }, []);
	assert.deepStrictEqual(r, { width: 900, height: 700 });
});
check('sanitizeSavedBounds: rejects junk, NaN, absurd sizes', () => {
	assert.strictEqual(win.sanitizeSavedBounds(null), undefined);
	assert.strictEqual(win.sanitizeSavedBounds('nope'), undefined);
	assert.strictEqual(win.sanitizeSavedBounds({}), undefined);
	assert.strictEqual(win.sanitizeSavedBounds({ width: NaN, height: 700 }), undefined);
	assert.strictEqual(win.sanitizeSavedBounds({ width: 5, height: 5 }), undefined);
	assert.strictEqual(win.sanitizeSavedBounds({ width: 99999, height: 700 }), undefined);
});
check('sanitizeSavedBounds: keeps a good record and drops partial positions', () => {
	assert.deepStrictEqual(win.sanitizeSavedBounds({ width: 900, height: 700, x: 1, y: 2, maximized: true }), {
		width: 900,
		height: 700,
		x: 1,
		y: 2,
		maximized: true,
	});
	const partial = win.sanitizeSavedBounds({ width: 900, height: 700, x: 5 });
	assert.strictEqual(partial.x, 5);
	assert.strictEqual(partial.y, undefined);
});

group('ipc result contract');

check('ok(): tags the payload without clobbering it', () => {
	assert.deepStrictEqual(r.ok({ nodes: [1, 2] }), { ok: true, nodes: [1, 2] });
});
check('fail(): classifies, and is distinguishable from an empty success', () => {
	const f = r.fail(SDK_401, '127.0.0.1:8000');
	assert.strictEqual(f.ok, false);
	assert.strictEqual(f.error.class, 'auth');
	assert.ok(r.isFailure(f));
	// The bug this contract prevents: a renderer reading `.nodes` off a failure
	// and rendering "no results" for a rejected token.
	assert.strictEqual(f.nodes, undefined);
	assert.strictEqual(r.isFailure(r.ok({ nodes: [] })), false);
});
check('isFailure: safe on junk', () => {
	assert.strictEqual(r.isFailure(null), false);
	assert.strictEqual(r.isFailure(undefined), false);
	assert.strictEqual(r.isFailure('nope'), false);
	assert.strictEqual(r.isFailure({}), false);
});


group('graph model + harvest (desktop-owned)');

const NODE = (id, over) =>
	Object.assign(
		{
			id: id,
			type: 'note',
			scope: 'proj-a',
			title: 'T ' + id,
			status: 'active',
			author: 'devon',
			created_at: '2026-08-01T00:00:00+00:00',
		},
		over || {}
	);

check('parseNode: keeps the fields the canvas needs, defaults the rest', () => {
	const n = gm.parseNode({ id: 'a', type: 'project', scope: 's', title: 'Hi', status: 'active' });
	assert.strictEqual(n.id, 'a');
	assert.strictEqual(n.type, 'project');
	assert.strictEqual(n.author, null);
	// A node with no id cannot be drawn or linked to, so it is dropped, not faked.
	assert.strictEqual(gm.parseNode({ type: 'note' }), null);
	assert.strictEqual(gm.parseNode(null), null);
	assert.strictEqual(gm.parseNode('nope'), null);
});
check('parseEdge: needs both endpoints', () => {
	assert.deepStrictEqual(gm.parseEdge({ src: 'a', dst: 'b' }), { src: 'a', dst: 'b', type: 'relates_to' });
	assert.strictEqual(gm.parseEdge({ src: 'a' }), null);
	assert.strictEqual(gm.parseEdge({ dst: 'b' }), null);
});
check('nodesFromBriefing: flattens every section, tolerates junk', () => {
	const out = gm.nodesFromBriefing({
		sections: [
			{ name: 'relevant', nodes: [NODE('a')] },
			{ name: 'recent_notes', nodes: [NODE('b'), null, 7] },
			{ name: 'broken' },
			'not-a-section',
		],
	});
	assert.deepStrictEqual(out.map((n) => n.id), ['a', 'b']);
	assert.deepStrictEqual(gm.nodesFromBriefing(null), []);
});
check('nodesFromSearch: reads the node off each result row', () => {
	const out = gm.nodesFromSearch({ results: [{ node: NODE('a'), score: 1 }, { score: 2 }] });
	assert.deepStrictEqual(out.map((n) => n.id), ['a']);
});
check('parseTraverse: carries depth and the truncation flag', () => {
	const walk = gm.parseTraverse({
		nodes: [Object.assign(NODE('a'), { depth: 0 }), Object.assign(NODE('b'), { depth: 1 })],
		edges: [{ src: 'a', dst: 'b', type: 'references' }],
		truncated: true,
	});
	assert.strictEqual(walk.nodes[1].depth, 1);
	assert.strictEqual(walk.truncated, true);
	// Absent `truncated` must read as false, never as truthy-undefined: the
	// closure decides whether to mark neighbours complete off this flag.
	assert.strictEqual(gm.parseTraverse({ nodes: [], edges: [] }).truncated, false);
});
check('isRenderableNode: drops the engine sentinel and inactive rows', () => {
	assert.strictEqual(gm.isRenderableNode(gm.parseNode(NODE('a'))), true);
	assert.strictEqual(gm.isRenderableNode(gm.parseNode(NODE('s', { type: 'engraphy_sentinel' }))), false);
	assert.strictEqual(gm.isRenderableNode(gm.parseNode(NODE('m', { status: 'merged' }))), false);
});
check('pruneDanglingEdges: an edge to a node we never saw is dropped', () => {
	const nodes = [gm.parseNode(NODE('a')), gm.parseNode(NODE('b'))];
	const edges = [
		{ src: 'a', dst: 'b', type: 'relates_to' },
		{ src: 'a', dst: 'ghost', type: 'references' },
	];
	// Handing cytoscape an edge with a missing endpoint invents a node or throws;
	// dropping it keeps the reported link count honest instead.
	assert.deepStrictEqual(gm.pruneDanglingEdges(nodes, edges), [edges[0]]);
});
check('scopeTallies: biggest cluster first, unseen scopes still listed', () => {
	const nodes = [NODE('a'), NODE('b'), NODE('c', { scope: 'proj-b' })].map(gm.parseNode);
	const out = gm.scopeTallies(
		[
			{ id: 'proj-a', display_name: 'A' },
			{ id: 'proj-b', display_name: 'B' },
			{ id: 'empty', display_name: 'E' },
		],
		nodes
	);
	assert.deepStrictEqual(out.map((s2) => [s2.id, s2.count]), [['proj-a', 2], ['proj-b', 1], ['empty', 0]]);
});
check('typeTallies / edgeTypeTallies: counted and sorted for the legend', () => {
	const nodes = [NODE('a'), NODE('b', { type: 'project' }), NODE('c')].map(gm.parseNode);
	assert.deepStrictEqual(gm.typeTallies(nodes), [
		{ type: 'note', count: 2 },
		{ type: 'project', count: 1 },
	]);
	assert.deepStrictEqual(
		gm.edgeTypeTallies([{ type: 'relates_to' }, { type: 'relates_to' }, { type: 'references' }]),
		[{ type: 'relates_to', count: 2 }, { type: 'references', count: 1 }]
	);
});
check('parseGraphMessage: the renderer trust boundary', () => {
	assert.deepStrictEqual(gm.parseGraphMessage({ type: 'build', deepSweep: true }), {
		type: 'build',
		deepSweep: true,
	});
	// A missing or garbage deepSweep must default OFF: the sweep spends `search`
	// calls, which move the Impact & usage counters.
	assert.deepStrictEqual(gm.parseGraphMessage({ type: 'build' }), { type: 'build', deepSweep: false });
	assert.deepStrictEqual(gm.parseGraphMessage({ type: 'build', deepSweep: 'yes' }), {
		type: 'build',
		deepSweep: false,
	});
	assert.strictEqual(gm.parseGraphMessage({ type: 'drop-table' }), null);
	assert.strictEqual(gm.parseGraphMessage(null), null);
	assert.strictEqual(gm.parseGraphMessage('build'), null);
});
check('parseSnapshot: round-trips a cache file, refuses a bad one', () => {
	const snap = {
		v: 1,
		space: 'devon',
		builtAt: '2026-08-22T00:00:00.000Z',
		scopes: [{ id: 'proj-a', displayName: 'A', visibility: 'private', ambient: false }],
		nodes: [NODE('a')],
		edges: [{ src: 'a', dst: 'a', type: 'relates_to' }],
		stats: { briefingCalls: 1, traverseCalls: 2, searchCalls: 0, deepSweep: false },
	};
	const back = gm.parseSnapshot(JSON.parse(JSON.stringify(snap)));
	assert.strictEqual(back.space, 'devon');
	assert.strictEqual(back.scopes[0].displayName, 'A');
	assert.strictEqual(back.stats.traverseCalls, 2);
	assert.strictEqual(gm.parseSnapshot({ v: 2, space: 'devon', nodes: [NODE('a')] }), null);
	assert.strictEqual(gm.parseSnapshot({ v: 1, space: 'devon', nodes: [] }), null);
	assert.strictEqual(gm.parseSnapshot(null), null);
});
check('retryAfterMs: reads the server backoff, ignores anything else', () => {
	assert.strictEqual(
		gh.retryAfterMs(new Error('ENGRAPHY_RATE_LIMITED: read window exceeded (60/min), retry after 55145ms')),
		55145
	);
	assert.strictEqual(gh.retryAfterMs(new Error('ENGRAPHY_VALIDATION: nope')), null);
	assert.strictEqual(gh.retryAfterMs(null), null);
});

/**
 * A tiny in-memory Engraphy whose reads obey the SAME caps as the real server, so
 * the harvest is exercised against the constraints it exists to work around:
 * briefing returns a handful of seeds, and traverse emits one walk row per edge
 * ordered by depth and cut at 50 rows.
 */
function fakeServer(nodes, edges, opts) {
	const o = opts || {};
	const calls = { scopeList: 0, briefing: 0, traverse: 0, search: 0 };
	const byId = new Map(nodes.map((n) => [n.id, n]));
	const nbrs = (id) => edges.filter((e) => e.src === id || e.dst === id);
	return {
		calls: calls,
		tools: {
			scopeList: async () => {
				calls.scopeList++;
				const ids = [...new Set(nodes.map((n) => n.scope))];
				return { v: 1, scopes: ids.map((id) => ({ id: id, display_name: id.toUpperCase() })) };
			},
			briefing: async (scope) => {
				calls.briefing++;
				const cap = o.briefingCap === undefined ? 10 : o.briefingCap;
				const hits = nodes.filter((n) => n.scope === scope).slice(0, cap);
				return { v: 1, sections: [{ name: 'relevant', nodes: hits }] };
			},
			search: async (a) => {
				calls.search++;
				return {
					v: 1,
					results: nodes.filter((n) => n.scope === a.scope).slice(0, 25).map((n) => ({ node: n })),
				};
			},
			traverse: async (a) => {
				calls.traverse++;
				const limit = a.limit || 50;
				const maxDepth = a.maxDepth || 2;
				// Honour direction and edge_types, or the hub-partitioning fallback
				// would appear to work here while doing nothing on a real server.
				const wanted = (e, id) =>
					(!a.edgeTypes || a.edgeTypes.includes(e.type)) &&
					(a.direction === 'both' ||
						(a.direction === 'out' && e.src === id) ||
						(a.direction === 'in' && e.dst === id));
				const rows = [];
				let frontier = [a.startId];
				const seen = new Set([a.startId]);
				const depthOf = new Map([[a.startId, 0]]);
				for (let d = 1; d <= maxDepth; d++) {
					const next = [];
					for (const id of frontier) {
						for (const e of nbrs(id)) {
							if (!wanted(e, id)) {
								continue;
							}
							const other = e.src === id ? e.dst : e.src;
							rows.push({ depth: d, edge: e, node: other });
							if (!seen.has(other)) {
								seen.add(other);
								depthOf.set(other, d);
								next.push(other);
							}
						}
					}
					frontier = next;
				}
				const truncated = rows.length > limit;
				const kept = rows.slice(0, limit);
				const outIds = new Set([a.startId]);
				for (const row of kept) {
					outIds.add(row.node);
				}
				return {
					v: 1,
					nodes: [...outIds].map((id) =>
						Object.assign({}, byId.get(id), { depth: depthOf.has(id) ? depthOf.get(id) : 0 })
					),
					edges: kept.map((row) => ({ src: row.edge.src, dst: row.edge.dst, type: row.edge.type })),
					truncated: truncated,
				};
			},
		},
	};
}

/** Pacing is exercised in its own check; everything else runs on a still clock. */
const HARVEST_OPTS = {
	space: 'devon',
	readsPerMin: 1e9,
	now: () => 0,
	sleep: async () => {},
};

checkAsync('harvest: reaches every LINKED node and edge through briefing + traverse', async () => {
	// Two scopes, one chain each, plus a cross-scope link. Briefing only ever
	// reveals the FIRST node of each scope, so everything else has to be found by
	// walking — which is the property that matters.
	const nodes = [];
	const edges = [];
	for (const scope of ['s1', 's2']) {
		for (let i = 0; i < 12; i++) {
			nodes.push(NODE(scope + '-' + i, { scope: scope }));
		}
		for (let i = 0; i < 11; i++) {
			edges.push({ src: scope + '-' + i, dst: scope + '-' + (i + 1), type: 'relates_to' });
		}
	}
	edges.push({ src: 's1-11', dst: 's2-0', type: 'references' });
	const srv = fakeServer(nodes, edges, { briefingCap: 1 });
	const snap = await gh.harvestGraph(srv.tools, HARVEST_OPTS);
	assert.strictEqual(snap.nodes.length, 24);
	assert.strictEqual(snap.edges.length, 23);
	assert.strictEqual(snap.stats.searchCalls, 0, 'the default harvest must never call search');
	assert.strictEqual(snap.stats.deepSweep, false);
});
checkAsync('harvest: an ISOLATED node is missed by default and found by the sweep', async () => {
	// The honest limit of a walk-based index: a node with no edges is reachable
	// only from a seed. Briefing here surfaces one node per scope, so the lone
	// orphan stays invisible until `search` sweeps for it.
	const nodes = [NODE('a', { scope: 's1' }), NODE('b', { scope: 's1' }), NODE('orphan', { scope: 's1' })];
	const edges = [{ src: 'a', dst: 'b', type: 'relates_to' }];
	const plain = await gh.harvestGraph(fakeServer(nodes, edges, { briefingCap: 1 }).tools, HARVEST_OPTS);
	assert.deepStrictEqual(plain.nodes.map((n) => n.id).sort(), ['a', 'b']);

	const swept = await gh.harvestGraph(
		fakeServer(nodes, edges, { briefingCap: 1 }).tools,
		Object.assign({}, HARVEST_OPTS, { deepSweep: true })
	);
	assert.deepStrictEqual(swept.nodes.map((n) => n.id).sort(), ['a', 'b', 'orphan']);
	assert.ok(swept.stats.searchCalls > 0);
	assert.strictEqual(swept.stats.deepSweep, true);
});
checkAsync('harvest: a HUB past the 50-row cap still gets its whole edge list', async () => {
	// 70 spokes exceeds the 50-row walk cap, so the depth-2 read truncates and one
	// page can never hold this node's edges. The harvest has to partition the read
	// — by direction, then by relationship — to get all of them. Losing this would
	// silently under-draw the busiest node in the graph, which is the one node a
	// graph view exists to show.
	//
	// Directions and relationships are BOTH mixed here, mirroring the real space
	// (relates_to / references / supersedes), so each slice fits under the cap.
	const nodes = [NODE('hub', { scope: 's1' })];
	const edges = [];
	const types = ['relates_to', 'references', 'supersedes'];
	for (let i = 0; i < 70; i++) {
		nodes.push(NODE('spoke-' + i, { scope: 's1' }));
		const t = types[i % types.length];
		edges.push(i % 2 ? { src: 'hub', dst: 'spoke-' + i, type: t } : { src: 'spoke-' + i, dst: 'hub', type: t });
	}
	const srv = fakeServer(nodes, edges, { briefingCap: 1 });
	const snap = await gh.harvestGraph(srv.tools, HARVEST_OPTS);
	assert.strictEqual(snap.edges.length, 70, 'every spoke edge must survive the cap');
	assert.strictEqual(snap.nodes.length, 71);
	assert.ok(snap.stats.truncatedWalks > 0);
	assert.strictEqual(snap.stats.unreadableHubs, 0);
});
checkAsync('harvest: an UNPARTITIONABLE hub is reported, not silently truncated', async () => {
	// The one case the frozen tool surface genuinely cannot serve: 70 edges that
	// are all the same relationship in the same direction, so neither axis splits
	// them and every slice is still the whole 70. The contract that matters is
	// that the shortfall is COUNTED rather than passed off as a complete graph.
	const nodes = [NODE('hub', { scope: 's1' })];
	const edges = [];
	for (let i = 0; i < 70; i++) {
		nodes.push(NODE('spoke-' + i, { scope: 's1' }));
		edges.push({ src: 'hub', dst: 'spoke-' + i, type: 'references' });
	}
	const srv = fakeServer(nodes, edges, { briefingCap: 1 });
	const snap = await gh.harvestGraph(srv.tools, HARVEST_OPTS);
	assert.strictEqual(snap.edges.length, 50, 'one page is all the tool can give here');
	assert.strictEqual(snap.stats.unreadableHubs, 1, 'the shortfall must be reported');
});
checkAsync('harvest: reports progress and never re-walks a closed node', async () => {
	const nodes = [];
	const edges = [];
	for (let i = 0; i < 8; i++) {
		nodes.push(NODE('n' + i, { scope: 's1' }));
	}
	for (let i = 0; i < 7; i++) {
		edges.push({ src: 'n' + i, dst: 'n' + (i + 1), type: 'relates_to' });
	}
	const srv = fakeServer(nodes, edges, { briefingCap: 1 });
	const phases = [];
	const snap = await gh.harvestGraph(
		srv.tools,
		Object.assign({}, HARVEST_OPTS, { onProgress: (p) => phases.push(p.phase) })
	);
	assert.strictEqual(snap.nodes.length, 8);
	// At most one traverse per node: the `closed` set is what stops the walk from
	// looping forever over a connected graph.
	assert.ok(srv.calls.traverse <= 8, 'traverse calls=' + srv.calls.traverse);
	assert.ok(
		phases.includes('scopes') &&
			phases.includes('seeding') &&
			phases.includes('walking') &&
			phases.includes('done')
	);
});
checkAsync('harvest: cancellation stops the walk instead of finishing it', async () => {
	const nodes = [];
	const edges = [];
	for (let i = 0; i < 30; i++) {
		nodes.push(NODE('n' + i, { scope: 's1' }));
	}
	for (let i = 0; i < 29; i++) {
		edges.push({ src: 'n' + i, dst: 'n' + (i + 1), type: 'relates_to' });
	}
	const srv = fakeServer(nodes, edges, { briefingCap: 1 });
	let ticks = 0;
	let threw = null;
	try {
		await gh.harvestGraph(srv.tools, Object.assign({}, HARVEST_OPTS, { isCancelled: () => ++ticks > 6 }));
	} catch (e) {
		threw = e;
	}
	assert.ok(threw instanceof gh.Cancelled, 'expected Cancelled, got ' + threw);
	assert.ok(srv.calls.traverse < 30);
});
checkAsync('harvest: a rate-limited read is retried, not surfaced as a failure', async () => {
	const nodes = [NODE('a', { scope: 's1' }), NODE('b', { scope: 's1' })];
	const edges = [{ src: 'a', dst: 'b', type: 'relates_to' }];
	const srv = fakeServer(nodes, edges, { briefingCap: 1 });
	let refusals = 0;
	const realTraverse = srv.tools.traverse;
	srv.tools.traverse = async (a) => {
		if (refusals < 2) {
			refusals++;
			throw new Error('ENGRAPHY_RATE_LIMITED: read window exceeded (60/min), retry after 900ms');
		}
		return realTraverse(a);
	};
	let slept = 0;
	const snap = await gh.harvestGraph(
		srv.tools,
		Object.assign({}, HARVEST_OPTS, { sleep: async (ms) => { slept += ms; } })
	);
	assert.strictEqual(refusals, 2);
	assert.ok(slept >= 1800, 'must honour the server backoff; slept=' + slept);
	assert.strictEqual(snap.nodes.length, 2);
	assert.ok(snap.stats.rateLimitWaitsMs >= 1800);
});
checkAsync('harvest: the pacer holds the client under the read budget', async () => {
	// Fake clock: time advances ONLY when the pacer sleeps, so a harvest that ran
	// past its per-minute budget without waiting would leave the clock at zero.
	const nodes = [];
	const edges = [];
	for (let i = 0; i < 25; i++) {
		nodes.push(NODE('n' + i, { scope: 's1' }));
	}
	for (let i = 0; i < 24; i++) {
		edges.push({ src: 'n' + i, dst: 'n' + (i + 1), type: 'relates_to' });
	}
	const srv = fakeServer(nodes, edges, { briefingCap: 1 });
	const clock = { t: 0 };
	const snap = await gh.harvestGraph(srv.tools, {
		space: 'devon',
		readsPerMin: 10,
		now: () => clock.t,
		sleep: async (ms) => {
			clock.t += ms;
		},
	});
	const reads = snap.stats.briefingCalls + snap.stats.traverseCalls + snap.stats.searchCalls + 1;
	assert.ok(reads > 10, 'reads=' + reads);
	assert.ok(clock.t >= 60000, 'the pacer never waited; elapsed=' + clock.t);
});


// ---- update checking -------------------------------------------------------
//
// versionCheck.ts is carried verbatim from the VS Code extension apart from
// PRODUCT_KEY, so the first half of these checks is that suite's, repeated here
// as the re-sync guard: a change that lands in one copy and not the other fails
// on whichever side was missed. The second half is desktop-only, because how
// this app updates depends on how it was installed and the extension has no
// equivalent question.

check('versionCheck: the copy here orders versions the same way', () => {
	assert.strictEqual(vc.compareVersions('0.1.0', '0.1.0'), 0);
	assert.ok(vc.compareVersions('0.1.0', '0.2.0') < 0);
	assert.ok(vc.compareVersions('0.10.0', '0.9.0') > 0, 'numeric, not lexical');
	assert.ok(vc.compareVersions('0.2.0-rc.1', '0.2.0') < 0);
	assert.strictEqual(vc.compareVersions('not a version', '0.1.0'), null);
});

check('versionCheck: PRODUCT_KEY here is the desktop app', () => {
	// Reading the extension's key would compare this app against 0.5.2 and
	// report an update that is not for it.
	assert.strictEqual(vc.PRODUCT_KEY, 'desktop');
});

function versionManifest(desktop) {
	return {
		schema: 1,
		products: {
			desktop: Object.assign(
				{
					latest: '0.2.0',
					minimumSupported: '0.1.0',
					notes: 'https://github.com/devon-clarkk/engraphy/releases/tag/v0.2.0',
					downloads: [
						{
							kind: 'nsis',
							url: 'https://github.com/devon-clarkk/engraphy/releases/download/v0.2.0/Engraphy-Desktop-Setup-0.2.0-win-x64.exe',
							size: 82347606,
							signed: false,
							platform: 'win32',
							arch: 'x64',
						},
					],
				},
				desktop || {}
			),
			'vscode-extension': { latest: '0.5.2', minimumSupported: '0.5.0', notes: null, downloads: [] },
			engine: { latest: '0.2.0', minimumSupported: '0.1.0', notes: null, downloads: [] },
		},
	};
}

check('versionCheck: the desktop version is read from its own key', () => {
	// The release is tagged v0.2.0 because that is the ENGINE version. This app
	// is 0.1.0, and reading the tag would tell a current install to update to a
	// version that is not its own.
	const doc = versionManifest({ latest: '0.1.0' });
	assert.strictEqual(vc.evaluate('0.1.0', vc.parseManifest(doc, 'desktop')).state, 'current');
	assert.strictEqual(vc.parseManifest(doc, 'engine').latest, '0.2.0');
});

check('versionCheck: states, including a build ahead of the release', () => {
	const p = vc.parseManifest(versionManifest(), 'desktop');
	assert.strictEqual(vc.evaluate('0.1.0', p).state, 'update');
	assert.strictEqual(vc.evaluate('0.2.0', p).state, 'current');
	// A local build during a release cycle must never read as out of date.
	assert.strictEqual(vc.evaluate('0.3.0', p).state, 'ahead');
	assert.strictEqual(vc.evaluate('0.0.9', p).state, 'unsupported');
	assert.strictEqual(vc.evaluate('0.1.0', null).state, 'unknown');
});

check('updateModel: nothing is shown unless a newer version is published', () => {
	const p = vc.parseManifest(versionManifest(), 'desktop');
	for (const running of ['0.2.0', '0.3.0']) {
		const vm = um.buildUpdateBanner(vc.evaluate(running, p), 'installer', 'win32', 'x64');
		assert.strictEqual(vm.visible, false, running + ' must show no banner');
	}
	assert.strictEqual(
		um.buildUpdateBanner(vc.evaluate('0.1.0', null), 'installer', 'win32', 'x64').visible,
		false
	);
});

check('updateModel: an installer build offers the download and names both versions', () => {
	const p = vc.parseManifest(versionManifest(), 'desktop');
	const vm = um.buildUpdateBanner(vc.evaluate('0.1.0', p), 'installer', 'win32', 'x64');
	assert.strictEqual(vm.visible, true);
	assert.strictEqual(vm.tone, 'info');
	assert.ok(vm.text.includes('0.2.0'), 'the published version');
	assert.ok(vm.text.includes('0.1.0'), 'the running version');
	assert.ok(vm.action.url.endsWith('.exe'));
	assert.ok(vm.notes.url.startsWith('https://'));
	// The size and the confirmation prompt are both stated before the click.
	assert.ok(vm.detail.includes('79 MB'), vm.detail);
	assert.ok(vm.detail.includes('More info'), vm.detail);
});

check('updateModel: a Microsoft Store install is never handed an installer', () => {
	// An .exe here installs a second copy of Engraphy beside the one the Store
	// manages, and the two then update independently.
	const p = vc.parseManifest(versionManifest(), 'desktop');
	const vm = um.buildUpdateBanner(vc.evaluate('0.1.0', p), 'microsoft-store', 'win32', 'x64');
	assert.strictEqual(vm.visible, true);
	assert.strictEqual(vm.action, null, 'no download button on the Store channel');
	assert.ok(vm.detail.includes('Microsoft Store'));
	assert.ok(vm.notes, 'the release notes are still reachable');
});

check('updateModel: below the supported floor reads as a warning', () => {
	const p = vc.parseManifest(versionManifest(), 'desktop');
	const vm = um.buildUpdateBanner(vc.evaluate('0.0.9', p), 'installer', 'win32', 'x64');
	assert.strictEqual(vm.visible, true);
	assert.strictEqual(vm.tone, 'warn');
	assert.ok(vm.text.includes('oldest supported'));
});

check('updateModel: no build for this machine offers the notes, not a dead button', () => {
	const p = vc.parseManifest(versionManifest(), 'desktop');
	const vm = um.buildUpdateBanner(vc.evaluate('0.1.0', p), 'installer', 'darwin', 'arm64');
	assert.strictEqual(vm.visible, true);
	assert.strictEqual(vm.action, null, 'a Windows installer is not a macOS download');
	assert.ok(vm.notes, 'the release is still reachable');
});

check('updateModel: a signed installer drops the confirmation line', () => {
	// The line exists to prepare the user for what Windows shows next. A signed
	// artifact does not need it, and the manifest is what says which this is.
	const doc = versionManifest({
		downloads: [
			{
				kind: 'nsis',
				url: 'https://github.com/devon-clarkk/engraphy/releases/download/v0.2.0/signed.exe',
				size: 1048576,
				signed: true,
				platform: 'win32',
				arch: 'x64',
			},
		],
	});
	const vm = um.buildUpdateBanner(
		vc.evaluate('0.1.0', vc.parseManifest(doc, 'desktop')),
		'installer',
		'win32',
		'x64'
	);
	assert.ok(!vm.detail.includes('More info'), vm.detail);
	assert.ok(vm.detail.includes('1 MB'), vm.detail);
});

check('updateModel: detectChannel reads the running process', () => {
	assert.strictEqual(um.detectChannel({ windowsStore: true }), 'microsoft-store');
	assert.strictEqual(um.detectChannel({}), 'installer');
	assert.strictEqual(um.detectChannel({ windowsStore: false }), 'installer');
});

check('parseUpdateCommand: only the three banner commands are accepted', () => {
	for (const t of ['ready', 'check', 'dismiss']) {
		assert.deepStrictEqual(m.parseUpdateCommand({ type: t }), { type: t });
	}
	// Turning the check off is a persisted preference and goes through the
	// settings channel, so there is one path to it rather than two.
	assert.strictEqual(m.parseUpdateCommand({ type: 'setEnabled', enabled: false }), null);
	assert.strictEqual(m.parseUpdateCommand({ type: 'nonsense' }), null);
	assert.strictEqual(m.parseUpdateCommand(null), null);
	assert.strictEqual(m.parseUpdateCommand('dismiss'), null);
});

async function versionCheckAsyncChecks() {
	// Update checking, over an injected fetch: offline, a 404 and a body that is
	// not JSON must all read as "no answer". An update check that surfaced its
	// own failures would be noise on every flight and every train.
	const okFetch = async () => ({
		ok: true,
		status: 200,
		json: async () => ({
			schema: 1,
			products: {
				desktop: { latest: '0.2.0', minimumSupported: '0.1.0', notes: null, downloads: [] },
			},
		}),
	});
	let v = await vc.checkForUpdate('0.1.0', vc.DEFAULT_MANIFEST_URL, okFetch, 'desktop');
	assert.strictEqual(v.state, 'update');
	assert.strictEqual(v.latest, '0.2.0');

	const failures = [
		['a 404', async () => ({ ok: false, status: 404, json: async () => ({}) })],
		[
			'being offline',
			async () => {
				throw new Error('getaddrinfo ENOTFOUND engraphy.tech');
			},
		],
		[
			'a body that is not JSON',
			async () => ({
				ok: true,
				status: 200,
				json: async () => {
					throw new Error('Unexpected token <');
				},
			}),
		],
	];
	for (const [name, fetcher] of failures) {
		v = await vc.checkForUpdate('0.1.0', vc.DEFAULT_MANIFEST_URL, fetcher, 'desktop');
		assert.strictEqual(v.state, 'unknown', name + ' must read as no answer');
		assert.strictEqual(
			um.buildUpdateBanner(v, 'installer', 'win32', 'x64').visible,
			false,
			name + ' must leave the banner hidden'
		);
	}

	// The URL is the bare static path: nothing about this install rides along,
	// so the check is not version telemetry.
	let seen = null;
	await vc.checkForUpdate(
		'0.1.0',
		vc.DEFAULT_MANIFEST_URL,
		async (url) => {
			seen = url;
			return okFetch();
		},
		'desktop'
	);
	assert.strictEqual(seen, vc.DEFAULT_MANIFEST_URL);
	assert.ok(!seen.includes('?'), 'no query string');
	assert.ok(!seen.includes('0.1.0'), 'the running version never appears in the URL');

	passed += 3;
	console.log('  ok - checkForUpdate: a good manifest yields the update verdict');
	console.log('  ok - checkForUpdate: offline, 404 and malformed all leave the banner hidden');
	console.log('  ok - checkForUpdate: the request carries no version telemetry');
}


// ---------------------------------------------------------------------------
// bootstrap.ts -- the Windows installer handoff
// ---------------------------------------------------------------------------
//
// The installer mints a token before this app is ever opened. These checks are
// about what the app agrees to accept from a file sitting in the user's
// profile, which is a smaller set than "valid JSON": a handoff decides where a
// bearer token gets sent, so anything that is not an unambiguous local server
// is refused rather than interpreted.

check('parseBootstrap: a well-formed local handoff is accepted', () => {
	const b = bs.parseBootstrap({
		schema: 1,
		serverUrl: 'http://127.0.0.1:8000/mcp/',
		space: 'personal',
		token: 'eng_live_abc',
	});
	assert.deepStrictEqual(b, {
		serverUrl: 'http://127.0.0.1:8000/mcp/',
		space: 'personal',
		token: 'eng_live_abc',
	});
});

check('parseBootstrap: localhost and ::1 are the same machine', () => {
	for (const url of ['http://localhost:8000/mcp/', 'http://[::1]:8000/mcp/', 'https://127.0.0.1:8443/mcp/']) {
		assert.ok(bs.parseBootstrap({ schema: 1, serverUrl: url, space: 's', token: 't' }),
			url + ' must be accepted');
	}
});

check('parseBootstrap: a handoff naming a remote host is refused', () => {
	// The threat this closes: a file dropped into the user's profile that aims
	// their memory, and the bearer token with it, at someone else's server.
	// Nothing legitimate writes a non-loopback URL here, so the shape is
	// narrowed to what the feature needs rather than validated case by case.
	for (const url of [
		'http://evil.example.com/mcp/',
		'http://127.0.0.1.evil.example.com/mcp/',
		'http://user@evil.example.com/mcp/',
		'file:///C:/Windows/system32',
		'not a url',
	]) {
		assert.strictEqual(bs.parseBootstrap({ schema: 1, serverUrl: url, space: 's', token: 't' }), null,
			url + ' must be refused');
	}
});

check('parseBootstrap: an unknown schema is a document for a different client', () => {
	const good = { serverUrl: 'http://127.0.0.1:8000/mcp/', space: 's', token: 't' };
	assert.strictEqual(bs.parseBootstrap({ ...good, schema: 2 }), null);
	assert.strictEqual(bs.parseBootstrap({ ...good }), null);
	assert.strictEqual(bs.parseBootstrap(null), null);
	assert.strictEqual(bs.parseBootstrap('{}'), null);
});

check('parseBootstrap: a missing or blank field is not a handoff', () => {
	const good = { schema: 1, serverUrl: 'http://127.0.0.1:8000/mcp/', space: 's', token: 't' };
	for (const key of ['serverUrl', 'space', 'token']) {
		assert.strictEqual(bs.parseBootstrap({ ...good, [key]: '' }), null, key + ' blank');
		assert.strictEqual(bs.parseBootstrap({ ...good, [key]: '   ' }), null, key + ' whitespace');
		assert.strictEqual(bs.parseBootstrap({ ...good, [key]: 42 }), null, key + ' non-string');
		const without = { ...good };
		delete without[key];
		assert.strictEqual(bs.parseBootstrap(without), null, key + ' absent');
	}
});

check('shouldImport: only onto an app with no connection of its own', () => {
	// A reinstall or a repair must never replace credentials the user is
	// already using. Holding a token is the signal that they are.
	assert.strictEqual(bs.shouldImport(false, false), true);
	assert.strictEqual(bs.shouldImport(true, false), false);
	assert.strictEqual(bs.shouldImport(false, true), false);
	assert.strictEqual(bs.shouldImport(true, true), false);
});

runAsyncChecks()
	.then(versionCheckAsyncChecks)
	.then(() => {
		console.log(`\n${passed} checks passed, ${failed} failed.`);
		if (failed > 0) {
			process.exit(1);
		}
	});
