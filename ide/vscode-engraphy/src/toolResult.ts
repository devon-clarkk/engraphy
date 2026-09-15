// Pure, dependency-free logic for the Engraphy MCP client: parsing a
// CallToolResult into a domain object, and building tool-call argument objects.
// No `vscode` or SDK imports so this is unit-testable with plain Node
// (scripts/test-client.js). The MCP SDK's CallToolResult is structurally
// compatible with RawToolResult (content[], structuredContent?, isError?).

/** The subset of an MCP CallToolResult this client reads. */
export interface RawToolResult {
	content?: Array<{ type: string; text?: string }>;
	structuredContent?: Record<string, unknown> | null;
	isError?: boolean;
}

/** A tool error surfaced by the server (isError:true). `code` is the ENGRAPHY_* prefix when present. */
export class EngraphyToolError extends Error {
	constructor(
		public readonly code: string,
		message: string
	) {
		super(message);
		this.name = 'EngraphyToolError';
	}
}

/** First text content block's text, or undefined. */
export function firstText(res: RawToolResult): string | undefined {
	return res.content?.find((c) => c.type === 'text' && typeof c.text === 'string')?.text;
}

/**
 * Turn a CallToolResult into the tool's payload object.
 *
 * Success: prefer `structuredContent`; else JSON-parse the first text block.
 * Error (`isError:true`): the server's `_error_result` sets the text to an
 * `ENGRAPHY_*`-prefixed string and `structuredContent` to `exc.extra` OR null, so
 * we parse the code out of the text and never assume structuredContent exists.
 */
export function parseToolResult(res: RawToolResult): unknown {
	const text = firstText(res);

	if (res.isError) {
		const msg = text ?? 'unknown Engraphy tool error';
		const code = parseErrorCode(msg);
		throw new EngraphyToolError(code, msg);
	}

	if (res.structuredContent != null && Object.keys(res.structuredContent).length > 0) {
		return res.structuredContent;
	}
	if (text != null) {
		try {
			return JSON.parse(text);
		} catch {
			// Not JSON — return the raw text so callers can still show something.
			return { text };
		}
	}
	return {};
}

/** Extract an `ENGRAPHY_XYZ` code from an error string, else "ENGRAPHY_ERROR". */
export function parseErrorCode(message: string): string {
	const m = /\b(ENGRAPHY_[A-Z_]+)\b/.exec(message);
	return m ? m[1] : 'ENGRAPHY_ERROR';
}

/**
 * Ensure an MCP endpoint URL's path ends in a trailing slash. The Streamable-
 * HTTP transport POSTs to the exact URL; a server mounted at `/mcp/` answers
 * `/mcp` with a 307 redirect, and that redirect round-trip stalls the SDK ~10s
 * per call (see mcp-trailing-slash-307-latency). Normalizing here kills the
 * stall no matter how the user typed `engraphy.serverUrl`. An empty string is
 * returned unchanged so the caller's own "serverUrl not set" guard still fires;
 * a non-parseable string is returned as-is (the transport surfaces the error);
 * any query/fragment is preserved after the slash.
 */
export function normalizeServerUrl(url: string): string {
	const raw = (url ?? '').trim();
	if (!raw) {
		return raw;
	}
	try {
		const u = new URL(raw);
		if (!u.pathname.endsWith('/')) {
			u.pathname += '/';
		}
		return u.toString();
	} catch {
		return raw;
	}
}

// ---- argument builders (drop undefined keys so we never send nulls) --------

function compact(obj: Record<string, unknown>): Record<string, unknown> {
	const out: Record<string, unknown> = {};
	for (const [k, v] of Object.entries(obj)) {
		if (v !== undefined) {
			out[k] = v;
		}
	}
	return out;
}

export interface SearchOpts {
	scope: string;
	query: string;
	types?: string[];
	limit?: number;
	includeInactive?: boolean;
	detail?: 'full' | 'summary';
}
export function buildSearchArgs(o: SearchOpts): Record<string, unknown> {
	return compact({
		scope: o.scope,
		query: o.query,
		types: o.types,
		limit: o.limit,
		include_inactive: o.includeInactive,
		detail: o.detail,
	});
}

export interface TraverseOpts {
	startId: string;
	direction: 'out' | 'in' | 'both';
	edgeTypes?: string[];
	maxDepth?: number;
	limit?: number;
	detail?: 'full' | 'summary';
}
export function buildTraverseArgs(o: TraverseOpts): Record<string, unknown> {
	// `direction` is required by the server (read.py / core.traverse raises on
	// an invalid/missing one) — always send it.
	return compact({
		start_id: o.startId,
		direction: o.direction,
		edge_types: o.edgeTypes,
		max_depth: o.maxDepth,
		limit: o.limit,
		detail: o.detail,
	});
}

