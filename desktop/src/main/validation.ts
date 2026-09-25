// Settings input validation, desktop-owned and pure.
//
// The extension validated a server URL with a single boolean (`isValidServerUrl`
// in client/webviewMessages.ts, frozen). A desktop Settings screen needs more
// than pass/fail: it needs per-field errors that block the save, warnings that
// do not, and normalization it can apply for the user instead of scolding them
// (the trailing-slash case is the big one, since `/mcp` costs ~10s per call to a
// 307 redirect).
//
// Pure: no electron, no DOM. Covered by scripts/test-client.js.

export type IssueLevel = 'error' | 'warn';

export interface FieldIssue {
	level: IssueLevel;
	message: string;
}

export interface FieldResult {
	issues: FieldIssue[];
	/** The value that should actually be saved / sent. */
	normalized: string;
}

export interface SettingsValidation {
	/** False when any field carries an `error`. Warnings never block. */
	ok: boolean;
	serverUrl: FieldResult;
	token: FieldResult;
	space: FieldResult;
	/** True when normalization changed something, so the UI can say what it did. */
	changed: boolean;
}

/** Loopback hosts where plain http is fine and no TLS warning is warranted. */
const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]', '0.0.0.0']);

export function isLocalHost(hostname: string): boolean {
	const h = (hostname || '').toLowerCase();
	return LOCAL_HOSTS.has(h) || h.endsWith('.local') || h.endsWith('.localhost');
}

/**
 * Validate + normalize the MCP endpoint URL.
 *
 * Errors (block the save): empty, unparseable, non-http(s) scheme.
 * Warnings (allowed through, normalized where we safely can): a missing trailing
 * slash (auto-added), a path with no `mcp` segment, a bare origin, and plain
 * http to a non-loopback host, which sends the bearer token in the clear.
 */
export function validateServerUrl(raw: string): FieldResult {
	const issues: FieldIssue[] = [];
	const trimmed = (raw ?? '').trim();

	if (!trimmed) {
		return {
			issues: [{ level: 'error', message: 'Enter the MCP endpoint, for example http://127.0.0.1:8000/mcp/' }],
			normalized: '',
		};
	}
	if (/\s/.test(trimmed)) {
		return {
			issues: [{ level: 'error', message: 'The URL contains a space. Paste just the endpoint.' }],
			normalized: trimmed,
		};
	}

	let u: URL;
	try {
		u = new URL(trimmed);
	} catch {
		const hint = /^[\w.-]+(:\d+)?(\/|$)/.test(trimmed)
			? ' Try adding http:// in front.'
			: '';
		return {
			issues: [{ level: 'error', message: 'That is not a valid URL.' + hint }],
			normalized: trimmed,
		};
	}

	if (u.protocol !== 'http:' && u.protocol !== 'https:') {
		return {
			issues: [
				{
					level: 'error',
					message: 'The URL must start with http:// or https:// (found ' + u.protocol + ').',
				},
			],
			normalized: trimmed,
		};
	}

	// Trailing slash: `/mcp` 307-redirects to `/mcp/` and the round trip stalls
	// every single call. Fix it silently and say so.
	if (!u.pathname.endsWith('/')) {
		u.pathname += '/';
		issues.push({
			level: 'warn',
			message: 'Added the trailing slash. Without it every call pays a 307 redirect.',
		});
	}
	if (u.pathname === '/') {
		issues.push({
			level: 'warn',
			message: 'This points at the server root. The MCP endpoint is usually /mcp/.',
		});
	} else if (!/mcp/i.test(u.pathname)) {
		issues.push({
			level: 'warn',
			message: 'This path has no "mcp" segment. Most Engraphy servers expose MCP at /mcp/.',
		});
	}
	if (u.protocol === 'http:' && !isLocalHost(u.hostname)) {
		issues.push({
			level: 'warn',
			message: 'Plain http to a remote host sends your token unencrypted. Prefer https outside localhost.',
		});
	}
	if (u.search || u.hash) {
		issues.push({ level: 'warn', message: 'Query strings and fragments are not used by the MCP endpoint.' });
	}

	return { issues, normalized: u.toString() };
}

/**
 * Validate + normalize a pasted token.
 *
 * `undefined` means "leave the stored token alone" and is always valid. The
 * common paste accidents are a copied `Authorization: Bearer ...` header and
 * trailing whitespace/newlines from a terminal, both of which we repair rather
 * than reject.
 */
export function validateToken(raw: string | undefined): FieldResult {
	if (raw === undefined) {
		return { issues: [], normalized: '' };
	}
	const issues: FieldIssue[] = [];
	let v = String(raw);

	const trimmed = v.trim();
	if (trimmed !== v) {
		issues.push({ level: 'warn', message: 'Trimmed surrounding whitespace from the token.' });
		v = trimmed;
	}
	const deHeadered = v.replace(/^Authorization\s*:\s*/i, '').trim();
	if (deHeadered !== v) {
		issues.push({ level: 'warn', message: 'Dropped the "Authorization:" header prefix.' });
		v = deHeadered;
	}
	const deBearer = v.replace(/^Bearer\s+/i, '').trim();
	if (deBearer !== v) {
		issues.push({ level: 'warn', message: 'Dropped the "Bearer " prefix. Paste only the token itself.' });
		v = deBearer;
	}
	if (v.length === 0) {
		// An explicit empty string is the documented "clear the token" signal.
		return { issues, normalized: '' };
	}
	if (/\s/.test(v)) {
		issues.push({
			level: 'error',
			message: 'The token contains a space or newline. It was probably copied with extra text.',
		});
	} else if (v.length < 8) {
		issues.push({ level: 'warn', message: 'That is short for a token. Check you copied the whole thing.' });
	}
	return { issues, normalized: v };
}

/**
 * Validate the space label. It is informational only (the real space is bound to
 * the token server side), so nothing here blocks except an absurd length.
 */
export function validateSpace(raw: string): FieldResult {
	const issues: FieldIssue[] = [];
	const v = (raw ?? '').trim();
	if (v.length > 64) {
		issues.push({ level: 'error', message: 'Keep the space label under 64 characters.' });
	}
	if (v && /[\r\n\t]/.test(v)) {
		issues.push({ level: 'error', message: 'The space label cannot contain line breaks or tabs.' });
	}
	return { issues, normalized: v };
}

/** Validate a whole settings form in one pass. */
export function validateSettingsInput(input: {
	serverUrl: string;
	token?: string;
	space: string;
}): SettingsValidation {
	const serverUrl = validateServerUrl(input.serverUrl);
	const token = validateToken(input.token);
	const space = validateSpace(input.space);
	const all = [serverUrl, token, space];
	return {
		ok: all.every((f) => !f.issues.some((i) => i.level === 'error')),
		serverUrl,
		token,
		space,
		changed:
			serverUrl.normalized !== (input.serverUrl ?? '').trim() ||
			(input.token !== undefined && token.normalized !== input.token) ||
			space.normalized !== (input.space ?? '').trim(),
	};
}

/** First blocking message across the whole form, for a one-line status area. */
export function firstError(v: SettingsValidation): string | null {
	for (const f of [v.serverUrl, v.token, v.space]) {
		const e = f.issues.find((i) => i.level === 'error');
		if (e) {
			return e.message;
		}
	}
	return null;
}
