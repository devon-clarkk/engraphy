// Pure, dependency-free glue between the confirm-queue webview and the extension
// host: the typed message protocol (both directions), the card view-models the
// webview renders, and a strict parser that turns an untrusted `postMessage`
// payload into a validated command intent (or null). No `vscode` / SDK / DOM
// imports so this is unit-testable with plain Node (scripts/test-client.js).

import { isExpired, type InboxItemData, type PendingListItem } from './toolResult';

// ---- view-models (host builds these; webview renders them) ------------------

export interface CandidateVM {
	id: string;
	title: string;
	/** 0..100, rounded — ready to render as a percent + meter width. */
	similarityPct: number;
}

export interface PendingCardVM {
	id: string;
	preview: string;
	candidates: CandidateVM[];
	expired: boolean;
	expiresAt: string | null;
	createdAt: string | null;
}

export interface InboxCardVM {
	id: string;
	preview: string;
	kind: string;
	scope: string;
	createdAt: string;
	/** Pretty-printed captured payload, shown read-only (promotion is authoring). */
	payloadJson: string;
}

/**
 * Whether the panel can talk to a server. 'no-server' swaps the bands for a
 * branded recovery block, with the remedy chosen by `reason`:
 *   'unconfigured'  no serverUrl set at all
 *   'unreachable'   a URL is set but nothing answered (server not running)
 *   'unauthorized'  the server answered and refused the token
 *
 * The three-way split matters: telling someone whose server is healthy to go
 * start a server is the wrong remedy, and it was the shipped 0.4.0 behavior.
 * The classifier that produces these lives in ./connection (computeConnection);
 * this shape is structurally identical to its NoServerVM.
 */
export type ConnectionVM =
	| { kind: 'ok' }
	| {
			kind: 'no-server';
			reason: 'unconfigured' | 'unreachable' | 'unauthorized';
			/** One user-showable sentence naming what actually failed. */
			summary?: string;
			/** Raw transport/tool text, rendered behind a closed disclosure. */
			detail?: string;
			code?: string | null;
			/** False when no token is configured, so the copy can say "add one". */
			hasToken?: boolean;
		};

/** The whole state the webview renders in one shot. */
export interface ConfirmStateVM {
	connection: ConnectionVM;
	pending: PendingCardVM[];
	inbox: InboxCardVM[];
	pendingError: string | null;
	inboxError: string | null;
	inboxTruncated: boolean;
	loading: boolean;
	/**
	 * True while the FIRST load is in flight. Drives skeleton placeholders instead
	 * of a spinner line, so a cold panel never flashes empty and then fills.
	 * Optional so the desktop's copy of this file stays assignable.
	 */
	firstLoad?: boolean;
}

export type ThemeKind = 'light' | 'dark' | 'high-contrast' | 'high-contrast-light';

/** Messages the host posts INTO the webview. */
export type HostToWebview =
	| { type: 'state'; state: ConfirmStateVM }
	| { type: 'theme'; kind: ThemeKind };

/** Messages the webview posts OUT to the host (untrusted until parsed). */
export type WebviewToHost =
	| { type: 'ready' }
	| { type: 'refresh' }
	| { type: 'approve'; pendingId: string }
	| { type: 'merge'; pendingId: string; mergeInto: string }
	| { type: 'promote'; inboxId: string }
	| { type: 'discard'; inboxId: string }
	| { type: 'openWalkthrough' }
	| { type: 'configureServer' }
	| { type: 'reconnect' };

// ---- host-side builders -----------------------------------------------------

function truncate(s: string, n = 200): string {
	const one = (s ?? '').replace(/\s+/g, ' ').trim();
	return one.length > n ? one.slice(0, n - 1) + '…' : one;
}

function safeJson(v: unknown): string {
	try {
		return JSON.stringify(v, null, 2);
	} catch {
		return String(v);
	}
}

