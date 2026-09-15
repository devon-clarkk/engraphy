// Connection settings + local app state persistence for the desktop app.
//
// The VS Code extension read serverUrl / token / space from VS Code settings.
// There is no such store here, so this owns its own persistence in the
// OS-appropriate per-user app data directory (app.getPath('userData')):
//   • Windows: %APPDATA%\Engraphy\engraphy-settings.json
//   • macOS:   ~/Library/Application Support/Engraphy/engraphy-settings.json
//
// The token is a secret (it IS the identity on the server), so it is never
// written in plaintext when the OS keychain is available: safeStorage wraps
// DPAPI on Windows and Keychain on macOS. When encryption is unavailable
// (e.g. a headless Linux session) we fall back to plaintext with a persisted
// `tokenPlain` marker so the UI can warn. The token is never logged.
//
// The same file also carries non-secret local app state (first-run flag, window
// bounds). Every writer merges over the previous file rather than rewriting it
// from scratch, so one subsystem's save cannot silently drop another's keys.

import { app, safeStorage } from 'electron';
import * as fs from 'fs';
import * as path from 'path';

import { parseBootstrap, shouldImport, type Bootstrap } from './bootstrap';

export const DEFAULT_SERVER_URL = 'http://127.0.0.1:8000/mcp/';

export interface Settings {
	serverUrl: string;
	token: string;
	space: string;
}

/** What the renderer is allowed to see: never the token itself, only whether one is set. */
export interface SafeSettings {
	serverUrl: string;
	space: string;
	hasToken: boolean;
	/** True when the token had to be stored in plaintext (no OS keychain). */
	tokenInsecure: boolean;
	/** Where the settings file lives, so the UI can offer "show me". */
	settingsPath: string;
	/** False until the user has finished (or skipped) first-run onboarding. */
	onboardingCompleted: boolean;
	/** Whether the daily check for a newer published version runs. */
	updateCheckEnabled: boolean;
}

export interface WindowBounds {
	x?: number;
	y?: number;
	width: number;
	height: number;
	maximized?: boolean;
}

interface DiskShape {
	serverUrl?: string;
	space?: string;
	/** base64 of safeStorage.encryptString(token) when encrypted. */
	tokenEnc?: string;
	/** plaintext token, only when the OS keychain was unavailable. */
	tokenPlain?: string;
	onboardingCompleted?: boolean;
	windowBounds?: WindowBounds;
	/** Epoch ms of the last update check, so it runs once a day, not once a launch. */
	updateLastChecked?: number;
	/** The published version the user said "not now" to. Per version, never forever. */
	updateDismissed?: string;
	/** False turns the update check off entirely. Absent means on. */
	updateCheckEnabled?: boolean;
}

export function settingsFilePath(): string {
	return path.join(app.getPath('userData'), 'engraphy-settings.json');
}

function readDisk(): DiskShape {
	try {
		const raw = fs.readFileSync(settingsFilePath(), 'utf8');
		const parsed = JSON.parse(raw) as DiskShape;
		return parsed && typeof parsed === 'object' ? parsed : {};
	} catch {
		return {};
	}
}

function writeDisk(d: DiskShape): void {
	const file = settingsFilePath();
	fs.mkdirSync(path.dirname(file), { recursive: true });
	fs.writeFileSync(file, JSON.stringify(d, null, 2), { encoding: 'utf8', mode: 0o600 });
}

/** Merge a patch over the current file. Never drops keys this caller does not know about. */
function patchDisk(patch: Partial<DiskShape>): DiskShape {
	const next = { ...readDisk(), ...patch };
	writeDisk(next);
	return next;
}

function decodeToken(d: DiskShape): string {
	if (d.tokenEnc) {
		try {
			return safeStorage.decryptString(Buffer.from(d.tokenEnc, 'base64'));
		} catch {
			return '';
		}
	}
	return d.tokenPlain ?? '';
}

/** Full settings incl. the decoded token. Main-process only, never sent to the renderer. */
export function loadSettings(): Settings {
	const d = readDisk();
	return {
		serverUrl: (d.serverUrl ?? DEFAULT_SERVER_URL).trim(),
		token: decodeToken(d),
		space: (d.space ?? '').trim(),
	};
}

/** The renderer-safe projection (no token value). */
export function loadSafeSettings(): SafeSettings {
	const d = readDisk();
	const token = decodeToken(d);
	return {
		serverUrl: (d.serverUrl ?? DEFAULT_SERVER_URL).trim(),
		space: (d.space ?? '').trim(),
		hasToken: token.length > 0,
		tokenInsecure: !!d.tokenPlain && !d.tokenEnc,
		settingsPath: settingsFilePath(),
		onboardingCompleted: !!d.onboardingCompleted,
		// Absent means on, matching loadUpdateState.
		updateCheckEnabled: d.updateCheckEnabled !== false,
	};
}

/**
 * Persist connection settings. An `undefined` token means "leave the stored
 * token unchanged" (so the user can update url/space without re-typing it); an
 * empty-string token clears it.
 */
