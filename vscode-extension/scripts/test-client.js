#!/usr/bin/env node
// Headless verification of the Engraphy client's pure logic: result parsing and
// argument building. The live server round-trip can only be exercised in a
// running VS Code; this covers the parts that can be wrong in ways `tsc` won't
// catch. Run: npm run test-client (compiles src/toolResult.ts to out-test first).

'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const t = require('../out-test/toolResult.js');
const w = require('../out-test/webviewMessages.js');
const s = require('../out-test/statsModel.js');
const c = require('../out-test/connection.js');
const m = require('../out-test/tokenMigration.js');
const cap = require('../out-test/capability.js');
const ar = require('../out-test/agentRuntimes.js');
const os = require('os');

let passed = 0;
function check(name, fn) {
	fn();
	passed++;
	console.log(`  ok - ${name}`);
}

// ---- shipped asset guards --------------------------------------------------
//
// Every SVG under media/ is painted as a CSS mask so it can follow the theme
// accent. A mask whose image fails to load paints NOTHING and logs nothing, so
// a malformed SVG is invisible in every sense. That shipped once: loop-mark.svg
// named the CSS custom property inside an XML comment, leading hyphens and all,
// and an XML comment may not contain a double hyphen. The file stopped being
// well-formed, Chromium refused it as an image, and the header mark plus every
// large mark in the onboarding block rendered blank in the published 0.4.0 vsix.
//
// The check is deliberately narrow: it catches the trap that actually bit,
// without pulling an XML parser into the test run.
function svgFiles(dir) {
	const out = [];
	for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
		const full = path.join(dir, entry.name);
		if (entry.isDirectory()) {
			out.push(...svgFiles(full));
		} else if (entry.name.toLowerCase().endsWith('.svg')) {
			out.push(full);
		}
	}
	return out;
}

check('every shipped SVG is well-formed (no "--" inside an XML comment)', () => {
	const files = svgFiles(path.join(__dirname, '..', 'media'));
	assert.ok(files.length > 0, 'expected at least one SVG under media/');
	for (const file of files) {
		const svg = fs.readFileSync(file, 'utf8');
		assert.ok(/<svg[\s>]/.test(svg), `not an SVG: ${file}`);
		for (const comment of svg.match(/<!--[\s\S]*?-->/g) || []) {
			assert.ok(
				!comment.slice(4, -3).includes('--'),
				`Malformed XML comment in ${path.relative(process.cwd(), file)}: an XML comment ` +
					'may not contain "--", so this file will not parse as an image and any CSS mask ' +
					`using it paints invisible. Offending comment:
${comment.trim()}`
			);
		}
	}
});

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

// ---- connection: error classification (src/connection.ts) ----
//
// The bug these pin down: 0.4.0 folded a 401 into "unreachable", so a HEALTHY
// server that rejected your token told you to go start a server. The exact
// string below is what the MCP SDK surfaces for a real 401 against Engraphy
// 0.1.0 — note it carries no status digits, which is why a naive /401/ test
// misses it.
const SDK_401 = 'Streamable HTTP error: Error POSTing to endpoint: unauthorized';

check('describeError: a real SDK 401 classifies as auth, not transport', () => {
	const d = c.describeError(new Error(SDK_401), '127.0.0.1:8000');
	assert.strictEqual(d.class, 'auth');
	assert.match(d.summary, /rejected this token/i);
	// The old predicate matched the same string, but with no way to say WHY.
	assert.ok(w.isConnectionError(SDK_401));
});
check('describeError: ECONNREFUSED on the cause chain is transport, with a host-specific remedy', () => {
	const e = new Error('fetch failed');
	e.cause = Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:8000'), { code: 'ECONNREFUSED' });
	const d = c.describeError(e, '127.0.0.1:8000');
	assert.strictEqual(d.class, 'transport');
	assert.strictEqual(d.code, 'ECONNREFUSED');
	assert.match(d.summary, /Nothing is listening at 127\.0\.0\.1:8000/);
	// The bare message is useless on its own, so the cause is folded into detail.
	assert.match(d.detail, /ECONNREFUSED/);
});
check('describeError: auth wins over transport words in the same blob', () => {
	// A 401 body can carry words the transport regex would claim. "Your token was
	// rejected" is always the more actionable reading, so auth is tested first.
	const d = c.describeError(new Error('unauthorized: network error while validating'), 'h');
	assert.strictEqual(d.class, 'auth');
});
check('describeError: an ordinary tool error stays a tool error', () => {
	const d = c.describeError(new Error('ENGRAPHY_VALIDATION: bad scope'), 'h');
	assert.strictEqual(d.class, 'tool');
});
check('describeError: TLS failures are transport, named by code', () => {
	const d = c.describeError(Object.assign(new Error('x'), { code: 'CERT_HAS_EXPIRED' }), 'h');
	assert.strictEqual(d.class, 'transport');
	assert.match(d.summary, /TLS certificate/);
});
check('hostLabel: host:port from a URL, raw string when unparseable', () => {
	assert.strictEqual(c.hostLabel('http://127.0.0.1:8000/mcp/'), '127.0.0.1:8000');
	assert.strictEqual(c.hostLabel('not a url'), 'not a url');
	assert.strictEqual(c.hostLabel(''), '');
});

