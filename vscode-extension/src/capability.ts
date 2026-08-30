// End-to-end capability model: can an AI agent on this machine actually reach
// Engraphy's memory right now.
//
// WHY THIS EXISTS. The status bar used to answer a narrower question than the
// one a user reads off it. `status.ts` paints "connected" from `authOk`, which
// is the EXTENSION'S OWN MCP client calling `scope_list` over HTTP. That proves
// exactly one thing: this extension process can reach the server. It says
// nothing about whether the coding agent in the editor holds the Engraphy
// tools, and those are two independent facts:
//
//   * The extension reaches the server through its own SDK client (mcpClient.ts).
//   * The agent reaches the server through whatever MCP registry THAT agent
//     reads. For Copilot Chat that is VS Code's registry, fed by
//     `vscode.lm.registerMcpServerDefinitionProvider`. For Claude Code, Cursor
//     and every other third-party agent it is that agent's OWN config file,
//     which the VS Code API does not touch at all.
//
// So a green bar above an agent with no Engraphy tools is not an edge case, it
// is the expected reading whenever the agent is not Copilot Chat. This module
// keeps the two axes separate and reports ready only when BOTH hold.
//
// Pure: no vscode, no SDK, no DOM imports, so scripts/test-client.js covers it.

/** Is `vscode.lm.registerMcpServerDefinitionProvider` present in this build. */
export type McpApiState = 'available' | 'missing';

/**
 * Evidence that VS Code actually CONSUMED the registered provider.
 *
 * `provideMcpServerDefinitions` is a callback the extension owns, so the only
 * certain proof that VS Code asked for the definitions is having been asked.
 * The API contract says the editor invokes providers when a chat message is
 * submitted, so in an editor with no Copilot Chat traffic the provider is
 * registered and never called, and the Engraphy server never enters VS Code's
 * MCP list. Without this record that state is indistinguishable from success.
 *
 * Persisted in globalState, so it is sticky across sessions on purpose: it
 * answers "has anything on this machine ever consumed the registry", which is
 * the question that matters.
 */
export interface ProviderSignal {
	/** ISO timestamp of the last time VS Code asked for definitions. */
	lastProvidedAt?: string;
	/** How many definitions the last call returned. 0 means one was withheld. */
	lastCount?: number;
	/** Total calls seen across all sessions. */
	calls?: number;
}

/** One coding agent that could hold the Engraphy tools. */
export interface AgentRuntimeStatus {
	id: string;
	label: string;
	/** The runtime is present on this machine. */
	detected: boolean;
	/** Engraphy is registered in the place THIS runtime reads. */
	registered: boolean;
	/** Where that registration lives, for the "where do I look" line. */
	configPath?: string;
	/**
	 * True for runtimes fed by VS Code's own MCP registry rather than a file.
	 * For those, `registered` is driven by the ProviderSignal, not by a config.
	 */
	viaVsCodeRegistry?: boolean;
	/** Whether the extension can write this runtime's config for the user. */
	writable?: boolean;
}

/** Whether the EXTENSION's own client can read from the server. */
export type ReachPhase =
	| 'unconfigured'
	| 'unreachable'
	| 'unauthorized'
	| 'degraded'
	| 'reachable';

export type CapabilityPhase =
	/** No server URL set yet. */
	| 'unconfigured'
	/** The server itself is not answering, or refused the token. */
	| 'server-unavailable'
	/** Server is fine, but no agent on this machine can see it. */
	| 'no-agent-path'
	/**
	 * Some agent holds the tools and another installed one does not.
	 *
	 * This phase is not a nicety. "At least one path is live" was the first rule
	 * tried here and it re-created the very bug this module exists to prevent:
	 * the VS Code signal is sticky across sessions, so on any machine where
	 * Copilot Chat had ever submitted a message the VS Code entry counted as
	 * registered forever, and an unregistered Claude Code sat beside it under a
	 * green bar. Aggregating the runtimes with `some` hides exactly the agent
	 * the user is typing into.
	 */
	| 'partial'
	/** Server is fine and every detected runtime holds the tools. */
	| 'ready';

export interface CapabilityVM {
	phase: CapabilityPhase;
	/** Status-bar text, without the icon. */
	label: string;
	/** Tooltip first line. */
	title: string;
	/** Longer explanation of what is missing and what to do about it. */
	detail?: string;
	/**
	 * True only when the server is reachable AND some agent can actually call
	 * it. This is the only flag the UI may render as connected.
	 */
	usable: boolean;
	/** The runtimes considered, for the panel list. */
	runtimes: AgentRuntimeStatus[];
	/** The single most useful next step, offered as a button. */
	action?: { command: string; title: string };
}

