// Status-bar capability indicator.
//
// It answers the question a user actually reads off the bar: can my agent use
// Engraphy right now. That needs TWO independent facts, and the bar reports
// ready only when both hold.
//
// AXIS 1, can this extension read the server. Two probes, and only the
// authenticated one counts:
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
// to search, which records usage. (`stats` is safe: the handler in
// engraphy/server/tools/read.py is a pure read of the rollup and bumps no
// metric. `scope_list` stays the probe because it is cheaper and touches less.)
//
// AXIS 2, can an AGENT read the server. Entirely separate, and the reason this
// module no longer paints connected off the probes above alone: they run
// through the extension's OWN client, which proves nothing about whether the
// coding agent in the editor holds the Engraphy tools. See capability.ts.

import * as vscode from 'vscode';
import { EngraphyClient, type HealthInfo } from './mcpClient';
import { buildHealthVM, type HealthVM } from './connection';
import {
	buildCapabilityVM,
	type AgentRuntimeStatus,
	type CapabilityVM,
	type McpApiState,
	type McpPolicy,
	type ProviderSignal,
	type ReachPhase,
} from './capability';

export interface StatusConnection {
	serverUrl: string;
	token: string;
	space: string;
}

/** Everything the capability verdict needs that is not a server probe. */
export interface AgentContext {
	api: McpApiState;
	providerRegistered: boolean;
	signal: ProviderSignal;
	runtimes: AgentRuntimeStatus[];
	/** Runtime ids the user has dismissed. See CapabilityInput.ignored. */
	ignored: string[];
	/** VS Code's own MCP gates, which an organisation can set. */
	policy: McpPolicy;
}

/** Map the server-probe phase onto the capability model's reach axis. */
export function reachPhaseOf(health: HealthVM): ReachPhase {
	switch (health.phase) {
		case 'connected':
			return 'reachable';
		case 'unconfigured':
			return 'unconfigured';
		case 'unauthorized':
			return 'unauthorized';
		case 'unreachable':
			return 'unreachable';
		default:
			return 'degraded';
	}
}

export class StatusBar {
	private readonly item: vscode.StatusBarItem;
	private last: HealthVM | undefined;
	private lastCapability: CapabilityVM | undefined;

	constructor(
		private readonly client: EngraphyClient,
		private readonly getConnection: () => StatusConnection,
		private readonly getAgentContext: () => AgentContext
	) {
		this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
		this.item.command = 'engraphy.refresh';
		this.setChecking();
		this.item.show();
	}

	/** Server reachability only. Lets callers avoid a second probe. */
	get health(): HealthVM | undefined {
		return this.last;
	}

	/** The end-to-end verdict the bar is currently showing. */
	get capability(): CapabilityVM | undefined {
		return this.lastCapability;
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
				}),
				conn
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
			}),
			conn
		);
	}

	/**
	 * Paint the END-TO-END verdict, not the server probe.
	 *
	 * A reachable server whose tools no agent holds is the state that caused
	 * silent data loss: the bar read connected, the agent had nothing, and
	 * nothing on screen disagreed. It now reads "agent cannot see memory".
	 */
	private apply(health: HealthVM, conn: StatusConnection): void {
		this.last = health;
		const ctx = this.getAgentContext();
		const vm = buildCapabilityVM({
			reach: reachPhaseOf(health),
			host: hostOf(conn.serverUrl),
			reachDetail: health.detail,
			api: ctx.api,
			providerRegistered: ctx.providerRegistered,
			signal: ctx.signal,
			runtimes: ctx.runtimes,
			ignored: ctx.ignored,
			policy: ctx.policy,
		});
		this.lastCapability = vm;
		// The space label is worth keeping on a ready bar; the other phases need
		// their own words more than they need the label.
		const text = vm.phase === 'ready' && conn.space ? `Engraphy (${conn.space})` : vm.label;
		this.item.text = `${iconFor(vm.phase)} ${text}`;
		const tip = new vscode.MarkdownString();
		tip.appendMarkdown(`**${vm.title}**`);
		if (vm.detail) {
			tip.appendMarkdown(`\n\n${vm.detail}`);
		}
		if (health.version) {
			tip.appendMarkdown(`\n\nServer v${health.version}.`);
		}
		this.item.tooltip = tip;
		// `partial` is usable and still warns: some agent can reach memory, and
		// another installed one silently cannot.
		this.item.backgroundColor =
			vm.usable && vm.phase !== 'partial'
				? undefined
				: new vscode.ThemeColor(
						vm.phase === 'server-unavailable'
							? 'statusBarItem.errorBackground'
							: 'statusBarItem.warningBackground'
					);
		// Clicking a broken bar should do the thing that fixes it, not re-probe.
		this.item.command = vm.action ? vm.action.command : 'engraphy.refresh';
	}

	dispose(): void {
		this.item.dispose();
	}
}

function hostOf(serverUrl: string): string {
	try {
		return new URL(serverUrl).host;
	} catch {
		return serverUrl || 'the configured URL';
	}
}

function iconFor(phase: CapabilityVM['phase']): string {
	switch (phase) {
		case 'ready':
			return '$(loop)';
		case 'no-agent-path':
		case 'partial':
			return '$(warning)';
		case 'server-unavailable':
			return '$(debug-disconnect)';
		case 'unconfigured':
			return '$(plug)';
		default:
			return '$(loop)';
	}
}