// ---- connection: three-way panel state ----
check('computeConnection: no URL maps to unconfigured', () => {
	assert.strictEqual(c.computeConnection({ serverConfigured: false, errors: [] }).reason, 'unconfigured');
});
check('computeConnection: every band 401 is unauthorized, NOT unreachable', () => {
	const vm = c.computeConnection({
		serverConfigured: true,
		errors: [new Error(SDK_401), new Error(SDK_401)],
		host: '127.0.0.1:8000',
		hasToken: true,
	});
	assert.strictEqual(vm.reason, 'unauthorized');
	assert.strictEqual(vm.hasToken, true);
});
check('computeConnection: unauthorized with NO token says "requires a token", not "rejected"', () => {
	const vm = c.computeConnection({
		serverConfigured: true,
		errors: [new Error(SDK_401)],
		host: '127.0.0.1:8000',
		hasToken: false,
	});
	assert.strictEqual(vm.reason, 'unauthorized');
	assert.match(vm.summary, /requires a token/i);
	assert.doesNotMatch(vm.summary, /rejected/i);
});
check('computeConnection: every band transport-failed is unreachable', () => {
	const e = Object.assign(new Error('fetch failed'), { code: 'ECONNREFUSED' });
	const vm = c.computeConnection({ serverConfigured: true, errors: [e, e], host: 'h' });
	assert.strictEqual(vm.reason, 'unreachable');
});
check('computeConnection: one band ok returns null (render bands, error inline)', () => {
	assert.strictEqual(
		c.computeConnection({ serverConfigured: true, errors: [null, new Error(SDK_401)], host: 'h' }),
		null
	);
	assert.strictEqual(c.computeConnection({ serverConfigured: true, errors: [null, null] }), null);
});
check('computeConnection: mixed auth + transport returns null (no single remedy)', () => {
	const vm = c.computeConnection({
		serverConfigured: true,
		errors: [new Error(SDK_401), Object.assign(new Error('fetch failed'), { code: 'ECONNREFUSED' })],
		host: 'h',
	});
	assert.strictEqual(vm, null);
});
check('computeConnection: plain tool errors are not a connection problem', () => {
	const e = new Error('ENGRAPHY_VALIDATION: bad scope');
	assert.strictEqual(c.computeConnection({ serverConfigured: true, errors: [e, e], host: 'h' }), null);
});

