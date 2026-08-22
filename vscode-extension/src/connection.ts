// Connection model: error classification, the panel connection state, and the
// status-bar health view-model.
//
// WHY THIS IS A SEPARATE MODULE. `webviewMessages.ts` is one of four files the
// Engraphy desktop app carries as a verbatim copy, so it is kept frozen and its
// older `isConnectionError` / `computeConnectionState` are left in place. Those
// two collapse "the server is not running" and "the server rejected your token"
// into a single `unreachable` state. Verified against a live Engraphy server
// (0.1.0): the MCP SDK surfaces a 401 as the string
//   "Streamable HTTP error: Error POSTing to endpoint: unauthorized"
// which carries no status digits, matches the old predicate's /Unauthorized/i
// branch, and therefore makes a HEALTHY server render "No server connected, you
// probably need to start one first". That is the wrong remedy for the single
// most likely first-run failure: a good server and a bad token.
//
// This module owns the three-way split. It is pure (no vscode, no SDK, no DOM),
// so scripts/test-client.js covers it.

/** Coarse failure class. Picks the UI state AND the remedy copy. */
export type ErrorClass = 'config' | 'auth' | 'transport' | 'tool';

export interface DescribedError {
	class: ErrorClass;
	/** One short sentence for the badge / banner. Safe to show a non-technical user. */
	summary: string;
	/** The raw underlying message, for the "detail" disclosure. Never shown by default. */
	detail: string;
	/** Transport code (ECONNREFUSED), HTTP status ("401"), or ENGRAPHY_* code when known. */
	code: string | null;
}

/** Node/undici transport codes that mean "we never got a usable HTTP response". */
const TRANSPORT_CODES = new Set([
	'ECONNREFUSED',
	'ECONNRESET',
	'ENOTFOUND',
	'EAI_AGAIN',
	'ETIMEDOUT',
	'EHOSTUNREACH',
	'ENETUNREACH',
	'EPIPE',
	'EPROTO',
	'UND_ERR_CONNECT_TIMEOUT',
	'UND_ERR_HEADERS_TIMEOUT',
	'UND_ERR_SOCKET',
	'CERT_HAS_EXPIRED',
	'DEPTH_ZERO_SELF_SIGNED_CERT',
	'SELF_SIGNED_CERT_IN_CHAIN',
	'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
]);

function messageOf(e: unknown): string {
	if (e instanceof Error) {
		return e.message || String(e);
	}
	if (typeof e === 'string') {
		return e;
	}
	if (e && typeof e === 'object' && typeof (e as { message?: unknown }).message === 'string') {
		return (e as { message: string }).message;
	}
	return String(e);
}

/** Pull the most specific machine code available off an error and its cause chain. */
function codeOf(e: unknown): string | null {
	const anyE = e as { code?: unknown; cause?: { code?: unknown } } | null;
	if (anyE && typeof anyE.code === 'number') {
		return String(anyE.code);
	}
	if (anyE && typeof anyE.code === 'string' && anyE.code.length > 0) {
		return anyE.code;
	}
	if (anyE && anyE.cause && typeof anyE.cause.code === 'string') {
		return anyE.cause.code;
	}
	return null;
}

/** Cause-chain message, where undici hides "connect ECONNREFUSED 127.0.0.1:9911". */
function causeMessage(e: unknown): string {
	const c = (e as { cause?: unknown } | null)?.cause;
	return c ? messageOf(c) : '';
}

const AUTH_RE =
	/\bunauthorized\b|\bforbidden\b|\b401\b|\b403\b|ENGRAPHY_AUTH|ENGRAPHY_ROLE|invalid\s+token|missing\s+token|authentication\s+failed/i;
const TRANSPORT_RE =
	/ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|fetch\s?failed|failed\s+to\s+fetch|socket\s+hang\s+up|network\s?error|ERR_SSL|ERR_TLS|certificate|self[-\s]?signed/i;
const CONFIG_RE = /serverUrl is not set|no server url/i;

