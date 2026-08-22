// Status-bar connection indicator. Polls the unauthenticated /healthz and shows
// reachable / offline, with the space label from settings.

import * as vscode from 'vscode';
import { EngraphyClient } from './mcpClient';

export class StatusBar {
	private readonly item: vscode.StatusBarItem;

	constructor(
		private readonly client: EngraphyClient,
		private readonly getSpace: () => string
	) {
		this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
		this.item.command = 'engraphy.refresh';
		this.setUnknown();
		this.item.show();
	}

	private setUnknown(): void {
		this.item.text = '$(loop) Engraphy';
		this.item.tooltip = 'Engraphy — checking connection…';
		this.item.backgroundColor = undefined;
	}

	async refresh(): Promise<void> {
		const space = this.getSpace();
		const suffix = space ? ` (${space})` : '';
		try {
			const h = await this.client.health();
			this.item.text = `$(loop) Engraphy${suffix}`;
			this.item.tooltip = `Engraphy server reachable — ${h.status}, v${h.version ?? '?'}, ${h.spaces ?? '?'} spaces`;
			this.item.backgroundColor = undefined;
		} catch (e) {
			this.item.text = `$(warning) Engraphy${suffix}`;
			this.item.tooltip = `Engraphy server unreachable: ${String(e)}\nClick to retry.`;
			this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
		}
	}

	dispose(): void {
		this.item.dispose();
	}
}
