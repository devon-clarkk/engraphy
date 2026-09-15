// Dev stub of the Engraphy server — a REAL MCP Streamable HTTP endpoint plus an
// unauthenticated /healthz, so the desktop app can be verified end to end with
// no Docker/Postgres/embedding-model stack running.
//
// It uses the SDK's own server side (low-level Server + StreamableHTTPServer-
// Transport) so the client does a genuine MCP `initialize` handshake and real
// tools/call round-trips — the same code path a live server exercises. The tool
// results are canned envelopes shaped to exactly what the app's pure parsers
// expect (pendingItemsFrom / inboxItemsFrom / parseStatsResult / nodesFromSearch
// / nodesFromTraverse). resolve_duplicate / inbox_review(discard|promote) mutate
// in-memory state so the queue visibly shrinks when you act on it.
//
// NOT production: no auth, no persistence, single mutable process. Run with
// `npm run stub` (listens on 127.0.0.1:8000). Never shipped in the packaged app.

import express, { type Request, type Response } from 'express';
import { randomUUID } from 'node:crypto';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';

const PORT = Number(process.env.STUB_PORT || 8000);
const HOST = '127.0.0.1';

// STUB_EMPTY=1 serves a freshly-provisioned space: reachable and authorized,
// but with no nodes, no pending duplicates, no inbox items and zero counters.
// That is a legitimate state a real user hits on day one, and it is the only
// way to exercise the app's empty states end to end.
const EMPTY = !!process.env.STUB_EMPTY;

// STUB_DELAY_MS holds every tool call for N milliseconds. The loading skeletons
// are otherwise unobservable: against a local stub the first paint completes in
// a few milliseconds, so any snapshot taken after load shows the finished panel
// and the skeleton path is never verified.
const DELAY_MS = Number(process.env.STUB_DELAY_MS || 0);

// ---- in-memory fixture state ----------------------------------------------

interface Node {
	id: string;
	title: string;
	type: string;
	scope: string;
	body: string;
	links: Array<{ rel: string; to: string }>;
}

const ALL_NODES: Node[] = [
	{ id: 'n_ada', title: 'Ada prefers async standups', type: 'preference', scope: 'team', body: 'Ada finds live standups disruptive; prefers a written async thread by 10am.', links: [{ rel: 'about', to: 'n_ada_person' }] },
	{ id: 'n_ada_person', title: 'Ada Lovelace', type: 'person', scope: 'team', body: 'Staff engineer, timezone CET, owns the ingestion pipeline.', links: [{ rel: 'about_of', to: 'n_ada' }] },
	{ id: 'n_ship', title: 'Ship v1 by end of Q3', type: 'commitment', scope: 'team', body: 'Public launch of Engraphy v1 committed for end of Q3.', links: [{ rel: 'depends_on', to: 'n_infra' }] },
	{ id: 'n_infra', title: 'Postgres + pgvector as the store', type: 'note', scope: 'team', body: 'Decision: single Postgres with pgvector for both graph edges and embeddings.', links: [{ rel: 'depended_on_by', to: 'n_ship' }] },
	{ id: 'n_dark', title: 'Devon prefers dark mode tooling', type: 'preference', scope: 'personal', body: 'Default all dev tools to dark themes.', links: [] },
];

const NODES: Node[] = EMPTY ? [] : ALL_NODES;

let PENDING: any[] = EMPTY ? [] : [
	{
		id: 'pend_1',
		payload_preview: 'Ada likes standups done as an async written thread instead of a live call.',
		candidates: [{ id: 'n_ada', title: 'Ada prefers async standups', similarity: 0.91 }],
		expires_at: new Date(Date.now() + 36e5).toISOString(),
		created_at: new Date(Date.now() - 6e5).toISOString(),
	},
	{
		id: 'pend_2',
		payload_preview: 'Store embeddings in pgvector alongside the graph in one Postgres instance.',
		candidates: [
			{ id: 'n_infra', title: 'Postgres + pgvector as the store', similarity: 0.84 },
			{ id: 'n_ship', title: 'Ship v1 by end of Q3', similarity: 0.42 },
		],
		expires_at: new Date(Date.now() + 36e5).toISOString(),
		created_at: new Date(Date.now() - 12e5).toISOString(),
	},
];