export function saveSettings(next: { serverUrl: string; space: string; token?: string }): SafeSettings {
	const prev = readDisk();
	const d: DiskShape = { ...prev, serverUrl: next.serverUrl.trim(), space: next.space.trim() };

	// Resolve the effective token: keep the previous one when token is undefined.
	const token = next.token === undefined ? decodeToken(prev) : next.token;

	// Always clear both slots first, so switching between encrypted and plaintext
	// (or clearing outright) can never leave a stale second copy on disk.
	delete d.tokenEnc;
	delete d.tokenPlain;
	if (token.length > 0) {
		if (safeStorage.isEncryptionAvailable()) {
			d.tokenEnc = safeStorage.encryptString(token).toString('base64');
		} else {
			d.tokenPlain = token;
		}
	}

	writeDisk(d);
	return loadSafeSettings();
}

// ---- local app state (non-secret) ------------------------------------------

/** True once the user has completed or explicitly skipped first-run onboarding. */
export function isOnboardingCompleted(): boolean {
	return !!readDisk().onboardingCompleted;
}

export function setOnboardingCompleted(done: boolean): void {
	patchDisk({ onboardingCompleted: done });
}

/** Last window geometry, or undefined on first run / after a corrupt read. */
export function loadWindowBounds(): WindowBounds | undefined {
	const b = readDisk().windowBounds;
	if (!b || typeof b.width !== 'number' || typeof b.height !== 'number') {
		return undefined;
	}
	if (!Number.isFinite(b.width) || !Number.isFinite(b.height) || b.width < 200 || b.height < 200) {
		return undefined;
	}
	return b;
}

export function saveWindowBounds(b: WindowBounds): void {
	patchDisk({ windowBounds: b });
}

// ---- update checking -------------------------------------------------------
//
// Three values, all non-secret, all merged over the same file as everything
// else. They live here rather than in memory because the point of each one is
// that it survives a restart: a check that ran on every launch would be a
// prompt on every launch, and a dismissal that did not persist would be no
// dismissal at all.

export interface UpdateState {
	lastChecked: number | undefined;
	dismissed: string | undefined;
	enabled: boolean;
}

export function loadUpdateState(): UpdateState {
	const d = readDisk();
	return {
		lastChecked: typeof d.updateLastChecked === 'number' ? d.updateLastChecked : undefined,
		dismissed: typeof d.updateDismissed === 'string' ? d.updateDismissed : undefined,
		// Absent means on, so an existing settings file does not have to be
		// rewritten for the check to start working.
		enabled: d.updateCheckEnabled !== false,
	};
}

export function setUpdateChecked(atMs: number): void {
	patchDisk({ updateLastChecked: atMs });
}

export function setUpdateDismissed(version: string): void {
	patchDisk({ updateDismissed: version });
}

export function setUpdateCheckEnabled(enabled: boolean): void {
	patchDisk({ updateCheckEnabled: enabled });
}

// ---- the installer handoff --------------------------------------------------
//
// The Windows installer mints a token before this app is ever opened and leaves
// it in `engraphy-bootstrap.json` beside this file. See bootstrap.ts for what
// the document has to look like and why the import happens exactly once.

/** Where the Windows installer writes the handoff, next to the settings file. */
export function bootstrapFilePath(): string {
	return path.join(app.getPath('userData'), 'engraphy-bootstrap.json');
}

/**
 * Import a handoff, if there is one and this app has no connection yet.
 *
 * Returns the imported settings, or undefined when nothing was imported, which
 * is the normal case on every launch after the first. The file is deleted on
 * success, so the token exists in plaintext for one launch rather than
 * indefinitely; it is deleted on a parse failure too, because a malformed
 * handoff is never going to become a good one and leaving it invites a reader
 * who is less careful than this one.
 */
export function importBootstrapFile(): SafeSettings | undefined {
	const file = bootstrapFilePath();
	let raw: string;
	try {
		raw = fs.readFileSync(file, 'utf8');
	} catch {
		return undefined;
	}

	const current = loadSafeSettings();
	if (!shouldImport(current.hasToken, current.onboardingCompleted)) {
		return undefined;
	}

	let parsed: Bootstrap | null = null;
	try {
		parsed = parseBootstrap(JSON.parse(raw));
	} catch {
		parsed = null;
	}

	// Deleted either way. Best effort: a file that cannot be removed is not a
	// reason to refuse a connection that otherwise works.
	try {
		fs.unlinkSync(file);
	} catch {
		/* nothing to do about it */
	}

	if (!parsed) {
		return undefined;
	}
	// Through saveSettings, so the token is encrypted into the OS keychain by
	// the same path a typed one takes. Onboarding is marked complete because it
	// asks for exactly what the installer has already supplied.
	const saved = saveSettings({
		serverUrl: parsed.serverUrl,
		space: parsed.space,
		token: parsed.token,
	});
	setOnboardingCompleted(true);
	return saved;
}
