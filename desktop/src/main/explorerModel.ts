// Pure result-shaping for the memory explorer.
//
// Lifted out of main.ts so it is unit-testable, and extended: the shipped
// `get` handler returned the whole `{v:1, nodes:[...], missing:[...]}` envelope
// and the view pretty-printed that raw JSON blob at the user. Engraphy's `get`
// (engraphy/core/get.py) returns a full node envelope with body, attrs, addenda,
// status, author and capped in/out edges, which is enough to render a real
// detail card instead.
//
// Pure: no electron, no SDK, no DOM. Covered by scripts/test-client.js.

/** The row shape the explorer list renders. */
export interface NodeRef {
	id: string;
	title: string;
	type: string;
	scope: string;
}

/** One edge on a node detail, flattened toward the *other* end. */
export interface EdgeRef {
	/** Edge type, e.g. "about" / "depends_on". */
	type: string;
	/** The id at the far end of the edge. */
	otherId: string;
	direction: 'out' | 'in';
}

/** A rendered node detail card. Every field is display-ready. */
export interface NodeDetailVM {
	id: string;
	title: string;
	type: string;
	scope: string;
	status: string | null;
	author: string | null;
	createdAt: string | null;
	body: string;
	/** attrs flattened to label/value pairs; objects are JSON-stringified. */
	attrs: Array<{ key: string; value: string }>;
	/** Merge history and similar top-level addenda, pretty-printed. */
	addenda: string[];
	edges: EdgeRef[];
	/** Whatever we could not model, so nothing is silently dropped. */
	raw: string;
}

function str(v: unknown, fallback = ''): string {
	if (v === null || v === undefined) {
		return fallback;
	}
	return typeof v === 'string' ? v : String(v);
}

