// Status-bar connection indicator.
//
// It runs TWO probes, and only the authenticated one can paint "connected":
//
//   /healthz    unauthenticated (engraphy/server/app.py exempts it from the
//               bearer middleware), so it answers 200 for a server you hold no
//               valid token for. Good for reachability and the version/space
//               counts, useless as proof you can read anything.
//   scope_list  authenticated, over MCP. This is what decides the indicator.
//
// 0.4.0 went green off /healthz alone, so a cold install with the default URL
// and no token showed a healthy status bar above panels that were all 401ing.
//
// scope_list is the probe on purpose: it is classified as a READ by the server
// (engraphy/server/auth.py WRITE_TOOLS), so read-only tokens can call it, and
// engraphy/core/scopes.py never imports core.metrics, so polling it cannot
// inflate the counters the Impact & usage panel reports. Do not "simplify" this
// to stats or search, both of which record usage.

import * as vscode from 'vscode';
import { EngraphyClient, type HealthInfo } from './mcpClient';
import { buildHealthVM, type HealthVM } from './connection';

export interface StatusConnection {
	serverUrl: string;
	token: string;
	space: string;
}

export class StatusBar {
	private readonly item: vscode.StatusBarItem;
	private last: HealthVM | undefined;

	constructor(
		private readonly client: EngraphyClient,
		private readonly getConnection: () => StatusConnection
	) {
		this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
		this.item.command = 'engraphy.refresh';
		this.setChecking();
		this.item.show();
	}

	/** The state the bar is currently showing. Lets callers avoid a second probe. */
	get health(): HealthVM | undefined {
		return this.last;
	}

	private setChecking(): void {
		this.item.text = '$(loop) Engraphy';
		this.item.tooltip = 'Engraphy: checking connection…';
		this.item.backgroundColor = undefined;
	}

	async refresh(): Promise<void> {
		const conn = this.getConnection();
		const configured = conn.serverUrl.length > 0;
		if (!configured) {
			this.apply(
				buildHealthVM({
					configured: false,
					hasToken: conn.token.length > 0,
					space: conn.space,
					serverUrl: conn.serverUrl,
					info: null,
					authOk: false,
				})
			);
			return;
		}

		this.setChecking();

		// Both probes run together: /healthz for the version/space detail and to
		// tell "nothing is listening" from "listening but refusing you", and
		// scope_list for the only answer that matters.
		const [healthRes, authRes] = await Promise.allSettled([
			this.client.health(),
			this.client.scopeList(),
		]);

		const info: HealthInfo | null = healthRes.status === 'fulfilled' ? healthRes.value : null;
		this.apply(
			buildHealthVM({
				configured: true,
				hasToken: conn.token.length > 0,
				space: conn.space,
				serverUrl: conn.serverUrl,
				info,
				healthzError: healthRes.status === 'rejected' ? healthRes.reason : undefined,
				authOk: authRes.status === 'fulfilled',
				authError: authRes.status === 'rejected' ? authRes.reason : undefined,
			})
		);
	}

	private apply(vm: HealthVM): void {
		this.last = vm;
		this.item.text = `${iconFor(vm.phase)} ${vm.label}`;
		this.item.tooltip = vm.detail ? `${vm.title}\n\n${vm.detail}` : vm.title;
		this.item.backgroundColor = vm.usable
			? undefined
			: new vscode.ThemeColor(
					vm.phase === 'unconfigured'
						? 'statusBarItem.warningBackground'
						: vm.phase === 'unauthorized'
							? 'statusBarItem.warningBackground'
							: 'statusBarItem.errorBackground'
				);
	}

	dispose(): void {
		this.item.dispose();
	}
}

function iconFor(phase: HealthVM['phase']): string {
	switch (phase) {
		case 'connected':
			return '$(loop)';
		case 'unauthorized':
			return '$(key)';
		case 'unreachable':
			return '$(debug-disconnect)';
		case 'degraded':
			return '$(warning)';
		case 'unconfigured':
			return '$(plug)';
		default:
			return '$(loop)';
	}
}