/** Best-effort human preview of an opaque inbox payload. */
export function inboxPreview(d: InboxItemData): string {
	const p = d.payload;
	if (typeof p === 'string') {
		return truncate(p, 160);
	}
	if (p && typeof p === 'object') {
		const o = p as Record<string, unknown>;
		for (const k of ['title', 'text', 'message', 'summary']) {
			if (typeof o[k] === 'string') {
				return truncate(o[k] as string, 160);
			}
		}
	}
	return `${d.kind} item`;
}

export function buildPendingCard(item: PendingListItem, now: number = Date.now()): PendingCardVM {
	return {
		id: item.id,
		preview: item.payload_preview || '(pending write)',
		candidates: item.candidates.map((c) => ({
			id: c.id,
			title: c.title,
			similarityPct: Math.round((Number.isFinite(c.similarity) ? c.similarity : 0) * 100),
		})),
		expired: isExpired(item, now),
		expiresAt: item.expires_at,
		createdAt: item.created_at,
	};
}

export function buildInboxCard(d: InboxItemData): InboxCardVM {
	return {
		id: d.id,
		preview: inboxPreview(d),
		kind: d.kind,
		scope: d.scope ?? '(space)',
		createdAt: d.created_at,
		payloadJson: safeJson(d.payload),
	};
}

/** Map a VS Code ColorThemeKind enum value (1..4) to the webview theme tag. */
export function themeKindFromEnum(kind: number): ThemeKind {
	switch (kind) {
		case 2:
			return 'dark';
		case 3:
			return 'high-contrast'; // HighContrast (dark HC)
		case 4:
			return 'high-contrast-light'; // HighContrastLight
		default:
			return 'light';
	}
}

// ---- promote authoring helpers (static node types + payload prefill) --------

export interface NodeTypeOption {
	type: string;
	description: string;
}

/**
 * Valid node types for the STARTER pack (packs/starter/pack.yaml) — which the
 * demo / default space uses (demo/seed_demo.py writes project_ref / person /
 * preference / commitment).
 *
 * This list is STATIC because no MCP tool exposes a space's node types at
 * runtime: the per-space `node_types` table is server-internal, and the write /
 * inbox_review inputSchema types `type` as a plain string, NOT an enum
 * (engraphy/server/wire_types.py — `Arg(STRING, required=True)`), with the schema
 * generated per tool name, not per space. A conversational-pack space has
 * different types (thing / fact / event / decision / opinion), so the promote
 * flow always offers an "Other…" free-text escape hatch on top of this list.
 * TODO: source this dynamically once the server grows a describe / pack-schema
 * tool (then this constant becomes the offline fallback only).
 */
export const STARTER_NODE_TYPES: NodeTypeOption[] = [
	{ type: 'note', description: 'A stable, useful fact or piece of knowledge worth keeping.' },
	{ type: 'person', description: 'Someone whose context matters across conversations.' },
	{ type: 'preference', description: 'A durable like / dislike / default (strength hard|soft).' },
	{ type: 'commitment', description: 'A dated or recurring obligation (cadence, next_due).' },
	{ type: 'project_ref', description: 'A pointer to an ongoing effort or external resource.' },
];

export interface PromoteDefaults {
	title: string;
	body: string;
}

/**
 * Seed values for the promote title / body inputs from a captured inbox payload,
 * keeping them editable. Body precedence: text > message > summary > body. A
 * string payload seeds the body (never the title); a non-object payload yields
 * empty strings (no crash, no "[object Object]").
 *
 * NOTE: this deliberately SOFTENS core/inbox.py D1 ("the captured payload is
 * never the source of the node") — a Devon-approved product change. The user
 * still reviews and can edit every field before the write is issued.
 */
export function promoteDefaults(payload: unknown): PromoteDefaults {
	if (typeof payload === 'string') {
		return { title: '', body: payload };
	}
	if (payload && typeof payload === 'object') {
		const o = payload as Record<string, unknown>;
		const pick = (keys: string[]): string => {
			for (const k of keys) {
				if (typeof o[k] === 'string') {
					return o[k] as string;
				}
			}
			return '';
		};
		return { title: pick(['title']), body: pick(['text', 'message', 'summary', 'body']) };
	}
	return { title: '', body: '' };
}