function asRecord(v: unknown): Record<string, unknown> {
	return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function safeJson(v: unknown, indent = 2): string {
	try {
		return JSON.stringify(v, null, indent) ?? String(v);
	} catch {
		return String(v);
	}
}

export function toNodeRef(n: unknown): NodeRef {
	const r = asRecord(n);
	return {
		id: str(r.id),
		title: str(r.title, '(untitled)') || '(untitled)',
		type: str(r.type, '?') || '?',
		scope: str(r.scope, '?') || '?',
	};
}

/** `search` returns {v, results:[{node, score}]}. */
export function nodeRefsFromSearch(result: unknown): NodeRef[] {
	const r = asRecord(result);
	const results = Array.isArray(r.results) ? r.results : [];
	return results
		.map((x) => asRecord(x).node)
		.filter((n) => !!n)
		.map(toNodeRef)
		.filter((n) => n.id.length > 0);
}

/** `traverse` returns {v, nodes:[{…node…, depth}], edges:[…]}. */
export function nodeRefsFromTraverse(result: unknown, excludeId?: string): NodeRef[] {
	const r = asRecord(result);
	const nodes = Array.isArray(r.nodes) ? r.nodes : [];
	return nodes
		.map(toNodeRef)
		.filter((n) => n.id.length > 0 && n.id !== excludeId);
}

/** `scope_list` returns {v, scopes:[…]} where a scope may be a string or an object. */
export function scopeIdsFrom(result: unknown): string[] {
	const r = asRecord(result);
	const scopes = Array.isArray(r.scopes) ? r.scopes : [];
	const out: string[] = [];
	for (const s of scopes) {
		if (typeof s === 'string') {
			if (s) {
				out.push(s);
			}
			continue;
		}
		const o = asRecord(s);
		const id = str(o.id) || str(o.name) || str(o.display_name);
		if (id) {
			out.push(id);
		}
	}
	return Array.from(new Set(out));
}

function edgesFrom(node: Record<string, unknown>, selfId: string): EdgeRef[] {
	const e = asRecord(node.edges);
	const out: EdgeRef[] = [];
	const take = (list: unknown, direction: 'out' | 'in'): void => {
		if (!Array.isArray(list)) {
			return;
		}
		for (const raw of list) {
			const r = asRecord(raw);
			const src = str(r.src);
			const dst = str(r.dst);
			// Prefer the documented {src,dst,type}; tolerate the stub's {rel,to}.
			const otherId = direction === 'out' ? dst || str(r.to) : src || str(r.to);
			const type = str(r.type) || str(r.rel) || 'linked';
			if (otherId && otherId !== selfId) {
				out.push({ type, otherId, direction });
			}
		}
	};
	take(e.out, 'out');
	take(e.in, 'in');
	// The dev stub models links as a flat `links:[{rel,to}]` array.
	take(node.links, 'out');
	return out;
}

/**
 * Turn a `get` envelope into a detail card for one id.
 *
 * Returns null when the id came back in `missing` or simply is not present,
 * which Engraphy treats as information rather than an error (existence is
 * information: an unreadable node and an unknown node collapse to the same
 * answer), so the caller renders "not found or not readable by your token".
 */
export function nodeDetailFrom(result: unknown, id: string): NodeDetailVM | null {
	const r = asRecord(result);
	const nodes = Array.isArray(r.nodes) ? r.nodes : [];
	const match = nodes.map(asRecord).find((n) => str(n.id) === id) ?? (nodes.length === 1 ? asRecord(nodes[0]) : null);
	if (!match || Object.keys(match).length === 0) {
		return null;
	}
	const attrsRecord = asRecord(match.attrs);
	const addenda = Array.isArray(match.addenda) ? match.addenda : [];
	return {
		id: str(match.id, id) || id,
		title: str(match.title, '(untitled)') || '(untitled)',
		type: str(match.type, '?') || '?',
		scope: str(match.scope, '?') || '?',
		status: match.status == null ? null : str(match.status),
		author: match.author == null ? null : str(match.author),
		createdAt: match.created_at == null ? null : str(match.created_at),
		body: str(match.body),
		attrs: Object.keys(attrsRecord)
			.sort()
			.map((key) => ({
				key,
				value: typeof attrsRecord[key] === 'string' ? (attrsRecord[key] as string) : safeJson(attrsRecord[key], 0),
			})),
		addenda: addenda.map((a) => (typeof a === 'string' ? a : safeJson(a))),
		edges: edgesFrom(match, str(match.id, id)),
		raw: safeJson(match),
	};
}

/** Distinct node types actually present in a set of search results. */
export function observedTypesFrom(nodes: NodeRef[]): string[] {
	const seen = new Set<string>();
	for (const n of nodes) {
		if (n.type && n.type !== '?') {
			seen.add(n.type);
		}
	}
	return Array.from(seen).sort();
}

export interface NodeTypeOption {
	type: string;
	description: string;
}

/**
 * Build the promote/authoring node-type list for THIS space.
 *
 * The static STARTER_NODE_TYPES list is the starter pack's, and a real space
 * often uses a different one. Devon's live space is the concrete case: it holds
 * types `note` and `project`, while the starter list offers `project_ref`. So a
 * user promoting an item was shown a type their space does not use, and the
 * right one only via the "Other..." free-text escape hatch.
 *
 * No MCP tool exposes a space's node types (the per-space table is server
 * internal and `write`'s inputSchema types `type` as a plain string, not an
 * enum), so the next best signal is what the space demonstrably contains.
 * Observed types lead; the starter entries stay underneath rather than being
 * replaced, because the observed set only reflects whatever the last search
 * returned and could be a narrow slice.
 */
export function mergeNodeTypes(observed: string[], starter: NodeTypeOption[]): NodeTypeOption[] {
	const out: NodeTypeOption[] = [];
	const taken = new Set<string>();
	for (const type of observed) {
		if (!type || taken.has(type)) {
			continue;
		}
		taken.add(type);
		const known = starter.find((s) => s.type === type);
		out.push({
			type,
			description: known ? known.description : 'Already used in this space.',
		});
	}
	for (const s of starter) {
		if (!taken.has(s.type)) {
			taken.add(s.type);
			out.push(s);
		}
	}
	return out;
}

/** Ids the server explicitly reported as unknown-or-unreadable. */
export function missingIdsFrom(result: unknown): string[] {
	const r = asRecord(result);
	return Array.isArray(r.missing) ? r.missing.map((m) => str(m)).filter(Boolean) : [];
}
