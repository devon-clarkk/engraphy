// Bearer-token storage.
//
// The token IS the identity on an Engraphy server: it resolves to a
// (space, principal, role). Through 0.4.0 it lived in `engraphy.token`, an
// ordinary VS Code setting, which means plaintext in settings.json, visible in
// the Settings UI, and eligible for Settings Sync, which would push a live
// credential to Microsoft's servers and onto every machine the user signs into.
//
// 0.5.0 moves it to SecretStorage (the OS keychain via VS Code) and migrates any
// existing setting on first activation.
//
// WHY A CACHE. SecretStorage is async and the connection getter is called
// synchronously from the MCP definition provider, the status bar, and both
// webview providers. Rather than make every one of those async (and rewrite the
// connection path twice in one release, since the in-flight guard already lands
// in mcpClient), the token is read once into a module-level cache and kept fresh
// by `onDidChange`. That event also fires when ANOTHER window in the same
// profile stores a token, so the cache cannot silently go stale.

import * as vscode from 'vscode';
import { planTokenMigration, type TokenMigrationPlan } from './tokenMigration';

export { planTokenMigration, type TokenMigrationPlan, type TokenSettingScopes } from './tokenMigration';

/** SecretStorage key. Same string as the old setting, for one obvious mapping. */
export const TOKEN_SECRET_KEY = 'engraphy.token';
const CONFIG_SECTION = 'engraphy';
const MIGRATED_KEY = 'engraphy.tokenMigrated.v1';

let cachedToken = '';

/** The token, synchronously. Empty string when none is stored. */
export function getToken(): string {
	return cachedToken;
}

/** Re-read the secret into the cache. Call after any write, and on change. */
export async function primeToken(context: vscode.ExtensionContext): Promise<void> {
	try {
		cachedToken = (await context.secrets.get(TOKEN_SECRET_KEY)) ?? '';
	} catch {
		// A locked or unavailable keychain must not stop the extension from
		// activating; the panels will simply report an unauthorized server.
		cachedToken = '';
	}
}

/** Store a token and refresh the cache. Throws if the keychain refuses. */
export async function setToken(context: vscode.ExtensionContext, token: string): Promise<void> {
	const trimmed = token.trim();
	if (trimmed) {
		await context.secrets.store(TOKEN_SECRET_KEY, trimmed);
	} else {
		await context.secrets.delete(TOKEN_SECRET_KEY);
	}
	await primeToken(context);
}

/** Keep the cache in step with writes from other windows in this profile. */
export function watchToken(context: vscode.ExtensionContext): vscode.Disposable {
	return context.secrets.onDidChange((e) => {
		if (e.key === TOKEN_SECRET_KEY) {
			void primeToken(context);
		}
	});
}

const TARGET_BY_SCOPE: Record<TokenMigrationPlan['clear'][number], vscode.ConfigurationTarget> = {
	global: vscode.ConfigurationTarget.Global,
	workspace: vscode.ConfigurationTarget.Workspace,
	workspaceFolder: vscode.ConfigurationTarget.WorkspaceFolder,
};

/**
 * One-time migration of a plaintext `engraphy.token` setting into SecretStorage.
 *
 * Order is load-bearing: store, read back to confirm the keychain actually kept
 * it, and only then clear the setting. Clearing first and failing to store would
 * destroy a token the user may not be able to re-mint.
 *
 * Genuinely one-time, gated on globalState. Re-running it forever would mean a
 * token later pasted into settings.json by hand (from a stale doc, say) silently
 * overwrites the working keychain token on the next activation.
 */
export async function migrateTokenSetting(
	context: vscode.ExtensionContext,
	log: vscode.OutputChannel
): Promise<void> {
	if (hasMigrated(context)) {
		return;
	}
	const cfg = vscode.workspace.getConfiguration(CONFIG_SECTION);
	const plan = planTokenMigration(cfg.inspect<string>('token'));
	if (plan.clear.length === 0) {
		await context.globalState.update(MIGRATED_KEY, true);
		return;
	}
	const value = plan.value;

	try {
		await context.secrets.store(TOKEN_SECRET_KEY, value);
		const readBack = await context.secrets.get(TOKEN_SECRET_KEY);
		if (readBack !== value) {
			throw new Error('secret did not read back as stored');
		}
	} catch (e) {
		log.appendLine(
			`token migration: could not write to SecretStorage (${String(e)}); ` +
				'leaving engraphy.token in settings.json so the connection keeps working.'
		);
		return;
	}

	for (const scope of plan.clear) {
		try {
			await cfg.update('token', undefined, TARGET_BY_SCOPE[scope]);
		} catch (e) {
			log.appendLine(`token migration: could not clear engraphy.token at ${scope} scope: ${String(e)}`);
		}
	}

	await primeToken(context);
	await context.globalState.update(MIGRATED_KEY, true);
	log.appendLine('token migration: moved engraphy.token from settings.json into SecretStorage.');
	void vscode.window.showInformationMessage(
		'Engraphy moved your token out of settings.json and into the OS keychain. ' +
			'It is no longer stored in plain text or synced by Settings Sync.'
	);
}

/** True once the plaintext setting has been dealt with at least once. */
export function hasMigrated(context: vscode.ExtensionContext): boolean {
	return context.globalState.get<boolean>(MIGRATED_KEY) === true;
}
