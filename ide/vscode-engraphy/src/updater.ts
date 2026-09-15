// The VS Code side of update checking: when to look, what to say, and what the
// buttons do. The comparison and the manifest shape live in versionCheck.ts,
// which is pure and covered by scripts/test-client.js.
//
// WHAT THIS IS FOR
//
// An extension installed from the VS Code Marketplace is kept current by the
// editor, and this check simply agrees with it. The install this exists for is
// the one no registry updates: an editor whose gallery is Open VSX, where the
// extension is installed from a .vsix and stays at whatever version was
// downloaded. That install has no other way to learn a newer version exists.
//
// It also covers a gallery install with `extensions.autoUpdate` turned off,
// where the editor knows about the update and is deliberately not applying it.
//
// HOW IT BEHAVES
//
// Once a day at most, and only after the editor has settled. Silent when it
// cannot reach the manifest, silent when the running version is current, and
// silent for a version the user has already dismissed. There is no repeat
// prompt for the same version, and running a newer build than the published one
// never reads as out of date.

import * as vscode from 'vscode';
import {
	CHECK_INTERVAL_MS,
	DEFAULT_MANIFEST_URL,
	PRODUCT_KEY,
	checkForUpdate,
	isDismissed,
	pickDownload,
	shouldCheck,
	type Fetcher,
	type UpdateVerdict,
} from './versionCheck';

const EXTENSION_ID = 'engraphy.engraphy';
const LAST_CHECKED_KEY = 'engraphy.update.lastChecked.v1';
const DISMISSED_KEY = 'engraphy.update.dismissed.v1';

/** Give the editor its startup window before spending anything on this. */
const STARTUP_DELAY_MS = 20_000;

/**
 * Node's fetch, typed to the narrow shape versionCheck asks for. Reaching for
 * the global rather than a dependency keeps the packaged extension to the one
 * bundled file it already ships.
 */
const nodeFetch = (globalThis as { fetch?: unknown }).fetch as Fetcher | undefined;

function config(): vscode.WorkspaceConfiguration {
	return vscode.workspace.getConfiguration('engraphy');
}

function enabled(): boolean {
	return config().get<boolean>('updateCheck.enabled') !== false;
}

function manifestUrl(): string {
	const raw = (config().get<string>('updateCheck.manifestUrl') ?? '').trim();
	return raw.length > 0 ? raw : DEFAULT_MANIFEST_URL;
}

/**
 * Whether the editor is set to apply extension updates on its own.
 *
 * `false` is the only value that means off. The two `onlySelected…` strings
 * still update, just on a narrower set, so treating any non-true value as off
 * would tell a working editor it is not working.
 */
function autoUpdateOff(): boolean {
	try {
		return vscode.workspace.getConfiguration('extensions').get<unknown>('autoUpdate') === false;
	} catch {
		return false;
	}
}

export interface UpdateNotice {
	verdict: UpdateVerdict;
	/** One line for the status-bar tooltip, or null when there is nothing to say. */
	summary: string | null;
}

export class Updater implements vscode.Disposable {
	private timer: ReturnType<typeof setTimeout> | undefined;
	private latest: UpdateVerdict | undefined;
	private readonly onNotice: () => void;

	constructor(
		private readonly context: vscode.ExtensionContext,
		private readonly currentVersion: string,
		private readonly output: vscode.OutputChannel,
		onNotice: () => void
	) {
		this.onNotice = onNotice;
	}

	/** The pending verdict, for surfaces that paint it. Undefined until a check runs. */
	get pending(): UpdateVerdict | undefined {
		return this.latest && (this.latest.state === 'update' || this.latest.state === 'unsupported')
			? this.latest
			: undefined;
	}

	/** One line naming both versions, for the status-bar tooltip. */
	get summary(): string | null {
		const v = this.pending;
		if (!v) {
			return null;
		}
		return `Engraphy ${v.latest} is available. This editor is running ${v.current}.`;
	}

	/**
	 * Arrange the background check. Returns immediately: activation never waits
	 * on the network, and a check that never completes costs nothing.
	 */
	start(): void {
		if (!enabled()) {
			return;
		}
		this.timer = setTimeout(() => {
			void this.runScheduled();
		}, STARTUP_DELAY_MS);
	}

	private async runScheduled(): Promise<void> {
		if (!enabled()) {
			return;
		}
		const last = this.context.globalState.get<number>(LAST_CHECKED_KEY);
		if (!shouldCheck(last, Date.now(), CHECK_INTERVAL_MS)) {
			return;
		}
		await this.run(false);
	}