let INBOX: any[] = EMPTY ? [] : [
	{ id: 'inb_1', kind: 'observation', scope: 'team', payload: { title: 'Ada mentioned a CET timezone', text: 'Ada said she is based in Berlin (CET) during the sync.' }, created_at: new Date(Date.now() - 3e6).toISOString() },
	{ id: 'inb_2', kind: 'note', scope: null, payload: 'Consider a keyboard shortcut for quick-capture into the inbox.', created_at: new Date(Date.now() - 9e6).toISOString() },
];

function daySeries(days: number, base: Record<string, number>) {
	const out = [];
	for (let i = days - 1; i >= 0; i--) {
		const d = new Date(Date.now() - i * 864e5).toISOString().slice(0, 10);
		const jitter = (n: number) => Math.max(0, Math.round(n * (0.5 + ((i * 7) % 11) / 11)));
		out.push({
			date: d,
			questions_asked: jitter(base.questions_asked),
			answered: jitter(base.answered),
			memory_reused: jitter(base.memory_reused),
			facts_stored: jitter(base.facts_stored),
			duplicates_prevented: jitter(base.duplicates_prevented),
			promotes: jitter(base.promotes),
		});
	}
	return out;
}

function emptyStatsEnvelope(rangeDays: number, groupBy: 'space' | 'user') {
	const zero = { questions_asked: 0, answered: 0, memory_reused: 0, facts_stored: 0, duplicates_prevented: 0, promotes: 0 };
	const series = [];
	for (let i = rangeDays - 1; i >= 0; i--) {
		series.push({ date: new Date(Date.now() - i * 864e5).toISOString().slice(0, 10), ...zero });
	}
	return {
		v: 1,
		space: 'fresh',
		group_by: groupBy,
		principal: null,
		range_days: rangeDays,
		generated_at: new Date().toISOString(),
		totals: zero,
		series,
	};
}

function statsEnvelope(rangeDays: number, groupBy: 'space' | 'user') {
	if (EMPTY) {
		return emptyStatsEnvelope(rangeDays, groupBy);
	}
	const perDay = groupBy === 'user'
		? { questions_asked: 4, answered: 3, memory_reused: 6, facts_stored: 2, duplicates_prevented: 1, promotes: 1 }
		: { questions_asked: 11, answered: 9, memory_reused: 18, facts_stored: 5, duplicates_prevented: 3, promotes: 2 };
	const series = daySeries(rangeDays, perDay);
	const totals = series.reduce(
		(acc, d) => {
			for (const k of Object.keys(perDay)) (acc as any)[k] += (d as any)[k];
			return acc;
		},
		{ questions_asked: 0, answered: 0, memory_reused: 0, facts_stored: 0, duplicates_prevented: 0, promotes: 0 }
	);
	return {
		v: 1,
		space: 'demo',
		group_by: groupBy,
		principal: groupBy === 'user' ? 'devon@example.com' : null,
		range_days: rangeDays,
		generated_at: new Date().toISOString(),
		totals,
		series,
	};
}

// ---- tool dispatch (canned envelopes shaped to the app's parsers) ----------