export interface CapabilityInput {
	reach: ReachPhase;
	/** Host label for the copy. */
	host: string;
	/** Raw reach error detail, passed through to the tooltip. */
	reachDetail?: string;
	api: McpApiState;
	/** False when registerMcpProvider bailed out or the provider threw. */
	providerRegistered: boolean;
	signal: ProviderSignal;
	runtimes: AgentRuntimeStatus[];
	/**
	 * Runtime ids the user has said they do not use. Detection is a heuristic
	 * (a leftover `~/.cursor` means Cursor was installed once, not that anyone
	 * runs it), so without this the `partial` phase would nag forever about an
	 * agent nobody opens. Dismissing is per-runtime and reversible: registering
	 * a runtime clears its entry.
	 */
	ignored?: string[];
	/** VS Code's own MCP gates. See McpPolicy. */
	policy?: McpPolicy;
}

/**
 * The settings VS Code uses to gate MCP, any of which an organisation can set
 * rather than the user.
 *
 * These matter because they produce the SAME observable state as "nothing
 * consumes the registry": the extension registers, the provider is offered, and
 * the server still never reaches the MCP list. On a managed machine that is a
 * likelier explanation than an editor with no Copilot Chat traffic, so the
 * warning copy must not assert the latter as though it were the only reading.
 *
 * Read from `chat.mcp.*`. Undefined means the setting was not readable, which
 * is not the same as off.
 */
export interface McpPolicy {
	/** `chat.mcp.enabled`. False disables MCP outright. */
	enabled?: boolean;
	/** `chat.mcp.access`. A restrictive value limits which servers load. */
	access?: string;
	/** `chat.mcp.allowManagedServersOnly`. True filters out every unmanaged server. */
	allowManagedServersOnly?: boolean;
	/** True when `chat.mcp.deniedServers` names Engraphy. */
	denied?: boolean;
}

/**
 * The gates known to be restricting MCP, in user-facing words. Empty when
 * nothing readable is restricting it, which is NOT proof that nothing is.
 */
export function policyRestrictions(p: McpPolicy | undefined): string[] {
	if (!p) {
		return [];
	}
	const out: string[] = [];
	if (p.enabled === false) {
		out.push('"chat.mcp.enabled" is off, which disables MCP entirely');
	}
	if (p.allowManagedServersOnly === true) {
		out.push(
			'"chat.mcp.allowManagedServersOnly" is on, so only servers your organisation manages are loaded and Engraphy is filtered out'
		);
	}
	if (p.denied === true) {
		out.push('"chat.mcp.deniedServers" names Engraphy');
	}
	if (p.access === 'none') {
		out.push('"chat.mcp.access" is set to "none", which blocks every MCP server');
	} else if (p.access && p.access !== 'all') {
		out.push('"chat.mcp.access" is set to "' + p.access + '"');
	}
	return out;
}

export const REGISTER_COMMAND = 'engraphy.registerWithAgent';
export const CONNECT_COMMAND = 'engraphy.configureServer';

/**
 * True when VS Code has demonstrably consumed the provider AND was handed a
 * definition. A call that returned zero definitions is NOT consumption: that is
 * the empty-URL or malformed-URL path, where the registry asked and got
 * nothing, which is exactly the silent case this whole module exists to name.
 */
export function vsCodeRegistryLive(
	api: McpApiState,
	registered: boolean,
	s: ProviderSignal
): boolean {
	return api === 'available' && registered && (s.calls ?? 0) > 0 && (s.lastCount ?? 0) > 0;
}

/**
 * Fold the VS Code registry into the runtime list as one more entry, so the
 * panel and the verdict see a single uniform list of ways an agent could hold
 * the tools.
 */
export function withVsCodeRuntime(
	runtimes: AgentRuntimeStatus[],
	api: McpApiState,
	providerRegistered: boolean,
	signal: ProviderSignal
): AgentRuntimeStatus[] {
	return [
		{
			id: 'vscode',
			label: 'VS Code MCP registry (Copilot Chat)',
			detected: api === 'available',
			registered: vsCodeRegistryLive(api, providerRegistered, signal),
			viaVsCodeRegistry: true,
			writable: false,
		},
		...runtimes,
	];
}

