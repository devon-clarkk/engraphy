#!/usr/bin/env node
// Headless verification of the Engraphy client's pure logic: result parsing and
// argument building. The live server round-trip can only be exercised in a
// running VS Code; this covers the parts that can be wrong in ways `tsc` won't
// catch. Run: npm run test-client (compiles src/toolResult.ts to out-test first).

'use strict';

const assert = require('assert');
const t = require('../out-test/toolResult.js');
const w = require('../out-test/webviewMessages.js');
const s = require('../out-test/statsModel.js');

let passed = 0;
function check(name, fn) {
	fn();
	passed++;
	console.log(`  ok - ${name}`);
}

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

console.log(`\n${passed} checks passed.`);
