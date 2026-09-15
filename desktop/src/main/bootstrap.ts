// Reading the handoff file the Windows installer leaves behind.
//
// The Docker-free Windows distribution (engraphy-public,
// docs/windows-native.md) installs the server, creates the database, creates a
// space and mints a token, all before anyone opens this app. The one thing left
// is getting that token from the installer to here, and the one thing a
// non-developer must not be asked to do is copy a bearer token between two
// windows correctly.
//
// So the installer writes `engraphy-bootstrap.json` next to this app's own
// settings file, and this app imports it on first launch: URL, space and token
// straight into the normal saved settings, which means the token goes through
// safeStorage into the OS keychain exactly as a typed one would. The file is
// deleted once it has been stored, so the plaintext lives for one launch rather
// than forever.
//
// WHY IMPORT RATHER THAN READ EVERY TIME
//
// The handoff is a starting point, not a source of truth. A user who later
// points the app at a different server, or re-mints a token, has to win over a
// file left on disk by an installer that ran months ago. Importing once and
// deleting is what makes that true, and it is why an import never overwrites
// settings the user has already saved.
//
// Pure, so scripts/test-client.js covers it: the caller does the file IO and
// the saving.

/** What the installer writes. Everything is validated; nothing is trusted. */
export interface Bootstrap {
	serverUrl: string;
	space: string;
	token: string;
}

function str(v: unknown): string | null {
	return typeof v === 'string' && v.trim().length > 0 ? v.trim() : null;
}

/**
 * Parse a handoff document, or return null when it is not one this app should
 * act on.
 *
 * The URL is restricted to loopback. This file is a local handoff from a local
 * installer, and the only server it can legitimately point at is the one on
 * this machine. A handoff naming a remote host would be a way to aim someone's
 * memory, and their bearer token, somewhere else by dropping a file in their
 * profile, so the shape is narrowed to what the feature actually needs.
 */
export function parseBootstrap(raw: unknown): Bootstrap | null {
	if (!raw || typeof raw !== 'object') {
		return null;
	}
	const doc = raw as Record<string, unknown>;
	// An unknown schema is a document written for a client this is not.
	if (doc.schema !== 1) {
		return null;
	}
	const serverUrl = str(doc.serverUrl);
	const space = str(doc.space);
	const token = str(doc.token);
	if (!serverUrl || !space || !token) {
		return null;
	}
	if (!isLoopbackHttpUrl(serverUrl)) {
		return null;
	}
	return { serverUrl, space, token };
}

/** http(s) on this machine, and nothing else. */
export function isLoopbackHttpUrl(raw: string): boolean {
	let url: URL;
	try {
		url = new URL(raw);
	} catch {
		return false;
	}
	if (url.protocol !== 'http:' && url.protocol !== 'https:') {
		return false;
	}
	const host = url.hostname.toLowerCase();
	// WHATWG `hostname` keeps the brackets on an IPv6 literal, so the loopback
	// address is '[::1]' here rather than '::1'.
	return host === '127.0.0.1' || host === 'localhost' || host === '[::1]';
}

/**
 * Whether an import should happen at all.
 *
 * Only onto an app that has no token yet. A second install, a repair, or a
 * reinstall over an existing profile must not silently replace credentials the
 * user is already using, and "has a token" is the signal that they are.
 */
export function shouldImport(hasToken: boolean, onboardingCompleted: boolean): boolean {
	return !hasToken && !onboardingCompleted;
}