	/**
	 * Run a check. `manual` reports every outcome, including "you are current",
	 * because a command the user invoked has to answer. The scheduled path stays
	 * silent unless there is something to act on.
	 */
	async run(manual: boolean): Promise<void> {
		if (!nodeFetch) {
			if (manual) {
				void vscode.window.showInformationMessage(
					'Engraphy: this editor build has no fetch available, so the update check cannot run.'
				);
			}
			return;
		}
		const verdict = await checkForUpdate(
			this.currentVersion,
			manifestUrl(),
			nodeFetch,
			PRODUCT_KEY
		);
		this.latest = verdict;
		await this.context.globalState.update(LAST_CHECKED_KEY, Date.now());
		this.output.appendLine(
			`Update check: running ${verdict.current}, published ${verdict.latest ?? 'unknown'} (${verdict.state}).`
		);
		this.onNotice();

		if (verdict.state === 'update' || verdict.state === 'unsupported') {
			const dismissed = this.context.globalState.get<string>(DISMISSED_KEY);
			if (!manual && isDismissed(dismissed, verdict.latest)) {
				return;
			}
			await this.prompt(verdict);
			return;
		}
		if (!manual) {
			return;
		}
		if (verdict.state === 'current') {
			void vscode.window.showInformationMessage(
				`Engraphy ${verdict.current} is the current version.`
			);
		} else if (verdict.state === 'ahead') {
			void vscode.window.showInformationMessage(
				`Engraphy ${verdict.current} is ahead of the published ${verdict.latest}.`
			);
		} else {
			void vscode.window.showInformationMessage(
				'Engraphy: no update information is available right now.'
			);
		}
	}

	/** The notification, and the ladder behind its buttons. */
	private async prompt(v: UpdateVerdict): Promise<void> {
		const headline =
			v.state === 'unsupported'
				? `Engraphy ${v.latest} is available. This editor is running ${v.current}, which is below the oldest supported version.`
				: `Engraphy ${v.latest} is available. This editor is running ${v.current}.`;
		const autoOff = autoUpdateOff();
		const message = autoOff
			? `${headline} Extension auto-update is turned off in this editor.`
			: headline;

		const UPDATE = 'Update';
		const NOTES = 'Release notes';
		const LATER = 'Not now';
		const buttons = [UPDATE, ...(v.notes ? [NOTES] : []), LATER];

		const picked = await vscode.window.showInformationMessage(message, ...buttons);
		if (picked === NOTES && v.notes) {
			await vscode.env.openExternal(vscode.Uri.parse(v.notes));
			return;
		}
		if (picked === LATER || picked === undefined) {
			// Per version, so this is "not this one", never "not ever".
			if (v.latest) {
				await this.context.globalState.update(DISMISSED_KEY, v.latest);
			}
			return;
		}
		await this.applyUpdate(v);
	}

	/**
	 * Take the user as far as this editor allows, in order of how little it asks
	 * of them.
	 *
	 * 1. Install from the editor's own gallery. On VS Code that reaches the
	 *    Marketplace and finishes the job. On an editor whose gallery is
	 *    Open VSX the extension is not published there, so this rung cannot
	 *    deliver, and the next one runs.
	 * 2. The extension's page in the editor, where Update is one click.
	 * 3. The .vsix on the release, for an editor with no gallery path at all.
	 * 4. The release notes, which always exist.
	 */
	private async applyUpdate(v: UpdateVerdict): Promise<void> {
		try {
			await vscode.commands.executeCommand('workbench.extensions.installExtension', EXTENSION_ID);
			// The command resolving is not proof the install happened: what a host
			// does with an id its gallery does not carry is the host's business,
			// and it is not required to reject. So the prompt claims nothing about
			// an install and offers only the step that is useful either way. A
			// reload with nothing new to load costs the user a second; being told
			// an update landed when it did not costs them the update.
			const RELOAD = 'Reload window';
			const SHOW = 'Show the extension';
			const picked = await vscode.window.showInformationMessage(
				`Reload the window to pick up Engraphy ${v.latest} once it is installed.`,
				RELOAD,
				SHOW
			);
			if (picked === RELOAD) {
				await vscode.commands.executeCommand('workbench.action.reloadWindow');
				return;
			}
			if (picked !== SHOW) {
				return;
			}
			// Falls through to the next rung, which is the point of offering it:
			// an editor that took the command and did nothing leaves the user
			// here, and they still need somewhere to go.
		} catch (e) {
			this.output.appendLine(`Gallery install was not available: ${String(e)}`);
		}

		try {
			await vscode.commands.executeCommand('workbench.extensions.search', `@id:${EXTENSION_ID}`);
			return;
		} catch (e) {
			this.output.appendLine(`Could not open the extensions view: ${String(e)}`);
		}

		const vsix = pickDownload(v.downloads, process.platform, process.arch);
		const target =
			vsix?.url ??
			v.registries['vscode-marketplace'] ??
			v.registries['open-vsx'] ??
			v.notes;
		if (target) {
			await vscode.env.openExternal(vscode.Uri.parse(target));
		}
	}

	dispose(): void {
		if (this.timer) {
			clearTimeout(this.timer);
		}
	}
}