function reachCopy(reach: ReachPhase, host: string): { label: string; title: string } {
	switch (reach) {
		case 'unauthorized':
			return {
				label: 'Engraphy: token needed',
				title: host + ' is running but would not accept this token.',
			};
		case 'unreachable':
			return {
				label: 'Engraphy: unreachable',
				title: 'Could not reach the memory server at ' + host + '.',
			};
		default:
			return {
				label: 'Engraphy: server error',
				title: host + ' answered but the connection check failed.',
			};
	}
}

/**
 * The end-to-end verdict.
 *
 * Order matters. A dead server is reported as a dead server even when an agent
 * is registered, because registering an agent against a server that is not
 * answering fixes nothing. Only once the server is proven readable does the
 * agent-path question become the one worth showing.
 */
export function buildCapabilityVM(input: CapabilityInput): CapabilityVM {
	const runtimes = withVsCodeRuntime(
		input.runtimes,
		input.api,
		input.providerRegistered,
		input.signal
	);

	if (input.reach === 'unconfigured') {
		return {
			phase: 'unconfigured',
			label: 'Engraphy: no server',
			title: 'Engraphy is not pointed at a memory server yet.',
			detail: 'Run "Engraphy: Connect to a server" to set one up.',
			usable: false,
			runtimes,
			action: { command: CONNECT_COMMAND, title: 'Connect to a server' },
		};
	}

	if (input.reach !== 'reachable') {
		const c = reachCopy(input.reach, input.host);
		return {
			phase: 'server-unavailable',
			label: c.label,
			title: c.title,
			detail: input.reachDetail,
			usable: false,
			runtimes,
			action: { command: CONNECT_COMMAND, title: 'Connect to a server' },
		};
	}

	const ignored = new Set(input.ignored ?? []);
	const live = runtimes.filter((r) => r.registered);
	// A gap is a runtime that is INSTALLED, is not registered, and the user has
	// not dismissed. The VS Code entry is never a gap on its own: an editor
	// without Copilot Chat is not a broken setup, it is simply not that path.
	const gaps = runtimes.filter(
		(r) => r.detected && !r.registered && !ignored.has(r.id) && !r.viaVsCodeRegistry
	);

	if (live.length > 0 && gaps.length > 0) {
		return {
			phase: 'partial',
			label:
				gaps.length === 1
					? gaps[0].label + ' cannot see memory'
					: gaps.length + ' agents cannot see memory',
			title:
				'Engraphy is reachable and ' +
				live.map((r) => r.label).join(', ') +
				' can use it, but ' +
				gaps.map((r) => r.label).join(', ') +
				' is installed here without Engraphy registered.',
			detail:
				'An agent holding no Engraphy tools cannot tell you so. Asked to save a memory it may report a save that never left it, and nothing reaches the server. Register it, or dismiss it if you do not use it.',
			// True, and deliberately so: memory IS reachable from at least one
			// agent. The label carries the warning rather than the usable flag.
			usable: true,
			runtimes,
			action: { command: REGISTER_COMMAND, title: 'Register with your coding agent' },
		};
	}

	if (live.length > 0) {
		return {
			phase: 'ready',
			label: 'Engraphy',
			title:
				'Connected to ' +
				input.host +
				'. Holding the Engraphy tools: ' +
				live.map((r) => r.label).join(', ') +
				'.',
			usable: true,
			runtimes,
		};
	}

	// The incident state: the server is healthy, the extension can read it, and
	// nothing an agent reads points at it. Name it rather than painting green.
	return {
		phase: 'no-agent-path',
		label: 'Engraphy: agent cannot see memory',
		title:
			'The memory server at ' +
			input.host +
			' is reachable from this extension, but no coding agent on this machine is registered to use it.',
		detail: agentGapDetail(input, runtimes),
		usable: false,
		runtimes,
		action: { command: REGISTER_COMMAND, title: 'Register with your coding agent' },
	};
}

