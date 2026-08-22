// Confirm-write queue — a branded WebviewView (replaces the old TreeView). Two
// bands, same server semantics as before:
//
//  • Pending duplicates — writes the server parked as needs_confirmation
//    (`pending_list`). Each card shows the payload preview NEXT TO the candidate
//    node(s) it collided with (title + similarity meter). Approve keeps it
//    distinct; Merge folds it into a chosen candidate (both via
//    resolve_duplicate). The server does NOT filter expired rows — an expired
//    card is greyed and its actions are still offered, because the server is the
//    authority that refuses a stale resolve (ENGRAPHY_PENDING_EXPIRED).
//
//  • Inbox — captured items awaiting triage (inbox_review). Promote is authoring
//    (native input flow, payload shown read-only); Discard drops the item.
//
// Rendering + message passing live here; the pure card/message logic is in
// webviewMessages.ts. THEME + BRAND styling is in media/confirm.css.

import * as vscode from 'vscode';
import { EngraphyClient } from './mcpClient';
import {
	EngraphyToolError,
	inboxItemsFrom,
	pendingItemsFrom,
	type InboxItemData,
	type PendingListItem,
} from './toolResult';
import {
	buildInboxCard,
	buildPendingCard,
	parseWebviewMessage,
	themeKindFromEnum,
	type ConfirmStateVM,
	type ConnectionVM,
	type HostToWebview,
} from './webviewMessages';
import { computeConnection, describeError, hostLabel } from './connection';

const INBOX_LIMIT = 50;
const PENDING_LIMIT = 50;

export class ConfirmWebviewProvider implements vscode.WebviewViewProvider {
	public static readonly viewId = 'engraphyConfirm';

	private view: vscode.WebviewView | undefined;
	private readonly disposables: vscode.Disposable[] = [];

	private pending: PendingListItem[] = [];
	private inbox: InboxItemData[] = [];
	private inboxTruncated = false;
	private pendingError: string | null = null;
	private inboxError: string | null = null;
	// The RAW thrown values, kept alongside the display strings: the connection
	// classifier reads `code` and the `cause` chain off the error object, which a
	// String()-ified message has already thrown away.
	private pendingRaw: unknown = null;
	private inboxRaw: unknown = null;
	private loading = false;
	/** True on the very first load, so the panel can show skeletons not a spinner. */
	private firstLoad = true;

	constructor(
		private readonly extensionUri: vscode.Uri,
		private readonly client: EngraphyClient,
		private readonly log: vscode.OutputChannel,
		/** Native authoring flow for an inbox item (payload shown read-only). */
		private readonly promoteItem: (item: InboxItemData) => Promise<void>,
		/** Current serverUrl + whether a token is stored, for the recovery copy. */
		private readonly getConnectionInfo: () => { serverUrl: string; hasToken: boolean }
	) {}