/** Friendly one-liner per transport code. Falls back to a generic reachability line. */
function transportSummary(code: string | null, host: string): string {
	switch (code) {
		case 'ECONNREFUSED':
			return 'Nothing is listening at ' + host + '. Is the server running?';
		case 'ENOTFOUND':
		case 'EAI_AGAIN':
			return 'Could not resolve the host name in your server URL (' + host + ').';
		case 'ETIMEDOUT':
		case 'UND_ERR_CONNECT_TIMEOUT':
		case 'UND_ERR_HEADERS_TIMEOUT':
			return 'The server at ' + host + ' did not respond in time.';
		case 'EHOSTUNREACH':
		case 'ENETUNREACH':
			return 'No network route to ' + host + '.';
		case 'CERT_HAS_EXPIRED':
		case 'DEPTH_ZERO_SELF_SIGNED_CERT':
		case 'SELF_SIGNED_CERT_IN_CHAIN':
		case 'UNABLE_TO_VERIFY_LEAF_SIGNATURE':
			return 'The TLS certificate at ' + host + ' was rejected (' + code + ').';
		default:
			return 'Could not reach the server at ' + host + '.';
	}
}

/**
 * Turn any thrown value into a classified, user-showable description.
 *
 * `host` only makes the copy concrete ("Nothing is listening at 127.0.0.1:8000");
 * pass '' when it is unknown.
 */
export function describeError(e: unknown, host = ''): DescribedError {
	const msg = messageOf(e);
	const cause = causeMessage(e);
	const code = codeOf(e);
	const blob = msg + ' ' + cause;
	const where = host || 'the configured URL';

	if (CONFIG_RE.test(msg)) {
		return { class: 'config', summary: 'No server URL is set yet.', detail: msg, code };
	}
	// Auth is tested BEFORE transport on purpose: a 401 body can carry words the
	// transport regex would claim, and "your token was rejected" is always the
	// more actionable of the two readings.
	if (code === '401' || code === '403' || AUTH_RE.test(blob)) {
		return {
			class: 'auth',
			summary: 'The server at ' + where + ' rejected this token.',
			detail: msg,
			code: code ?? '401',
		};
	}
	if ((code && TRANSPORT_CODES.has(code)) || TRANSPORT_RE.test(blob)) {
		return {
			class: 'transport',
			summary: transportSummary(code, where),
			// The bare SDK message on a transport failure is just "fetch failed",
			// so fold in the cause, which carries the real reason.
			detail: cause ? msg + ' (' + cause + ')' : msg,
			code,
		};
	}
	return { class: 'tool', summary: msg, detail: msg, code };
}

/** host:port for error copy, derived from a server URL. Never throws. */
export function hostLabel(serverUrl: string): string {
	try {
		return new URL(serverUrl).host;
	} catch {
		return serverUrl || '';
	}
}

// ---- panel connection state ------------------------------------------------

/**
 * Reason a panel cannot show its bands. Widened from the two-way
 * unconfigured/unreachable split: `unauthorized` means the server answered and
 * refused the token, which needs a completely different remedy.
 */
export type NoServerReason = 'unconfigured' | 'unreachable' | 'unauthorized';

export interface NoServerVM {
	reason: NoServerReason;
	/** One user-showable sentence. */
	summary?: string;
	/** Raw transport/tool text, rendered behind a closed disclosure. */
	detail?: string;
	code?: string | null;
	/**
	 * Whether a token is configured at all. `unauthorized` covers two different
	 * situations that need different words: "you have not given me a token" and
	 * "the token you gave me was refused". Saying "rejected" to someone who never
	 * entered one sends them looking for a fault that is not there.
	 */
	hasToken?: boolean;
}

/**
 * Decide the panel-level connection state from the per-band failures.
 *
 * Preserves the rule that ONE band failing is a per-band problem rendered
 * inline, not a dead server: every slot must be non-null before the panel is
 * swapped for the recovery block. Adds the three-way split: all-auth failures
 * mean `unauthorized` (server is up, token is wrong), all-transport failures
 * mean `unreachable`.
 *
 * Returns null when the panel should render its normal bands.
 */