// ---- connection: the status bar cannot go green off /healthz alone ----
check('buildHealthVM: healthz 200 but auth FAILED is never "connected"', () => {
	// The shipped 0.4.0 bug in one assertion: /healthz is unauthenticated, so it
	// answers 200 for a server you cannot read a single byte from.
	const vm = c.buildHealthVM({
		configured: true,
		hasToken: true,
		space: '',
		serverUrl: 'http://127.0.0.1:8000/mcp/',
		info: { status: 'ok', version: '0.1.0', spaces: 4 },
		authOk: false,
		authError: new Error(SDK_401),
	});
	assert.strictEqual(vm.phase, 'unauthorized');
	assert.strictEqual(vm.usable, false);
	assert.match(vm.label, /token rejected/i);
});
check('buildHealthVM: authenticated probe OK gives connected, with healthz detail', () => {
	const vm = c.buildHealthVM({
		configured: true,
		hasToken: true,
		space: 'demo',
		serverUrl: 'http://127.0.0.1:8000/mcp/',
		info: { status: 'ok', version: '0.1.0', spaces: 4 },
		authOk: true,
	});
	assert.strictEqual(vm.phase, 'connected');
	assert.strictEqual(vm.usable, true);
	assert.match(vm.label, /demo/);
	assert.match(vm.title, /server v0\.1\.0/);
	assert.match(vm.title, /4 space/);
});
check('buildHealthVM: no token set gives "token needed", and never says restart', () => {
	const vm = c.buildHealthVM({
		configured: true,
		hasToken: false,
		space: '',
		serverUrl: 'http://127.0.0.1:8000/mcp/',
		info: { status: 'ok' },
		authOk: false,
		authError: new Error(SDK_401),
	});
	assert.strictEqual(vm.phase, 'unauthorized');
	assert.match(vm.label, /token needed/i);
	assert.match(vm.title, /requires a token/i);
});
check('buildHealthVM: nothing listening is unreachable, usable false', () => {
	const e = Object.assign(new Error('fetch failed'), { code: 'ECONNREFUSED' });
	const vm = c.buildHealthVM({
		configured: true,
		hasToken: true,
		space: '',
		serverUrl: 'http://127.0.0.1:8000/mcp/',
		info: null,
		healthzError: e,
		authOk: false,
		authError: e,
	});
	assert.strictEqual(vm.phase, 'unreachable');
	assert.strictEqual(vm.usable, false);
});
check('buildHealthVM: no URL configured is unconfigured, usable false', () => {
	const vm = c.buildHealthVM({
		configured: false,
		hasToken: false,
		space: '',
		serverUrl: '',
		info: null,
		authOk: false,
	});
	assert.strictEqual(vm.phase, 'unconfigured');
	assert.strictEqual(vm.usable, false);
});
check('buildHealthVM: only "connected" is ever usable', () => {
	const cases = [
		c.buildHealthVM({ configured: false, hasToken: false, space: '', serverUrl: '', info: null, authOk: false }),
		c.buildHealthVM({ configured: true, hasToken: true, space: '', serverUrl: 'http://h:1/mcp/', info: null, authOk: false, authError: new Error(SDK_401) }),
		c.buildHealthVM({ configured: true, hasToken: true, space: '', serverUrl: 'http://h:1/mcp/', info: null, authOk: false, authError: Object.assign(new Error('fetch failed'), { code: 'ECONNREFUSED' }) }),
		c.buildHealthVM({ configured: true, hasToken: true, space: '', serverUrl: 'http://h:1/mcp/', info: null, authOk: false, authError: new Error('ENGRAPHY_INTERNAL: boom') }),
	];
	assert.deepStrictEqual(
		cases.map((v) => v.phase),
		['unconfigured', 'unauthorized', 'unreachable', 'degraded']
	);
	for (const vm of cases) {
		assert.strictEqual(vm.usable, false, `${vm.phase} must not be usable`);
	}
});

// ---- the recovery block's new message type ----
check('parseWebviewMessage / parseStatsMessage accept reconnect', () => {
	assert.deepStrictEqual(w.parseWebviewMessage({ type: 'reconnect' }), { type: 'reconnect' });
	assert.deepStrictEqual(s.parseStatsMessage({ type: 'reconnect' }), { type: 'reconnect' });
});

// ---- no private-repo naming leaks into shipped source or assets ----
check('no ENGRAM naming in src/ or media/', () => {
	const roots = [path.join(__dirname, '..', 'src'), path.join(__dirname, '..', 'media')];
	const offenders = [];
	const walk = (dir) => {
		for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
			const full = path.join(dir, entry.name);
			if (entry.isDirectory()) {
				walk(full);
			} else if (/\.(ts|js|css|svg|md)$/i.test(entry.name)) {
				// "Engraphy" does not contain "engram", so this only catches the real thing.
				if (/engram/i.test(fs.readFileSync(full, 'utf8'))) {
					offenders.push(path.relative(process.cwd(), full));
				}
			}
		}
	};
	roots.forEach(walk);
	assert.deepStrictEqual(offenders, [], `ENGRAM naming leaked into: ${offenders.join(', ')}`);
});