/** The specific reason no agent path is live, in the user's own terms. */
function agentGapDetail(input: CapabilityInput, runtimes: AgentRuntimeStatus[]): string {
	const lines: string[] = [];
	if (input.api === 'missing') {
		lines.push(
			'This VS Code build has no MCP provider API, which needs VS Code 1.101 or newer, so automatic registration with the editor is not available here.'
		);
	} else if (!input.providerRegistered) {
		lines.push('The Engraphy MCP provider did not register with VS Code in this session.');
	} else if ((input.signal.calls ?? 0) === 0) {
		// Say what is OBSERVED, then the candidate causes. The observation is
		// certain; the cause is not, and asserting one cause is how a policy
		// block gets misread as "you just have not used Copilot Chat yet".
		lines.push('Engraphy is offered to VS Code, and VS Code has never asked for it.');
		const restrictions = policyRestrictions(input.policy);
		if (restrictions.length > 0) {
			lines.push(
				'MCP is restricted in this editor: ' +
					restrictions.join('; ') +
					'. On a managed machine these are often set by your organisation and shown as "Managed by organization" in Settings.'
			);
		} else {
			lines.push(
				'Two things cause this. VS Code queries providers when a Copilot Chat message is submitted, so an editor with no Copilot Chat traffic never picks the server up. An organisation policy can also gate MCP: check "chat.mcp.enabled", "chat.mcp.access", "chat.mcp.allowManagedServersOnly" and "chat.mcp.deniedServers" in Settings for a "Managed by organization" badge.'
			);
		}
	} else if ((input.signal.lastCount ?? 0) === 0) {
		lines.push(
			'VS Code asked for the Engraphy server definition and none was returned, which happens when the server URL is empty or malformed.'
		);
	}
	const others = runtimes.filter((r) => !r.viaVsCodeRegistry && r.detected);
	if (others.length > 0) {
		lines.push(
			'Detected on this machine: ' +
				others.map((r) => r.label).join(', ') +
				'. These agents read their own MCP config and do not use the VS Code registry, so each needs Engraphy registered separately.'
		);
	}
	lines.push(
		'Run "Engraphy: Register with your coding agent" to do that without editing any file by hand.'
	);
	return lines.join('\n\n');
}

// ---- write freshness --------------------------------------------------------

/**
 * When memory last actually changed, derived from the `stats` day series.
 *
 * This is the client-side answer to "did the write the agent claims it made
 * really land". An agent that reports a save it never made leaves this line
 * untouched, so the claim is directly falsifiable by looking at the panel.
 *
 * `stats` is a safe probe for this. Metric increments happen only at the write
 * and search chokepoints (engraphy/core/search.py and engraphy/core/dedup.py
 * call metrics.bump_safe); the stats handler in engraphy/server/tools/read.py
 * is a pure read of the rollup and bumps nothing, so polling it cannot inflate
 * the numbers the Impact & usage panel reports. `search` is NOT safe for this
 * and must not be substituted.
 */
export interface WriteFreshness {
	/** ISO date (YYYY-MM-DD) of the most recent day with any write activity. */
	lastWriteDate?: string;
	/** Write-shaped events on that day: inserts, dedup outcomes and promotes. */
	lastWriteCount: number;
	/** Total write-shaped events across the queried range. */
	rangeTotal: number;
	/** One line for the panel and the tooltip. */
	summary: string;
}

export interface WriteDay {
	date: string;
	facts_stored: number;
	duplicates_prevented: number;
	promotes: number;
}

/**
 * `today` is passed in rather than read off the clock, so this stays pure and
 * testable. Pass an ISO YYYY-MM-DD string.
 */
export function buildWriteFreshness(series: WriteDay[], today: string): WriteFreshness {
	let rangeTotal = 0;
	let lastWriteDate: string | undefined;
	let lastWriteCount = 0;
	for (const d of series) {
		const n = (d.facts_stored ?? 0) + (d.duplicates_prevented ?? 0) + (d.promotes ?? 0);
		rangeTotal += n;
		if (n > 0 && (!lastWriteDate || d.date > lastWriteDate)) {
			lastWriteDate = d.date;
			lastWriteCount = n;
		}
	}
	if (!lastWriteDate) {
		return {
			lastWriteCount: 0,
			rangeTotal: 0,
			summary:
				'No memory has been written in this range. An agent reporting a save has not reached this server.',
		};
	}
	const when = lastWriteDate === today ? 'today' : 'on ' + lastWriteDate;
	return {
		lastWriteDate,
		lastWriteCount,
		rangeTotal,
		summary:
			'Last write reached the server ' +
			when +
			' (' +
			lastWriteCount +
			(lastWriteCount === 1 ? ' write' : ' writes') +
			' that day, ' +
			rangeTotal +
			' in range).',
	};
}