// ---- webview→host message parsing (strict; untrusted input) -----------------

function asString(v: unknown): string | undefined {
	return typeof v === 'string' && v.length > 0 ? v : undefined;
}

/**
 * Validate a raw `postMessage` payload from the webview into a typed command, or
 * return null for anything malformed (unknown type, missing/blank ids). The host
 * must never act on an unparsed message — this is the trust boundary.
 */
export function parseWebviewMessage(raw: unknown): WebviewToHost | null {
	if (!raw || typeof raw !== 'object') {
		return null;
	}
	const m = raw as Record<string, unknown>;
	switch (m.type) {
		case 'ready':
			return { type: 'ready' };
		case 'refresh':
			return { type: 'refresh' };
		case 'approve': {
			const pendingId = asString(m.pendingId);
			return pendingId ? { type: 'approve', pendingId } : null;
		}
		case 'merge': {
			const pendingId = asString(m.pendingId);
			const mergeInto = asString(m.mergeInto);
			return pendingId && mergeInto ? { type: 'merge', pendingId, mergeInto } : null;
		}
		case 'promote': {
			const inboxId = asString(m.inboxId);
			return inboxId ? { type: 'promote', inboxId } : null;
		}
		case 'discard': {
			const inboxId = asString(m.inboxId);
			return inboxId ? { type: 'discard', inboxId } : null;
		}
		case 'openWalkthrough':
			return { type: 'openWalkthrough' };
		case 'configureServer':
			return { type: 'configureServer' };
		case 'reconnect':
			return { type: 'reconnect' };
		default:
			return null;
	}
}

// ---- connection state (no-server onboarding decision) -----------------------

/**
 * SUPERSEDED by describeError/computeConnection in ./connection, which classify
 * auth separately from transport instead of folding both into 'unreachable'.
 * Both functions below are kept because this file is carried verbatim by the
 * Engraphy desktop app; deleting exports here would break that copy and the
 * shared-package extraction that is meant to reconcile the two. Nothing in the
 * extension calls them any more.
 *
 * True when an error string looks like the server being unreachable or
 * rejecting us — a transport/connection/auth failure rather than a normal tool
 * error. Used to decide whether to swap the bands for the onboarding block, so
 * it errs toward the recognizable network/auth signatures and leaves ordinary
 * ENGRAPHY_* validation errors to render inline.
 */
export function isConnectionError(message: string): boolean {
	if (!message) {
		return false;
	}
	return /ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|fetch failed|failed to fetch|socket hang up|network\s?error|and the server|Unauthorized|Forbidden|\b401\b|\b403\b|ENGRAPHY_AUTH|ENGRAPHY_ROLE|is not set/i.test(
		message
	);
}

/**
 * Decide whether the panel should show its normal bands or the branded
 * "no server connected" onboarding block. Unconfigured (no URL) always shows
 * onboarding; a configured URL shows onboarding only when BOTH bands failed
 * with connection-like errors (one band erroring on its own is a per-band
 * problem, not a dead server).
 */
export function computeConnectionState(opts: {
	serverConfigured: boolean;
	pendingError: string | null;
	inboxError: string | null;
}): ConnectionVM {
	if (!opts.serverConfigured) {
		return { kind: 'no-server', reason: 'unconfigured' };
	}
	const { pendingError, inboxError } = opts;
	if (pendingError && inboxError && isConnectionError(pendingError) && isConnectionError(inboxError)) {
		return { kind: 'no-server', reason: 'unreachable', detail: pendingError };
	}
	return { kind: 'ok' };
}

/**
 * Validate a user-entered server URL: must parse as an absolute http(s) URL.
 * Pure so the configure-server command can reject junk before saving it.
 */
export function isValidServerUrl(url: string): boolean {
	const raw = (url ?? '').trim();
	if (!raw) {
		return false;
	}
	try {
		const u = new URL(raw);
		return u.protocol === 'http:' || u.protocol === 'https:';
	} catch {
		return false;
	}
}