// ---- token migration planning (src/tokenMigration.ts) ----
//
// This is the one sequence in the extension where a mistake destroys a
// credential the user may not be able to re-mint. Two ways to get it wrong:
// pick the value VS Code would NOT have resolved (so a working token is
// replaced by a stale one from a broader scope), or miss a scope that holds a
// copy (so the plaintext token survives the migration that claimed to remove
// it). Both are covered below.
check('planTokenMigration: nothing set is a no-op', () => {
	assert.deepStrictEqual(m.planTokenMigration(undefined), { value: '', clear: [] });
	assert.deepStrictEqual(m.planTokenMigration({}), { value: '', clear: [] });
});
check('planTokenMigration: blank and whitespace-only values count as absent', () => {
	assert.deepStrictEqual(m.planTokenMigration({ globalValue: '' }), { value: '', clear: [] });
	assert.deepStrictEqual(m.planTokenMigration({ globalValue: '   \t ' }), { value: '', clear: [] });
});
check('planTokenMigration: a global-only token migrates and clears global', () => {
	assert.deepStrictEqual(m.planTokenMigration({ globalValue: 'tok-g' }), {
		value: 'tok-g',
		clear: ['global'],
	});
});
check('planTokenMigration: most specific scope wins, ALL holders are cleared', () => {
	// VS Code resolves workspaceFolder over workspace over global, so that is the
	// token actually in use. Clearing only the one we took would leave the other
	// two sitting in plain text.
	assert.deepStrictEqual(
		m.planTokenMigration({ globalValue: 'g', workspaceValue: 'w', workspaceFolderValue: 'f' }),
		{ value: 'f', clear: ['global', 'workspace', 'workspaceFolder'] }
	);
	assert.deepStrictEqual(m.planTokenMigration({ globalValue: 'g', workspaceValue: 'w' }), {
		value: 'w',
		clear: ['global', 'workspace'],
	});
});
check('planTokenMigration: a blank narrow scope does not shadow a real broad one', () => {
	// An empty workspace override must not win over a working global token, and
	// must not earn a pointless settings write of its own.
	assert.deepStrictEqual(m.planTokenMigration({ globalValue: 'tok-g', workspaceValue: '  ' }), {
		value: 'tok-g',
		clear: ['global'],
	});
});
check('planTokenMigration: the migrated value is trimmed', () => {
	assert.strictEqual(m.planTokenMigration({ globalValue: '  tok  ' }).value, 'tok');
});
check('planTokenMigration: a real inspect() shape is accepted as-is', () => {
	// What vscode actually hands back carries extra keys; extras must not confuse
	// the planner or trip the "nothing set" branch.
	const inspectLike = {
		key: 'engraphy.token',
		defaultValue: '',
		globalValue: 'tok-g',
		workspaceValue: undefined,
		workspaceFolderValue: undefined,
		languageIds: [],
	};
	assert.deepStrictEqual(m.planTokenMigration(inspectLike), { value: 'tok-g', clear: ['global'] });
});

// ---- capability model ------------------------------------------------------
//
// These cover the exact state that caused silent data loss: a reachable server,
// an extension that can read it, and no agent anywhere holding the tools.

check('vsCodeRegistryLive: registering is not consumption', () => {
	// The provider registered and VS Code never asked. This is the normal state
	// in an editor with no Copilot Chat traffic, and it used to be invisible.
	assert.strictEqual(cap.vsCodeRegistryLive('available', true, {}), false);
	assert.strictEqual(cap.vsCodeRegistryLive('available', true, { calls: 0 }), false);
});
check('vsCodeRegistryLive: being asked and returning nothing is not consumption', () => {
	// The empty-URL / malformed-URL path. VS Code asked, Engraphy withheld.
	assert.strictEqual(cap.vsCodeRegistryLive('available', true, { calls: 3, lastCount: 0 }), false);
});
check('vsCodeRegistryLive: asked and answered is consumption', () => {
	assert.strictEqual(cap.vsCodeRegistryLive('available', true, { calls: 1, lastCount: 1 }), true);
});
check('vsCodeRegistryLive: an absent API can never be live', () => {
	assert.strictEqual(cap.vsCodeRegistryLive('missing', true, { calls: 9, lastCount: 1 }), false);
	assert.strictEqual(cap.vsCodeRegistryLive('available', false, { calls: 9, lastCount: 1 }), false);
});

const REACHABLE = {
	reach: 'reachable',
	host: '127.0.0.1:8000',
	api: 'available',
	providerRegistered: true,
	signal: {},
	runtimes: [],
};

check('capability: a reachable server with no agent path is NOT connected', () => {
	// THE INCIDENT. Server up, extension reads it fine, nothing consumed the
	// registry, no third-party runtime registered. The old bar said "connected".
	const vm = cap.buildCapabilityVM(REACHABLE);
	assert.strictEqual(vm.phase, 'no-agent-path');
	assert.strictEqual(vm.usable, false);
	assert.match(vm.label, /agent cannot see memory/);
	assert.strictEqual(vm.action.command, cap.REGISTER_COMMAND);
	// The explanation must name the real reason, not a generic failure.
	assert.match(vm.detail, /VS Code has never asked for it/);
});