export function computeConnection(opts: {
	serverConfigured: boolean;
	/** One slot per band. `null` means that band succeeded. */
	errors: Array<unknown | null>;
	host?: string;
	hasToken?: boolean;
}): NoServerVM | null {
	if (!opts.serverConfigured) {
		return { reason: 'unconfigured', hasToken: opts.hasToken };
	}
	const slots = opts.errors;
	if (slots.length === 0 || slots.some((e) => e == null)) {
		return null;
	}
	const described = slots.map((e) => describeError(e, opts.host ?? ''));
	const first = described[0];
	if (described.every((d) => d.class === 'auth')) {
		return {
			reason: 'unauthorized',
			summary:
				opts.hasToken === false
					? 'The server at ' + (opts.host || 'the configured URL') + ' requires a token.'
					: first.summary,
			detail: first.detail,
			code: first.code,
			hasToken: opts.hasToken,
		};
	}
	if (described.every((d) => d.class === 'transport' || d.class === 'config')) {
		return {
			reason: 'unreachable',
			summary: first.summary,
			detail: first.detail,
			code: first.code,
			hasToken: opts.hasToken,
		};
	}
	// Mixed classes, or a plain tool error: not a panel-level connection problem.
	return null;
}

// ---- status bar health -----------------------------------------------------

export type HealthPhase =
	| 'unconfigured'
	| 'checking'
	| 'connected'
	| 'unauthorized'
	| 'unreachable'
	| 'degraded';

export interface HealthVM {
	phase: HealthPhase;
	/** Status-bar text, without the icon. Short enough for a crowded bar. */
	label: string;
	/** Tooltip. */
	title: string;
	/** Raw error detail, appended to the tooltip. */
	detail?: string;
	/** True when the extension can actually read data. Only this paints "connected". */
	usable: boolean;
	space?: string;
	version?: string;
	spaces?: number;
}

export interface HealthProbe {
	configured: boolean;
	hasToken: boolean;
	space: string;
	serverUrl: string;
	/** /healthz payload, or null when it failed or was not run. */
	info: { status?: string; version?: string; spaces?: number } | null;
	/** What /healthz threw, if anything. */
	healthzError?: unknown;
	/** True when the authenticated MCP probe (scope_list) succeeded. */
	authOk: boolean;
	/** What the authenticated probe threw, if anything. */
	authError?: unknown;
}

/**
 * Build the status-bar state.
 *
 * The load-bearing rule: /healthz is UNAUTHENTICATED on this server
 * (engraphy/server/app.py exempts it from the bearer middleware), so it answers
 * 200 for a server you hold no valid token for. Reporting "connected" off
 * /healthz alone is exactly how you get a healthy-looking status bar sitting
 * over panels that are all 401ing, which is the shipped 0.4.0 behavior. The bar
 * reports connected only when the AUTHENTICATED probe succeeded.
 */
export function buildHealthVM(p: HealthProbe): HealthVM {
	const host = hostLabel(p.serverUrl);
	if (!p.configured) {
		return {
			phase: 'unconfigured',
			label: 'Engraphy: no server',
			title:
				'Engraphy is not pointed at a memory server yet. Run "Engraphy: Connect to a server" to set one up.',
			usable: false,
		};
	}
	if (p.authOk) {
		const bits = ['Connected to ' + host];
		if (p.info?.version) {
			bits.push('server v' + p.info.version);
		}
		if (typeof p.info?.spaces === 'number') {
			bits.push(p.info.spaces + ' space(s)');
		}
		return {
			phase: 'connected',
			label: p.space ? 'Engraphy (' + p.space + ')' : 'Engraphy',
			title: bits.join(', ') + '. Click to refresh.',
			usable: true,
			space: p.space || undefined,
			version: p.info?.version,
			spaces: p.info?.spaces,
		};
	}
	const d = describeError(p.authError ?? p.healthzError ?? new Error('unknown'), host);
	if (d.class === 'auth') {
		return {
			phase: 'unauthorized',
			label: p.hasToken ? 'Engraphy: token rejected' : 'Engraphy: token needed',
			title: p.hasToken
				? host +
					' is running but rejected your token. Mint a fresh one with "engraphy-admin token create" ' +
					'and run "Engraphy: Connect to a server" to paste it in.'
				: host +
					' is running but requires a token. Run "Engraphy: Connect to a server" to add one.',
			detail: d.detail,
			usable: false,
		};
	}
	if (d.class === 'transport' || d.class === 'config') {
		return {
			phase: 'unreachable',
			label: 'Engraphy: unreachable',
			title: d.summary + ' Click to retry.',
			detail: d.detail,
			usable: false,
		};
	}
	return {
		phase: 'degraded',
		label: 'Engraphy: server error',
		title: host + ' answered but the connection check failed: ' + d.summary,
		detail: d.detail,
		usable: false,
	};
}
