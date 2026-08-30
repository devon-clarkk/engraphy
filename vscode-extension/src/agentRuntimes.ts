// Coding-agent runtimes, and registering Engraphy with the one the user
// actually runs.
//
// WHY THIS EXISTS. `vscode.lm.registerMcpServerDefinitionProvider` offers the
// Engraphy server to VS CODE. VS Code hands that to Copilot Chat. It does not
// reach any other agent: Claude Code reads `~/.claude.json`, Cursor reads
// `~/.cursor/mcp.json`, and neither one consults the VS Code MCP registry. An
// extension that only calls the VS Code API has therefore done nothing at all
// for a user whose agent is one of those, which is the failure this module
// closes: the extension writes the runtime's own config for them, so nobody has
// to hand-edit an MCP json file.
//
// SPLIT. Everything above the `---- filesystem ----` line is pure and covered by
// scripts/test-client.js. Below it is the fs layer, kept thin on purpose.
//
// TOKEN HANDLING. 0.5.0 deliberately moved the token out of `settings.json` and
// into the OS keychain. Registering with a third-party runtime writes a bearer
// token into that runtime's plaintext config, because an HTTP MCP entry has
// nowhere else to carry an Authorization header. That is a real trade and the
// caller MUST confirm it with the user before calling `registerRuntime`; see
// `TOKEN_PLAINTEXT_WARNING`.

import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

import type { AgentRuntimeStatus } from './capability';

export const TOKEN_PLAINTEXT_WARNING =
	'This writes your Engraphy URL and token into that agent’s own config file in plain text. ' +
	'The agent needs it there to send the Authorization header. Your keychain copy is unchanged.';

/** How one runtime stores MCP servers. */
export interface RuntimeSpec {
	id: string;
	label: string;
	/** Config file, relative to the home directory. */
	relPath: string;
	/** Top-level key holding the server map. */
	mapKey: 'mcpServers' | 'servers';
	/** Whether the entry carries an explicit `"type": "http"` discriminator. */
	typed: boolean;
	/**
	 * Extra paths that prove the runtime is installed even when the config file
	 * does not exist yet. Relative to home.
	 */
	detectPaths: string[];
}

/**
 * The runtimes the extension can register with.
 *
 * `vscode-user` is the same file a user would otherwise hand-edit, which is the
 * remedy this fix replaces with a button. It is listed last because the
 * provider API is the better path when something consumes it.
 */
export const RUNTIMES: RuntimeSpec[] = [
	{
		id: 'claude-code',
		label: 'Claude Code',
		relPath: '.claude.json',
		mapKey: 'mcpServers',
		typed: true,
		detectPaths: ['.claude.json', '.claude'],
	},
	{
		id: 'cursor',
		label: 'Cursor',
		relPath: path.join('.cursor', 'mcp.json'),
		mapKey: 'mcpServers',
		typed: false,
		detectPaths: ['.cursor'],
	},
	{
		id: 'vscode-user',
		label: 'VS Code user mcp.json',
		relPath: vsCodeUserMcpRelPath(),
		mapKey: 'servers',
		typed: true,
		detectPaths: [vsCodeUserMcpRelPath()],
	},
];

/** Per-platform location of VS Code's user-level mcp.json, relative to home. */
function vsCodeUserMcpRelPath(): string {
	if (process.platform === 'win32') {
		return path.join('AppData', 'Roaming', 'Code', 'User', 'mcp.json');
	}
	if (process.platform === 'darwin') {
		return path.join('Library', 'Application Support', 'Code', 'User', 'mcp.json');
	}
	return path.join('.config', 'Code', 'User', 'mcp.json');
}

// ---- pure config surgery ----------------------------------------------------

export interface ServerEntry {
	type?: 'http';
	url: string;
	headers?: Record<string, string>;
}

/** The entry to write for a given runtime shape. */
export function buildEntry(spec: RuntimeSpec, url: string, token: string): ServerEntry {
	const entry: ServerEntry = { url };
	if (spec.typed) {
		entry.type = 'http';
	}
	if (token) {
		entry.headers = { Authorization: 'Bearer ' + token };
	}
	return entry;
}