check('capability: a detected third-party runtime is named in the gap detail', () => {
	const vm = cap.buildCapabilityVM({
		...REACHABLE,
		runtimes: [{ id: 'claude-code', label: 'Claude Code', detected: true, registered: false }],
	});
	assert.strictEqual(vm.phase, 'no-agent-path');
	assert.match(vm.detail, /Claude Code/);
	assert.match(vm.detail, /do not use the VS Code registry/);
});

check('capability: a registered third-party runtime IS ready, with no Copilot at all', () => {
	// Devon's working machine: Claude Code holds the tools via ~/.claude.json,
	// and VS Code has never consumed the provider. That is genuinely ready.
	const vm = cap.buildCapabilityVM({
		...REACHABLE,
		runtimes: [{ id: 'claude-code', label: 'Claude Code', detected: true, registered: true }],
	});
	assert.strictEqual(vm.phase, 'ready');
	assert.strictEqual(vm.usable, true);
	assert.match(vm.title, /Claude Code/);
});

check('capability: a consumed VS Code registry is ready on its own', () => {
	const vm = cap.buildCapabilityVM({ ...REACHABLE, signal: { calls: 2, lastCount: 1 } });
	assert.strictEqual(vm.phase, 'ready');
	assert.strictEqual(vm.usable, true);
});

check('capability: an old VS Code says so instead of failing silently', () => {
	const vm = cap.buildCapabilityVM({ ...REACHABLE, api: 'missing', providerRegistered: false });
	assert.strictEqual(vm.phase, 'no-agent-path');
	assert.match(vm.detail, /VS Code 1\.101 or newer/);
});

check('capability: a dead server outranks the agent question', () => {
	// Registering an agent against a server that is not answering fixes nothing,
	// so the server problem is the one reported.
	const vm = cap.buildCapabilityVM({
		...REACHABLE,
		reach: 'unauthorized',
		runtimes: [{ id: 'claude-code', label: 'Claude Code', detected: true, registered: true }],
	});
	assert.strictEqual(vm.phase, 'server-unavailable');
	assert.strictEqual(vm.usable, false);
});

check('capability: no server URL asks for one', () => {
	const vm = cap.buildCapabilityVM({ ...REACHABLE, reach: 'unconfigured' });
	assert.strictEqual(vm.phase, 'unconfigured');
	assert.strictEqual(vm.action.command, cap.CONNECT_COMMAND);
});

check('capability: a consumed VS Code registry does NOT excuse an unregistered agent', () => {
	// THE REGRESSION GUARD. "At least one path is live" was the first rule here,
	// and it re-created the original bug: the VS Code signal is sticky across
	// sessions, so on any machine where Copilot Chat had ever submitted a
	// message the VS Code entry counted registered forever, and an unregistered
	// Claude Code sat beside it under a green bar.
	const vm = cap.buildCapabilityVM({
		...REACHABLE,
		signal: { calls: 1, lastCount: 1 },
		runtimes: [{ id: 'claude-code', label: 'Claude Code', detected: true, registered: false }],
	});
	assert.strictEqual(vm.phase, 'partial');
	assert.match(vm.label, /Claude Code cannot see memory/);
	assert.strictEqual(vm.action.command, cap.REGISTER_COMMAND);
});

check('capability: an UNDETECTED runtime is not a gap', () => {
	// Not having Cursor installed is not a broken setup.
	const vm = cap.buildCapabilityVM({
		...REACHABLE,
		signal: { calls: 1, lastCount: 1 },
		runtimes: [{ id: 'cursor', label: 'Cursor', detected: false, registered: false }],
	});
	assert.strictEqual(vm.phase, 'ready');
});

check('capability: a dismissed runtime is not a gap', () => {
	// Detection is a heuristic: a leftover ~/.cursor means Cursor was installed
	// once, not that anyone runs it. Dismissing must actually silence it.
	const vm = cap.buildCapabilityVM({
		...REACHABLE,
		signal: { calls: 1, lastCount: 1 },
		runtimes: [{ id: 'cursor', label: 'Cursor', detected: true, registered: false }],
		ignored: ['cursor'],
	});
	assert.strictEqual(vm.phase, 'ready');
});

check('capability: an unconsumed VS Code registry is never itself a gap', () => {
	// An editor with no Copilot Chat traffic is not broken, it is simply not
	// that path. Only a real third-party runtime can raise `partial`.
	const vm = cap.buildCapabilityVM({
		...REACHABLE,
		signal: {},
		runtimes: [{ id: 'claude-code', label: 'Claude Code', detected: true, registered: true }],
	});
	assert.strictEqual(vm.phase, 'ready');
});