	resolveWebviewView(webviewView: vscode.WebviewView): void {
		this.view = webviewView;
		webviewView.webview.options = {
			enableScripts: true,
			localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'media')],
		};
		webviewView.webview.html = this.getHtml(webviewView.webview);

		webviewView.webview.onDidReceiveMessage(
			(raw) => void this.onMessage(raw),
			undefined,
			this.disposables
		);

		// Live theme reactions: CSS follows the injected vscode-* class/vars
		// automatically, but we also push the kind so the webview can react in JS.
		this.disposables.push(
			vscode.window.onDidChangeActiveColorTheme((t) => this.postTheme(t.kind))
		);

		webviewView.onDidDispose(() => this.dispose(), undefined, this.disposables);
	}

	/** External refresh (Refresh command, settings change, initial paint). */
	async refresh(): Promise<void> {
		await this.reload();
	}

	// ---- host → webview -----------------------------------------------------

	private post(msg: HostToWebview): void {
		void this.view?.webview.postMessage(msg);
	}

	private postTheme(kind: vscode.ColorThemeKind): void {
		this.post({ type: 'theme', kind: themeKindFromEnum(kind) });
	}

	private buildState(): ConfirmStateVM {
		const info = this.getConnectionInfo();
		const host = hostLabel(info.serverUrl);
		const noServer = computeConnection({
			serverConfigured: info.serverUrl.length > 0,
			errors: [this.pendingRaw, this.inboxRaw],
			host,
			hasToken: info.hasToken,
		});
		const connection: ConnectionVM = noServer ? { kind: 'no-server', ...noServer } : { kind: 'ok' };
		return {
			connection,
			pending: this.pending.map((p) => buildPendingCard(p)),
			inbox: this.inbox.map(buildInboxCard),
			// Per-band errors are shown inline only when the panel as a whole is fine;
			// the summary is friendlier than the raw transport string.
			pendingError: this.pendingRaw ? describeError(this.pendingRaw, host).summary : null,
			inboxError: this.inboxRaw ? describeError(this.inboxRaw, host).summary : null,
			inboxTruncated: this.inboxTruncated,
			loading: this.loading,
			firstLoad: this.firstLoad,
		};
	}

	private postState(): void {
		this.post({ type: 'state', state: this.buildState() });
	}

	// ---- read (re-read both bands, then re-render) --------------------------

	async reload(): Promise<void> {
		if (!this.view) {
			return;
		}
		this.loading = true;
		this.postState();
		await Promise.all([this.reloadPending(), this.reloadInbox()]);
		this.loading = false;
		this.firstLoad = false;
		this.postState();
		if (vscode.window.activeColorTheme) {
			this.postTheme(vscode.window.activeColorTheme.kind);
		}
	}

	private async reloadPending(): Promise<void> {
		try {
			const res = await this.client.pendingList(PENDING_LIMIT);
			this.pending = pendingItemsFrom(res);
			this.pendingError = null;
			this.pendingRaw = null;
		} catch (e) {
			this.pending = [];
			this.pendingError = this.msg(e);
			this.pendingRaw = e;
			this.log.appendLine(`pending_list failed: ${this.pendingError}`);
		}
	}

	private async reloadInbox(): Promise<void> {
		try {
			const res = await this.client.inboxList(INBOX_LIMIT);
			const { items, truncated } = inboxItemsFrom(res);
			this.inbox = items;
			this.inboxTruncated = truncated;
			this.inboxError = null;
			this.inboxRaw = null;
		} catch (e) {
			this.inbox = [];
			this.inboxError = this.msg(e);
			this.inboxRaw = e;
			this.log.appendLine(`inbox list failed: ${this.inboxError}`);
		}
	}

	// ---- webview → host -----------------------------------------------------

	private async onMessage(raw: unknown): Promise<void> {
		const msg = parseWebviewMessage(raw);
		if (!msg) {
			this.log.appendLine(`ignored malformed webview message: ${safe(raw)}`);
			// Re-sync the webview so its optimistic "busy" disable is cleared.
			this.postState();
			return;
		}
		switch (msg.type) {
			case 'ready':
				this.postTheme(vscode.window.activeColorTheme.kind);
				await this.reload();
				return;
			case 'refresh':
				await this.reload();
				return;
			case 'approve':
				await this.resolve(msg.pendingId, 'distinct');
				return;
			case 'merge':
				await this.resolve(msg.pendingId, 'merge', msg.mergeInto);
				return;
			case 'promote':
				await this.onPromote(msg.inboxId);
				return;
			case 'discard':
				await this.onDiscard(msg.inboxId);
				return;
			case 'openWalkthrough':
				await vscode.commands.executeCommand('engraphy.gettingStarted');
				this.postState(); // clear the webview's optimistic busy state
				return;
			case 'configureServer':
				await vscode.commands.executeCommand('engraphy.configureServer');
				this.postState();
				return;
			case 'reconnect':
				await vscode.commands.executeCommand('engraphy.reconnect');
				this.postState();
				return;
		}
	}

	private async resolve(
		pendingId: string,
		resolution: 'distinct' | 'merge',
		mergeInto?: string
	): Promise<void> {
		try {
			await this.client.resolveDuplicate(pendingId, resolution, mergeInto);
			void vscode.window.showInformationMessage(
				`Engraphy: pending write resolved as ${resolution}.`
			);
		} catch (e) {
			if (e instanceof EngraphyToolError && e.code === 'ENGRAPHY_PENDING_EXPIRED') {
				void vscode.window.showWarningMessage(
					'Engraphy: that pending write has expired — it can no longer be resolved. ' +
						'Re-issue the write to get a fresh confirmation window.'
				);
			} else {
				void vscode.window.showErrorMessage(`Engraphy: ${this.msg(e)}`);
			}
			this.log.appendLine(`resolve_duplicate(${resolution}) failed: ${this.msg(e)}`);
		} finally {
			// Always re-read: on success the row is gone; on failure the list still
			// reflects server truth (e.g. the expired row stays, now visibly stale).
			await this.reload();
		}
	}

	private async onPromote(inboxId: string): Promise<void> {
		const item = this.inbox.find((i) => i.id === inboxId);
		if (!item) {
			await this.reload();
			return;
		}
		try {
			await this.promoteItem(item);
		} catch (e) {
			void vscode.window.showErrorMessage(`Engraphy: ${this.msg(e)}`);
			this.log.appendLine(`promote failed: ${this.msg(e)}`);
		} finally {
			await this.reload();
		}
	}

	private async onDiscard(inboxId: string): Promise<void> {
		const item = this.inbox.find((i) => i.id === inboxId);
		const ok = await vscode.window.showWarningMessage(
			`Discard inbox item "${item ? previewLabel(item) : inboxId}"?`,
			{ modal: true },
			'Discard'
		);
		if (ok !== 'Discard') {
			// User cancelled — re-sync to clear the webview's optimistic busy state.
			this.postState();
			return;
		}
		try {
			await this.client.inboxDiscard(inboxId);
			void vscode.window.showInformationMessage('Engraphy: item discarded.');
		} catch (e) {
			void vscode.window.showErrorMessage(`Engraphy: ${this.msg(e)}`);
			this.log.appendLine(`discard failed: ${this.msg(e)}`);
		} finally {
			await this.reload();
		}
	}

	private msg(e: unknown): string {
		return e instanceof EngraphyToolError ? `${e.code}: ${e.message}` : String(e);
	}

	// ---- HTML (CSP + nonce + asWebviewUri) ----------------------------------

	private getHtml(webview: vscode.Webview): string {
		const nonce = makeNonce();
		const asset = (name: string): vscode.Uri =>
			webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'media', name));
		const scriptUri = asset('confirm.js');
		const statesUri = asset('states.js'); // skeletons / empty / error / recovery
		const styleUri = asset('confirm.css');
		const statesCss = asset('states.css');
		const markUri = asset('loop-mark.svg');
		const csp = [
			`default-src 'none'`,
			`img-src ${webview.cspSource} data:`,
			`style-src ${webview.cspSource} 'unsafe-inline'`,
			`font-src ${webview.cspSource}`,
			`script-src 'nonce-${nonce}'`,
		].join('; ');

		return `<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8" />
	<meta http-equiv="Content-Security-Policy" content="${csp}" />
	<meta name="viewport" content="width=device-width, initial-scale=1.0" />
	<link href="${styleUri}" rel="stylesheet" />
	<link href="${statesCss}" rel="stylesheet" />
	<title>Engraphy — Confirm-write queue</title>
</head>
<body>
	<header class="header">
		<div id="mark" class="mark" data-mark-uri="${markUri}" aria-hidden="true"></div>
		<div class="titles">
			<span class="wordmark brand-mono">Engraphy</span>
			<span class="subtitle">Confirm-write queue</span>
		</div>
		<div class="header-actions">
			<button id="refresh" class="btn btn-ghost" title="Re-read the queue">↻ Refresh</button>
		</div>
	</header>
	<main id="root" aria-live="polite"></main>
	<script nonce="${nonce}" src="${statesUri}"></script>
	<script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
	}

	dispose(): void {
		while (this.disposables.length) {
			try {
				this.disposables.pop()?.dispose();
			} catch {
				// best-effort
			}
		}
		this.view = undefined;
	}
}

function previewLabel(item: InboxItemData): string {
	const p = item.payload;
	if (typeof p === 'string') {
		return p.slice(0, 40);
	}
	if (p && typeof p === 'object') {
		const o = p as Record<string, unknown>;
		for (const k of ['title', 'text', 'message', 'summary']) {
			if (typeof o[k] === 'string') {
				return (o[k] as string).slice(0, 40);
			}
		}
	}
	return item.kind;
}

function safe(v: unknown): string {
	try {
		return JSON.stringify(v);
	} catch {
		return String(v);
	}
}

function makeNonce(): string {
	const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
	let out = '';
	for (let i = 0; i < 32; i++) {
		out += chars.charAt(Math.floor(Math.random() * chars.length));
	}
	return out;
}