/** True when the parsed config already carries an `engraphy` server. */
export function hasEngraphy(config: unknown, spec: RuntimeSpec): boolean {
	const map = (config as Record<string, unknown> | null)?.[spec.mapKey];
	if (!map || typeof map !== 'object') {
		return false;
	}
	return Object.prototype.hasOwnProperty.call(map, 'engraphy');
}

/** The URL currently registered for engraphy, if any. Used to spot drift. */
export function registeredUrl(config: unknown, spec: RuntimeSpec): string | undefined {
	const map = (config as Record<string, unknown> | null)?.[spec.mapKey] as
		| Record<string, unknown>
		| undefined;
	const entry = map?.engraphy as { url?: unknown } | undefined;
	return typeof entry?.url === 'string' ? entry.url : undefined;
}

/**
 * Merge an engraphy entry into a parsed config, without disturbing anything
 * else in the file. Returns a new object; the input is not mutated.
 */
export function mergeEngraphy(
	config: unknown,
	spec: RuntimeSpec,
	entry: ServerEntry
): Record<string, unknown> {
	const base =
		config && typeof config === 'object' && !Array.isArray(config)
			? { ...(config as Record<string, unknown>) }
			: {};
	const existing = base[spec.mapKey];
	const map =
		existing && typeof existing === 'object' && !Array.isArray(existing)
			? { ...(existing as Record<string, unknown>) }
			: {};
	map.engraphy = entry;
	base[spec.mapKey] = map;
	return base;
}

/**
 * Strip `//` and block comments so a JSONC config (VS Code allows them in
 * mcp.json) can be parsed. String-aware, so a `//` inside a URL survives.
 *
 * This is deliberately only good enough to READ. If a file needed stripping the
 * caller must not write it back, because round-tripping would silently delete
 * the user's comments. `readConfig` reports `hadComments` for exactly that.
 */
export function stripJsonComments(text: string): { out: string; hadComments: boolean } {
	let out = '';
	let hadComments = false;
	let inString = false;
	let escaped = false;
	for (let i = 0; i < text.length; i++) {
		const c = text[i];
		const next = text[i + 1];
		if (inString) {
			out += c;
			if (escaped) {
				escaped = false;
			} else if (c === '\\') {
				escaped = true;
			} else if (c === '"') {
				inString = false;
			}
			continue;
		}
		if (c === '"') {
			inString = true;
			out += c;
			continue;
		}
		if (c === '/' && next === '/') {
			hadComments = true;
			while (i < text.length && text[i] !== '\n') {
				i++;
			}
			out += '\n';
			continue;
		}
		if (c === '/' && next === '*') {
			hadComments = true;
			i += 2;
			while (i < text.length && !(text[i] === '*' && text[i + 1] === '/')) {
				i++;
			}
			i++;
			continue;
		}
		out += c;
	}
	return { out, hadComments };
}

// ---- filesystem -------------------------------------------------------------

export interface LoadedConfig {
	path: string;
	exists: boolean;
	/** Parsed contents, or null when the file is absent or unparseable. */
	config: Record<string, unknown> | null;
	/** True when the file exists but could not be parsed. Never overwrite one. */
	unparseable: boolean;
	/** True when comments had to be stripped to parse. Never rewrite one. */
	hadComments: boolean;
}

export function configPathFor(spec: RuntimeSpec, home = os.homedir()): string {
	return path.join(home, spec.relPath);
}

export function readConfig(spec: RuntimeSpec, home = os.homedir()): LoadedConfig {
	const p = configPathFor(spec, home);
	if (!fs.existsSync(p)) {
		return { path: p, exists: false, config: null, unparseable: false, hadComments: false };
	}
	let text: string;
	try {
		text = fs.readFileSync(p, 'utf8');
	} catch {
		return { path: p, exists: true, config: null, unparseable: true, hadComments: false };
	}
	// An empty file is a legitimate starting point, not a parse failure. VS Code
	// ships a zero-byte User/mcp.json on installs that have never added a server.
	if (text.trim().length === 0) {
		return { path: p, exists: true, config: {}, unparseable: false, hadComments: false };
	}
	const { out, hadComments } = stripJsonComments(text);
	try {
		const parsed = JSON.parse(out) as unknown;
		if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
			return { path: p, exists: true, config: null, unparseable: true, hadComments };
		}
		return {
			path: p,
			exists: true,
			config: parsed as Record<string, unknown>,
			unparseable: false,
			hadComments,
		};
	} catch {
		return { path: p, exists: true, config: null, unparseable: true, hadComments };
	}
}