check('capability: several gaps are counted, not listed, in the label', () => {
	const vm = cap.buildCapabilityVM({
		...REACHABLE,
		signal: { calls: 1, lastCount: 1 },
		runtimes: [
			{ id: 'claude-code', label: 'Claude Code', detected: true, registered: false },
			{ id: 'cursor', label: 'Cursor', detected: true, registered: false },
		],
	});
	assert.strictEqual(vm.phase, 'partial');
	assert.match(vm.label, /2 agents cannot see memory/);
	assert.match(vm.title, /Claude Code, Cursor/);
});

check('capability: partial stays usable, because memory IS reachable', () => {
	// The label carries the warning. Claiming memory is unusable when one agent
	// can reach it would be its own false report.
	const vm = cap.buildCapabilityVM({
		...REACHABLE,
		signal: { calls: 1, lastCount: 1 },
		runtimes: [{ id: 'cursor', label: 'Cursor', detected: true, registered: false }],
	});
	assert.strictEqual(vm.usable, true);
	assert.strictEqual(vm.phase, 'partial');
});

check('capability: gaps with NO live path stay no-agent-path, not partial', () => {
	const vm = cap.buildCapabilityVM({
		...REACHABLE,
		runtimes: [{ id: 'claude-code', label: 'Claude Code', detected: true, registered: false }],
	});
	assert.strictEqual(vm.phase, 'no-agent-path');
	assert.strictEqual(vm.usable, false);
});

// ---- write freshness -------------------------------------------------------

check('writeFreshness: an empty series says plainly that nothing was written', () => {
	const f = cap.buildWriteFreshness([], '2026-08-30');
	assert.strictEqual(f.lastWriteDate, undefined);
	assert.match(f.summary, /No memory has been written/);
});
check('writeFreshness: zero-filled days are not writes', () => {
	// The stats series zero-fills every day in range, so "has rows" is not
	// "has writes". A phantom save leaves exactly this shape behind.
	const f = cap.buildWriteFreshness(
		[
			{ date: '2026-08-29', facts_stored: 0, duplicates_prevented: 0, promotes: 0 },
			{ date: '2026-08-30', facts_stored: 0, duplicates_prevented: 0, promotes: 0 },
		],
		'2026-08-30'
	);
	assert.strictEqual(f.rangeTotal, 0);
	assert.match(f.summary, /has not reached this server/);
});
check('writeFreshness: counts every write-shaped outcome, not just inserts', () => {
	// A `needs_confirmation` park counts under duplicates_prevented and is real
	// server traffic, so it must move the freshness line even though no node
	// was inserted yet.
	const f = cap.buildWriteFreshness(
		[
			{ date: '2026-08-28', facts_stored: 2, duplicates_prevented: 0, promotes: 0 },
			{ date: '2026-08-30', facts_stored: 0, duplicates_prevented: 1, promotes: 1 },
		],
		'2026-08-30'
	);
	assert.strictEqual(f.lastWriteDate, '2026-08-30');
	assert.strictEqual(f.lastWriteCount, 2);
	assert.strictEqual(f.rangeTotal, 4);
	assert.match(f.summary, /today/);
});
check('writeFreshness: an out-of-order series still finds the latest day', () => {
	const f = cap.buildWriteFreshness(
		[
			{ date: '2026-08-30', facts_stored: 1, duplicates_prevented: 0, promotes: 0 },
			{ date: '2026-08-20', facts_stored: 5, duplicates_prevented: 0, promotes: 0 },
		],
		'2026-08-31'
	);
	assert.strictEqual(f.lastWriteDate, '2026-08-30');
	assert.match(f.summary, /on 2026-08-30/);
});

// ---- agent runtime configs -------------------------------------------------

check('stripJsonComments: strips comments but not a URL double slash', () => {
	const r = ar.stripJsonComments('{"url": "http://x/mcp/"} // trailing');
	assert.strictEqual(r.hadComments, true);
	assert.strictEqual(JSON.parse(r.out).url, 'http://x/mcp/');
});
check('stripJsonComments: an escaped quote does not end the string', () => {
	const r = ar.stripJsonComments('{"a": "he said \\" // not a comment"}');
	assert.strictEqual(r.hadComments, false);
	assert.strictEqual(JSON.parse(r.out).a, 'he said " // not a comment');
});
check('stripJsonComments: block comments go too', () => {
	const r = ar.stripJsonComments('{/* hi */"a": 1}');
	assert.strictEqual(r.hadComments, true);
	assert.deepStrictEqual(JSON.parse(r.out), { a: 1 });
});