/** get accepts "up to 25 ids" (tool_registry) — clamp a single call to 25. */
export const GET_MAX_IDS = 25;
export function buildGetArgs(ids: string[]): Record<string, unknown> {
	return { ids: ids.slice(0, GET_MAX_IDS) };
}
/** Split a longer id list into <=25-id chunks for multiple get calls. */
export function chunkIds(ids: string[], size = GET_MAX_IDS): string[][] {
	const chunks: string[][] = [];
	for (let i = 0; i < ids.length; i += size) {
		chunks.push(ids.slice(i, i + size));
	}
	return chunks;
}

export function buildInboxListArgs(limit?: number, offset?: number): Record<string, unknown> {
	return compact({ action: 'list', limit, offset });
}
export function buildPendingListArgs(limit?: number, offset?: number): Record<string, unknown> {
	return compact({ limit, offset });
}
export function buildInboxDiscardArgs(id: string): Record<string, unknown> {
	return { action: 'discard', id };
}
export interface PromoteOpts {
	id: string;
	type: string;
	scope: string;
	title: string;
	body: string;
	attrs?: Record<string, unknown>;
	links?: unknown[];
	sessionId?: string;
}
export function buildInboxPromoteArgs(o: PromoteOpts): Record<string, unknown> {
	return compact({
		action: 'promote',
		id: o.id,
		type: o.type,
		scope: o.scope,
		title: o.title,
		body: o.body,
		attrs: o.attrs,
		links: o.links,
		session_id: o.sessionId,
	});
}

// ---- inbox parsing (pure — the confirm-queue's inbox band) -----------------

export interface InboxItemData {
	id: string;
	kind: string;
	scope: string | null;
	payload: unknown;
	created_at: string;
}

/** Parse the {v, items, truncated} inbox envelope. Defensive per field. */
export function inboxItemsFrom(result: unknown): { items: InboxItemData[]; truncated: boolean } {
	const r = result as { items?: InboxItemData[]; truncated?: boolean };
	return { items: r.items ?? [], truncated: !!r.truncated };
}

// ---- pending_list parsing (pure — the confirm-queue's pending band) --------

export interface PendingCandidate {
	id: string;
	title: string;
	similarity: number;
}

/** One row of pending_list's `pending` array (server field names preserved). */
export interface PendingListItem {
	id: string;
	payload_preview: string;
	candidates: PendingCandidate[];
	expires_at: string | null;
	created_at: string | null;
}

/**
 * Parse the {v:1, pending:[...]} envelope from pending_list. Defensive per field.
 * Note `payload_preview` is a STRING (title + capped body), not an object — it is
 * rendered as-is, never JSON-stringified.
 */
export function pendingItemsFrom(result: unknown): PendingListItem[] {
	const r = result as { pending?: Array<Record<string, unknown>> };
	return (r.pending ?? []).map((p) => ({
		id: String(p.id ?? ''),
		payload_preview:
			typeof p.payload_preview === 'string' ? p.payload_preview : String(p.payload_preview ?? ''),
		candidates: Array.isArray(p.candidates)
			? (p.candidates as Array<Record<string, unknown>>).map((c) => ({
					id: String(c.id ?? ''),
					title: String(c.title ?? '(untitled)'),
					similarity: typeof c.similarity === 'number' ? c.similarity : Number(c.similarity ?? 0),
				}))
			: [],
		expires_at: p.expires_at == null ? null : String(p.expires_at),
		created_at: p.created_at == null ? null : String(p.created_at),
	}));
}

/** True when the row's TTL has passed — resolve_duplicate will refuse it. */
export function isExpired(item: PendingListItem, now: number = Date.now()): boolean {
	if (!item.expires_at) {
		return false;
	}
	const t = Date.parse(item.expires_at);
	return !Number.isNaN(t) && t <= now;
}

/**
 * resolve_duplicate: {pending_id, resolution, merge_into?}. `merge_into` is only
 * meaningful — and only sent — when resolution === 'merge'.
 */
export function buildResolveDuplicateArgs(
	pendingId: string,
	resolution: 'distinct' | 'merge',
	mergeInto?: string
): Record<string, unknown> {
	return compact({
		pending_id: pendingId,
		resolution,
		merge_into: resolution === 'merge' ? mergeInto : undefined,
	});
}
