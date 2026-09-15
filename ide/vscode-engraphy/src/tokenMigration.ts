// Pure helper for the plaintext-token migration: given a VS Code
// `inspect('token')` result, decide WHICH value to move into the keychain and
// WHICH scopes still hold a plaintext copy that has to be cleared.
//
// Split out of tokenStore.ts (which imports `vscode`) purely so this can be
// unit-tested with plain Node. It is worth testing on its own because it is the
// one sequence in the extension where a mistake destroys a credential the user
// may not be able to re-mint: pick the wrong value and the working token is
// replaced by a stale one; miss a scope and the plaintext copy survives the
// "migration" that claimed to remove it.

/**
 * The subset of VS Code's `inspect()` result this cares about: the three scopes
 * a setting can be written at, listed least specific first. Typed structurally
 * so a real `WorkspaceConfiguration.inspect<string>()` result is assignable
 * without importing `vscode` here.
 */
export interface TokenSettingScopes {
	globalValue?: string;
	workspaceValue?: string;
	workspaceFolderValue?: string;
}

export interface TokenMigrationPlan {
	/** The token to store. Empty string when there is nothing to migrate. */
	value: string;
	/** Which scopes actually hold a value and therefore need clearing. */
	clear: Array<'global' | 'workspace' | 'workspaceFolder'>;
}

/**
 * Decide what to migrate and what to clear, given an inspect() result.
 *
 * Pure, and split out from the IO because this is the one sequence where getting
 * it wrong destroys a credential: the value picked must be the one VS Code would
 * actually resolve (most specific scope wins), and EVERY scope holding a value
 * must be cleared or the plaintext copy survives. Covered by
 * scripts/test-client.js.
 *
 * Blank and whitespace-only values are treated as absent: they are not worth
 * migrating and clearing them would be a pointless settings write.
 */
export function planTokenMigration(info: TokenSettingScopes | undefined): TokenMigrationPlan {
	const clear: TokenMigrationPlan['clear'] = [];
	if (!info) {
		return { value: '', clear };
	}
	const has = (v: string | undefined): boolean => typeof v === 'string' && v.trim().length > 0;
	if (has(info.globalValue)) {
		clear.push('global');
	}
	if (has(info.workspaceValue)) {
		clear.push('workspace');
	}
	if (has(info.workspaceFolderValue)) {
		clear.push('workspaceFolder');
	}
	if (clear.length === 0) {
		return { value: '', clear };
	}
	// Most specific scope wins, matching how VS Code resolves the setting itself.
	const value = (
		(has(info.workspaceFolderValue) ? info.workspaceFolderValue : undefined) ??
		(has(info.workspaceValue) ? info.workspaceValue : undefined) ??
		info.globalValue ??
		''
	).trim();
	return { value, clear };
}
