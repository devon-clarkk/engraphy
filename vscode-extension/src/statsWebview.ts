// Stats panel — a branded WebviewView that visualizes the server's `stats` tool:
// the value-proof dashboard. Same theme-adaptive + brand approach as the
// confirm-queue webview (it even links media/confirm.css for the shared brand
// base, buttons, loop mark, and the no-server onboarding block), plus a
// stats-specific stylesheet for tiles + sparklines.
//
// Rendering + message passing live here; the pure view-model, answer-rate,
// sparkline scaling, scope labels, and message parsing are in statsModel.ts.

import * as vscode from 'vscode';
import { EngraphyClient } from './mcpClient';
import { EngraphyToolError } from './toolResult';
import { themeKindFromEnum } from './webviewMessages';
import {
	buildStatsView,
	parseStatsMessage,
	statsConnectionState,
	type StatsHostToWebview,
	type StatsStateVM,
	type StatsViewVM,
} from './statsModel';

const DEFAULT_RANGE = 30;

export class StatsWebviewProvider implements vscode.WebviewViewProvider {
	public static readonly viewId = 'engraphyStats';

	private view: vscode.WebviewView | undefined;
	private readonly disposables: vscode.Disposable[] = [];

	private groupBy: 'space' | 'user' = 'space';
	private rangeDays = DEFAULT_RANGE;
	private vm: StatsViewVM | null = null;
	private error: string | null = null;
	private loading = false;

	constructor(
		private readonly extensionUri: vscode.Uri,
		private readonly client: EngraphyClient,
		private readonly log: vscode.OutputChannel,
		private readonly getServerConfigured: () => boolean
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
		this.disposables.push(
			vscode.window.onDidChangeActiveColorTheme((t) => this.postTheme(t.kind))
		);
		webviewView.onDidDispose(() => this.dispose(), undefined, this.disposables);
	}

	/** External refresh (Refresh command, settings change, initial paint). */
	async refresh(): Promise<void> {
		await this.reload();
	}

	private post(msg: StatsHostToWebview): void {
		void this.view?.webview.postMessage(msg);
	}
	private postTheme(kind: vscode.ColorThemeKind): void {
		this.post({ type: 'theme', kind: themeKindFromEnum(kind) });
	}

	private buildState(): StatsStateVM {
		return {
			connection: statsConnectionState(this.getServerConfigured(), this.error),
			loading: this.loading,
			error: this.error,
			groupBy: this.groupBy,
			rangeDays: this.rangeDays,
			view: this.vm,
		};
	}
	private postState(): void {
		this.post({ type: 'state', state: this.buildState() });
	}

	async reload(): Promise<void> {
		if (!this.view) {
			return;
		}
		this.loading = true;
		this.postState();
		try {
			const res = await this.client.stats(this.rangeDays, this.groupBy);
			this.vm = buildStatsView(res);
			this.error = null;
		} catch (e) {
			this.vm = null;
			this.error = this.msg(e);
			this.log.appendLine(`stats failed: ${this.error}`);
		}
		this.loading = false;
		this.postState();
		if (vscode.window.activeColorTheme) {
			this.postTheme(vscode.window.activeColorTheme.kind);
		}
	}

	private async onMessage(raw: unknown): Promise<void> {
		const msg = parseStatsMessage(raw);
		if (!msg) {
			this.log.appendLine(`ignored malformed stats message: ${safe(raw)}`);
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
			case 'setRange':
				this.rangeDays = msg.rangeDays;
				await this.reload();
				return;
			case 'setGroup':
				this.groupBy = msg.groupBy;
				await this.reload();
				return;
			case 'openWalkthrough':
				await vscode.commands.executeCommand('engraphy.gettingStarted');
				this.postState();
				return;
			case 'configureServer':
				await vscode.commands.executeCommand('engraphy.configureServer');
				this.postState();
				return;
		}
	}

	private msg(e: unknown): string {
		return e instanceof EngraphyToolError ? `${e.code}: ${e.message}` : String(e);
	}

	private getHtml(webview: vscode.Webview): string {
		const nonce = makeNonce();
		const asset = (name: string): vscode.Uri =>
			webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'media', name));
		const confirmCss = asset('confirm.css'); // shared brand/theme base + no-server block
		const statsCss = asset('stats.css');
		const scriptUri = asset('stats.js');
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
	<link href="${confirmCss}" rel="stylesheet" />
	<link href="${statsCss}" rel="stylesheet" />
	<title>Engraphy — Stats</title>
</head>
<body>
	<header class="header">
		<div id="mark" class="mark" data-mark-uri="${markUri}" aria-hidden="true"></div>
		<div class="titles">
			<span class="wordmark brand-mono">Engraphy</span>
			<span class="subtitle">Impact &amp; usage</span>
		</div>
		<div class="header-actions">
			<button id="refresh" class="btn btn-ghost" title="Re-read stats">↻ Refresh</button>
		</div>
	</header>
	<main id="root" aria-live="polite"></main>
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