function callTool(name: string, args: Record<string, unknown>): unknown {
	switch (name) {
		case 'search': {
			const q = String(args.query ?? '').toLowerCase();
			const scope = String(args.scope ?? 'all');
			const hits = NODES.filter(
				(n) =>
					(scope === 'all' || n.scope === scope) &&
					(q === '' || n.title.toLowerCase().includes(q) || n.body.toLowerCase().includes(q))
			);
			return { v: 1, results: hits.map((n) => ({ node: { id: n.id, title: n.title, type: n.type, scope: n.scope }, score: 0.7 })) };
		}
		case 'get': {
			const ids = (args.ids as string[]) ?? [];
			return { v: 1, nodes: NODES.filter((n) => ids.includes(n.id)) };
		}
		case 'traverse': {
			const startId = String(args.start_id ?? '');
			const start = NODES.find((n) => n.id === startId);
			const links = start ? start.links : [];
			const neighborIds = links.map((l) => l.to);
			const nodes = NODES.filter((n) => neighborIds.includes(n.id)).map((n) => ({ id: n.id, title: n.title, type: n.type, scope: n.scope, depth: 1 }));
			const edges = links
				.filter((l) => NODES.some((n) => n.id === l.to))
				.map((l) => ({ src: startId, dst: l.to, type: l.rel }));
			return { v: 1, nodes, edges, truncated: false };
		}
		case 'scope_list':
			return { v: 1, scopes: [{ id: 'team', display_name: 'Team' }, { id: 'personal', display_name: 'Personal' }] };
		case 'stats':
			return statsEnvelope(Number(args.range_days ?? 30), args.group_by === 'user' ? 'user' : 'space');
		case 'pending_list':
			return { v: 1, pending: PENDING };
		case 'inbox_review': {
			const action = String(args.action ?? 'list');
			if (action === 'discard') {
				INBOX = INBOX.filter((i) => i.id !== String(args.id));
				return { v: 1, ok: true };
			}
			if (action === 'promote') {
				INBOX = INBOX.filter((i) => i.id !== String(args.id));
				return { v: 1, outcome: 'written', node_id: 'n_' + randomUUID().slice(0, 6) };
			}
			return { v: 1, items: INBOX, truncated: false };
		}
		case 'resolve_duplicate': {
			const id = String(args.pending_id ?? '');
			const before = PENDING.length;
			PENDING = PENDING.filter((p) => p.id !== id);
			if (PENDING.length === before) {
				return toolError('ENGRAPHY_PENDING_NOT_FOUND', `no pending row ${id}`);
			}
			return { v: 1, ok: true, resolution: args.resolution };
		}
		case 'briefing': {
			// Shaped for graphHarvest's nodesFromBriefing, which is the only reader
			// of this tool in the app: seeds the graph index from every fixture node
			// in the requested scope. A real server's briefing is capped and
			// hint-dependent (see graphHarvest.ts); the stub just returns
			// everything, since the fixture is five nodes total.
			const scope = String(args.scope ?? '');
			const rows = NODES.filter((n) => n.scope === scope).map((n) => ({
				id: n.id,
				title: n.title,
				type: n.type,
				scope: n.scope,
			}));
			return { v: 1, sections: [{ name: 'recent_notes', nodes: rows }] };
		}
		default:
			return toolError('ENGRAPHY_UNKNOWN_TOOL', `stub has no tool ${name}`);
	}
}

const TOOL_NAMES = ['search', 'get', 'traverse', 'scope_list', 'stats', 'pending_list', 'inbox_review', 'resolve_duplicate', 'briefing'];

function toolError(code: string, message: string) {
	return { __isError: true, text: `${code}: ${message}` };
}

// ---- MCP server wiring -----------------------------------------------------

function buildServer(): Server {
	const server = new Server({ name: 'engraphy-stub', version: '0.0.0-stub' }, { capabilities: { tools: {} } });

	server.setRequestHandler(ListToolsRequestSchema, async () => ({
		tools: TOOL_NAMES.map((name) => ({ name, description: `stub ${name}`, inputSchema: { type: 'object' } })),
	}));

	server.setRequestHandler(CallToolRequestSchema, async (req) => {
		if (DELAY_MS > 0) {
			await new Promise((r) => setTimeout(r, DELAY_MS));
		}
		const name = req.params.name;
		const args = (req.params.arguments ?? {}) as Record<string, unknown>;
		const result = callTool(name, args) as any;
		if (result && result.__isError) {
			return { content: [{ type: 'text', text: result.text }], isError: true };
		}
		return { content: [{ type: 'text', text: JSON.stringify(result) }], structuredContent: result };
	});

	return server;
}

// ---- HTTP (Streamable HTTP transport, stateless-per-request) ---------------

const app = express();
app.use(express.json({ limit: '4mb' }));

app.get('/healthz', (_req: Request, res: Response) => {
	res.json({ status: 'ok', version: '0.0.0-stub', schema_version: 'stub', spaces: 1, embedding_model: 'stub-none' });
});

async function handleMcp(req: Request, res: Response): Promise<void> {
	// New transport + server per request (stateless mode: sessionIdGenerator
	// undefined) — simplest correct wiring for a single-user dev stub.
	const server = buildServer();
	const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
	res.on('close', () => {
		void transport.close();
		void server.close();
	});
	await server.connect(transport);
	await transport.handleRequest(req, res, req.body);
}

app.post('/mcp/', handleMcp);
app.post('/mcp', handleMcp);
app.get('/mcp/', handleMcp);
app.get('/mcp', handleMcp);

app.listen(PORT, HOST, () => {
	// eslint-disable-next-line no-console
	console.log(`engraphy stub listening on http://${HOST}:${PORT}  (MCP: /mcp/  health: /healthz)` + (EMPTY ? '  [EMPTY fixtures]' : '') + (DELAY_MS ? '  [delay ' + DELAY_MS + 'ms]' : ''));
});