const claudeSpec = ar.RUNTIMES.find((r) => r.id === 'claude-code');
const cursorSpec = ar.RUNTIMES.find((r) => r.id === 'cursor');

check('buildEntry: matches the shape Claude Code actually stores', () => {
	assert.deepStrictEqual(ar.buildEntry(claudeSpec, 'http://127.0.0.1:8000/mcp/', 'tok'), {
		type: 'http',
		url: 'http://127.0.0.1:8000/mcp/',
		headers: { Authorization: 'Bearer tok' },
	});
});
check('buildEntry: an untyped runtime gets no type discriminator', () => {
	assert.strictEqual(ar.buildEntry(cursorSpec, 'http://x/mcp/', 'tok').type, undefined);
});
check('buildEntry: no token means no Authorization header', () => {
	assert.strictEqual(ar.buildEntry(claudeSpec, 'http://x/mcp/', '').headers, undefined);
});
check('every shipped runtime shape was read off a real config', () => {
	// Both entries below were copied from a populated config on a working
	// machine. An unverified shape writes a file that parses and does nothing,
	// which is the silent failure this whole change exists to end, so no
	// runtime ships on a guess.
	assert.deepStrictEqual(
		ar.RUNTIMES.map((r) => r.id),
		['claude-code', 'cursor']
	);
	assert.strictEqual(claudeSpec.mapKey, 'mcpServers');
	assert.strictEqual(claudeSpec.typed, true);
	assert.strictEqual(cursorSpec.mapKey, 'mcpServers');
	assert.strictEqual(cursorSpec.typed, false);
});

check('mergeEngraphy: preserves every unrelated key and sibling server', () => {
	const before = {
		projects: { a: 1 },
		mcpServers: { other: { url: 'http://other/' } },
	};
	const after = ar.mergeEngraphy(before, claudeSpec, { type: 'http', url: 'http://x/mcp/' });
	assert.deepStrictEqual(after.projects, { a: 1 });
	assert.deepStrictEqual(after.mcpServers.other, { url: 'http://other/' });
	assert.strictEqual(after.mcpServers.engraphy.url, 'http://x/mcp/');
	// The input must not be mutated: the caller still holds it for the backup.
	assert.strictEqual(before.mcpServers.engraphy, undefined);
});
check('mergeEngraphy: a missing or non-object map is created, not crashed on', () => {
	assert.strictEqual(ar.mergeEngraphy(null, claudeSpec, { url: 'u' }).mcpServers.engraphy.url, 'u');
	assert.strictEqual(
		ar.mergeEngraphy({ mcpServers: 'nonsense' }, claudeSpec, { url: 'u' }).mcpServers.engraphy.url,
		'u'
	);
});
check('hasEngraphy / registeredUrl read what mergeEngraphy wrote', () => {
	const merged = ar.mergeEngraphy({}, claudeSpec, { type: 'http', url: 'http://x/mcp/' });
	assert.strictEqual(ar.hasEngraphy(merged, claudeSpec), true);
	assert.strictEqual(ar.registeredUrl(merged, claudeSpec), 'http://x/mcp/');
	assert.strictEqual(ar.hasEngraphy({}, claudeSpec), false);
});

// ---- registration round trip, against a real temp home ---------------------

check('registerRuntime: writes, backs up, and reads back verified', () => {
	const home = fs.mkdtempSync(path.join(os.tmpdir(), 'engraphy-home-'));
	const url = 'http://127.0.0.1:8000/mcp/';
	fs.writeFileSync(path.join(home, '.claude.json'), JSON.stringify({ projects: { keep: 1 } }));

	const out = ar.registerRuntime(claudeSpec, url, 'tok', home);
	assert.strictEqual(out.ok, true);
	assert.ok(fs.existsSync(out.backup), 'a backup is taken before the write');
	assert.strictEqual(ar.verifyRegistration(claudeSpec, url, home), true);

	const written = JSON.parse(fs.readFileSync(out.path, 'utf8'));
	assert.deepStrictEqual(written.projects, { keep: 1 }, 'unrelated config survives');
	assert.strictEqual(written.mcpServers.engraphy.headers.Authorization, 'Bearer tok');
	// No temp file may be left behind: a stray one carries the token.
	assert.strictEqual(fs.existsSync(out.path + '.engraphy-tmp'), false);

	// Re-running is a no-op rather than a second write.
	assert.strictEqual(ar.registerRuntime(claudeSpec, url, 'tok', home).unchanged, true);
	fs.rmSync(home, { recursive: true, force: true });
});