/** Every runtime, with whether it is installed and whether Engraphy is in it. */
export function detectRuntimes(home = os.homedir()): AgentRuntimeStatus[] {
	return RUNTIMES.map((spec) => {
		const detected = spec.detectPaths.some((rel) => fs.existsSync(path.join(home, rel)));
		const loaded = readConfig(spec, home);
		return {
			id: spec.id,
			label: spec.label,
			detected,
			registered: loaded.config ? hasEngraphy(loaded.config, spec) : false,
			configPath: loaded.path,
			writable: !loaded.unparseable && !loaded.hadComments,
		};
	});
}

export interface RegisterOutcome {
	ok: boolean;
	path: string;
	/** Present when the write was refused, explaining why in user-facing words. */
	problem?: string;
	/** Path of the backup taken before the write. */
	backup?: string;
	/** True when the entry was already correct and nothing needed writing. */
	unchanged?: boolean;
}

/**
 * Write the engraphy entry into one runtime's config.
 *
 * Refuses rather than risking the user's file when it cannot round-trip safely:
 * an unparseable config, or one carrying comments that a rewrite would delete.
 * Backs up before writing, writes to a sibling temp file and renames, so a
 * crash mid-write cannot leave a truncated config behind.
 */
export function registerRuntime(
	spec: RuntimeSpec,
	url: string,
	token: string,
	home = os.homedir()
): RegisterOutcome {
	const loaded = readConfig(spec, home);
	if (loaded.unparseable) {
		return {
			ok: false,
			path: loaded.path,
			problem:
				'That config file is not valid JSON, so it was left untouched. Fix or move it, then run this again.',
		};
	}
	if (loaded.hadComments) {
		return {
			ok: false,
			path: loaded.path,
			problem:
				'That config file contains comments, and rewriting it would delete them, so it was left untouched.',
		};
	}
	const entry = buildEntry(spec, url, token);
	const current = loaded.config ?? {};
	const existingEntry = (current[spec.mapKey] as Record<string, unknown> | undefined)?.engraphy;
	if (existingEntry && JSON.stringify(existingEntry) === JSON.stringify(entry)) {
		return { ok: true, path: loaded.path, unchanged: true };
	}
	const merged = mergeEngraphy(current, spec, entry);

	let backup: string | undefined;
	try {
		fs.mkdirSync(path.dirname(loaded.path), { recursive: true });
		if (loaded.exists) {
			backup = loaded.path + '.engraphy-backup';
			fs.copyFileSync(loaded.path, backup);
		}
		const tmp = loaded.path + '.engraphy-tmp';
		// mode 0600 matters on POSIX: the file now carries a bearer token. It is
		// a no-op on Windows, where the user profile ACL is the protection.
		fs.writeFileSync(tmp, JSON.stringify(merged, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 });
		fs.renameSync(tmp, loaded.path);
	} catch (e) {
		return {
			ok: false,
			path: loaded.path,
			problem: 'Could not write that config file: ' + (e instanceof Error ? e.message : String(e)),
			backup,
		};
	}
	return { ok: true, path: loaded.path, backup };
}

/**
 * Read back what landed on disk and confirm the entry is really there with the
 * URL just written. Registration is not reported as done on the strength of a
 * successful write call alone: this whole bug class comes from trusting an
 * unverified success.
 */
export function verifyRegistration(spec: RuntimeSpec, url: string, home = os.homedir()): boolean {
	const loaded = readConfig(spec, home);
	if (!loaded.config || !hasEngraphy(loaded.config, spec)) {
		return false;
	}
	return registeredUrl(loaded.config, spec) === url;
}