check('registerRuntime: creates a missing config and its directory', () => {
	const home = fs.mkdtempSync(path.join(os.tmpdir(), 'engraphy-home-'));
	const out = ar.registerRuntime(cursorSpec, 'http://x/mcp/', '', home);
	assert.strictEqual(out.ok, true);
	assert.strictEqual(ar.verifyRegistration(cursorSpec, 'http://x/mcp/', home), true);
	fs.rmSync(home, { recursive: true, force: true });
});

check('registerRuntime: a zero-byte config is a starting point, not a failure', () => {
	// A config file that exists but is empty must not read as unparseable, or
	// the button refuses on exactly the machine that needs it.
	const home = fs.mkdtempSync(path.join(os.tmpdir(), 'engraphy-home-'));
	const target = path.join(home, cursorSpec.relPath);
	fs.mkdirSync(path.dirname(target), { recursive: true });
	fs.writeFileSync(target, '');
	const out = ar.registerRuntime(cursorSpec, 'http://x/mcp/', 'tok', home);
	assert.strictEqual(out.ok, true);
	assert.strictEqual(
		JSON.parse(fs.readFileSync(target, 'utf8')).mcpServers.engraphy.url,
		'http://x/mcp/'
	);
	fs.rmSync(home, { recursive: true, force: true });
});

check('registerRuntime: refuses a commented config rather than eating the comments', () => {
	const home = fs.mkdtempSync(path.join(os.tmpdir(), 'engraphy-home-'));
	const target = path.join(home, '.claude.json');
	fs.writeFileSync(target, '// keep me\n{"projects": {}}');
	const out = ar.registerRuntime(claudeSpec, 'http://x/mcp/', 'tok', home);
	assert.strictEqual(out.ok, false);
	assert.match(out.problem, /comments/);
	assert.strictEqual(fs.readFileSync(target, 'utf8'), '// keep me\n{"projects": {}}');
	fs.rmSync(home, { recursive: true, force: true });
});

check('registerRuntime: refuses an unparseable config rather than overwriting it', () => {
	const home = fs.mkdtempSync(path.join(os.tmpdir(), 'engraphy-home-'));
	const target = path.join(home, '.claude.json');
	fs.writeFileSync(target, '{ this is not json');
	const out = ar.registerRuntime(claudeSpec, 'http://x/mcp/', 'tok', home);
	assert.strictEqual(out.ok, false);
	assert.strictEqual(fs.readFileSync(target, 'utf8'), '{ this is not json');
	fs.rmSync(home, { recursive: true, force: true });
});

check('registerRuntime: backups do not overwrite each other', () => {
	// ~/.claude.json is Claude Code's live state file. A backup that the next
	// run destroys is not a backup, so each one is timestamped.
	const home = fs.mkdtempSync(path.join(os.tmpdir(), 'engraphy-home-'));
	fs.writeFileSync(path.join(home, '.claude.json'), JSON.stringify({ projects: {} }));
	const a = ar.registerRuntime(claudeSpec, 'http://one/mcp/', 'tok', home);
	const b = ar.registerRuntime(claudeSpec, 'http://two/mcp/', 'tok', home);
	assert.strictEqual(a.ok, true);
	assert.strictEqual(b.ok, true);
	assert.notStrictEqual(a.backup, b.backup);
	assert.ok(fs.existsSync(a.backup) && fs.existsSync(b.backup));
	// The first backup still holds the pre-Engraphy file.
	assert.strictEqual(JSON.parse(fs.readFileSync(a.backup, 'utf8')).mcpServers, undefined);
	fs.rmSync(home, { recursive: true, force: true });
});

check('verifyRegistration: a stale URL does not count as registered', () => {
	// Registration drift: the entry exists but points somewhere else, so the
	// agent is talking to the wrong server. That is not success.
	const home = fs.mkdtempSync(path.join(os.tmpdir(), 'engraphy-home-'));
	ar.registerRuntime(claudeSpec, 'http://old/mcp/', 'tok', home);
	assert.strictEqual(ar.verifyRegistration(claudeSpec, 'http://new/mcp/', home), false);
	assert.strictEqual(ar.verifyRegistration(claudeSpec, 'http://old/mcp/', home), true);
	fs.rmSync(home, { recursive: true, force: true });
});

console.log(`\n${passed} checks passed.`);
