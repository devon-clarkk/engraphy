// Engraphy for VS Code.
//
// Tier 1: registers the Engraphy MCP server with VS Code's native MCP support
// (contributes.mcpServerDefinitionProviders + registerMcpServerDefinition-
// Provider) so Copilot agent mode can use Engraphy's tools, AND registers it
// with the third-party coding agents that read their own config instead. The
// VS Code API reaches VS Code and nothing else, so on its own it leaves a
// Claude Code or Cursor user with no Engraphy tools at all. See
// agentRuntimes.ts for the runtimes and capability.ts for the verdict.
//
// Tier 2: a confirm-write queue + memory explorer UI driven by a typed MCP
// client, plus the status-bar capability indicator and Refresh.
//
// API reference: https://code.visualstudio.com/api/extension-guides/ai/mcp

import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';
import { promisify } from 'util';

import { EngraphyClient, type EngraphyConnection } from './mcpClient';
import { EngraphyToolError, type InboxItemData } from './toolResult';
import { STARTER_NODE_TYPES, isValidServerUrl, promoteDefaults } from './webviewMessages';
import { ExplorerProvider } from './explorerView';
import { ConfirmWebviewProvider } from './confirmWebview';
import { StatsWebviewProvider } from './statsWebview';
import { StatusBar, type AgentContext } from './status';
import {
	buildWriteFreshness,
	type AgentRuntimeStatus,
	type McpApiState,
	type ProviderSignal,
} from './capability';
import {
	RUNTIMES,
	TOKEN_PLAINTEXT_WARNING,
	detectRuntimes,
	registerRuntime,
	verifyRegistration,
	type RuntimeSpec,
} from './agentRuntimes';
import { getToken, migrateTokenSetting, primeToken, setToken, watchToken } from './tokenStore';
import { describeError, hostLabel } from './connection';

const execAsync = promisify(cp.exec);

const PROVIDER_ID = 'engraphy';
const CONFIG_SECTION = 'engraphy';
const DOCKER_INSTALL_DOCS = 'https://docs.docker.com/compose/install/';

/**
 * Current connection settings. The URL and space are ordinary settings; the
 * token comes from the SecretStorage-backed cache in tokenStore, NOT from
 * settings.json (see that module for why it is cached rather than awaited).
 */
function connection(): EngraphyConnection {
	const cfg = vscode.workspace.getConfiguration(CONFIG_SECTION);
	return {
		serverUrl: (cfg.get<string>('serverUrl') ?? '').trim(),
		token: getToken(),
		space: (cfg.get<string>('space') ?? '').trim(),
	};
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
	const output = vscode.window.createOutputChannel('Engraphy');
	context.subscriptions.push(output);

	const extensionVersion =
		(context.extension.packageJSON as { version?: string })?.version ?? '0.0.0';

	// Priming MUST be awaited: connection() reads the cache synchronously, so
	// registering anything before the token is in hand would race.
	await primeToken(context);
	context.subscriptions.push(watchToken(context));

	// ---- Tier 1: MCP provider registration (best-effort; never blocks the UI) ----
	//
	// Registration is now INSTRUMENTED and its result is kept, because "the call
	// did not throw" was never evidence the editor picked the server up, and the
	// difference is invisible to a user otherwise.
	const didChangeEmitter = new vscode.EventEmitter<void>();
	context.subscriptions.push(didChangeEmitter);
	const mcpApi: McpApiState =
		typeof vscode.lm?.registerMcpServerDefinitionProvider === 'function' ? 'available' : 'missing';
	const providerRegistered = registerMcpProvider(
		context,
		output,
		extensionVersion,
		didChangeEmitter,
		(count) => void recordProviderSignal(context, count)
	);

	// Runtime detection touches the filesystem, so it is cached and refreshed on
	// demand rather than run on every status repaint.
	let runtimes: AgentRuntimeStatus[] = [];
	const rescanRuntimes = (): AgentRuntimeStatus[] => {
		try {
			runtimes = detectRuntimes();
		} catch (e) {
			output.appendLine(`Could not scan for coding agents: ${String(e)}`);
			runtimes = [];
		}
		return runtimes;
	};
	rescanRuntimes();
	const agentContext = (): AgentContext => ({
		api: mcpApi,
		providerRegistered,
		signal: readProviderSignal(context),
		runtimes,
	});

	// ---- Tier 2: client + views + status bar ----
	const client = new EngraphyClient(connection, extensionVersion);
	context.subscriptions.push({ dispose: () => void client.close() });

	// Confirm-write queue is now a branded WebviewView (theme-adaptive; see
	// confirmWebview.ts + media/confirm.*). The memory explorer stays a TreeView
	// for now — the graph-explorer webview is a deliberate follow-up.
	// The panels need BOTH the URL and whether a token exists: "the server
	// refused your token" and "the server needs a token you never set" are the
	// same 401 but different remedies.
	const connectionInfo = (): { serverUrl: string; hasToken: boolean } => {
		const c = connection();
		return { serverUrl: c.serverUrl, hasToken: c.token.length > 0 };
	};
	const confirmProvider = new ConfirmWebviewProvider(
		context.extensionUri,
		client,
		output,
		(item) => promoteItem(client, output, item),
		connectionInfo
	);
	const statsProvider = new StatsWebviewProvider(
		context.extensionUri,
		client,
		output,
		connectionInfo
	);
	const explorerProvider = new ExplorerProvider(client, output);
	context.subscriptions.push(
		vscode.window.registerWebviewViewProvider(StatsWebviewProvider.viewId, statsProvider, {
			webviewOptions: { retainContextWhenHidden: true },
		}),
		vscode.window.registerWebviewViewProvider(ConfirmWebviewProvider.viewId, confirmProvider, {
			webviewOptions: { retainContextWhenHidden: true },
		}),
		vscode.window.createTreeView('engraphyExplorer', { treeDataProvider: explorerProvider })
	);

	const status = new StatusBar(client, connection, agentContext);
	context.subscriptions.push(status);

	const refreshAll = async (): Promise<void> => {
		rescanRuntimes();
		await status.refresh();
		await confirmProvider.refresh();
		await statsProvider.refresh();
		explorerProvider.refresh();
	};

	// ---- commands ----
	context.subscriptions.push(
		vscode.commands.registerCommand('engraphy.startLocalServer', () =>
			startLocalServer(output, didChangeEmitter)
		),
		vscode.commands.registerCommand('engraphy.reconnect', async () => {
			await client.reconnect();
			didChangeEmitter.fire();
			await refreshAll();
			void vscode.window.showInformationMessage('Engraphy: reconnected and refreshed.');
		}),
		vscode.commands.registerCommand('engraphy.refresh', () => runSafely(output, refreshAll)),
		vscode.commands.registerCommand('engraphy.search', () =>
			runSafely(output, () => searchCommand(client, explorerProvider))
		),
		vscode.commands.registerCommand('engraphy.viewNodeDetails', (id: string) =>
			runSafely(output, () => viewNodeDetails(client, id))
		),
		// Row actions (approve / merge / promote / discard) now flow through the
		// webview's message channel (confirmWebview.ts). This palette-safe command
		// stays as a manual entry point for resolving a pending_id by hand.
		vscode.commands.registerCommand('engraphy.pending.resolveById', () =>
			runSafely(output, () => resolveByIdCommand(client, confirmProvider, output))
		),
		// Onboarding: open the setup walkthrough, paste a URL+token, or explain the
		// (not-yet-available) hosted option.
		vscode.commands.registerCommand('engraphy.gettingStarted', () => openWalkthrough()),
		vscode.commands.registerCommand('engraphy.configureServer', () =>
			runSafely(output, () => configureServer(context, client, confirmProvider, output))
		),
		vscode.commands.registerCommand('engraphy.cloudComingSoon', () => cloudComingSoon()),
		vscode.commands.registerCommand('engraphy.registerWithAgent', () =>
			runSafely(output, async () => {
				await registerWithAgent(output, connection());
				rescanRuntimes();
				await refreshAll();
			})
		),
		vscode.commands.registerCommand('engraphy.verifyWrites', () =>
			runSafely(output, () => verifyWrites(client, output))
		)
	);

	// Re-read on settings change: reconnect the client, refresh MCP defs + UI.
	context.subscriptions.push(
		vscode.workspace.onDidChangeConfiguration((e) => {
			if (e.affectsConfiguration(CONFIG_SECTION)) {
				output.appendLine('Engraphy settings changed, reconnecting.');
				didChangeEmitter.fire();
				void client.reconnect().then(() => runSafely(output, refreshAll));
			}
		})
	);

	// The plaintext-token migration is deliberately NOT awaited before the
	// registrations above: it writes settings and secrets, and a slow or locked
	// keychain would otherwise stall activation (including the MCP collection)
	// behind it, leaving blank panels. It self-heals instead: cfg.update fires
	// onDidChangeConfiguration, and secrets.onDidChange re-primes the cache, so
	// both paths below reconnect and repaint on their own once it lands.
	void migrateTokenSetting(context, output);

	output.appendLine(`Engraphy extension activated (v${extensionVersion}).`);
	// Initial paint (non-blocking), then tell the user if nothing can reach the
	// memory. The status bar cannot be the only channel for this: it is hideable
	// (workbench.statusBar.visible), and the whole failure being fixed here is
	// one nobody noticed.
	void runSafely(output, async () => {
		await refreshAll();
		await warnIfNoAgentPath(context, status, output);
	});
	// First run: guide a cold install to a server. Only nags once (globalState)
	// and only when a server isn't actually reachable — never on a working setup.
	void maybeOfferFirstRunWalkthrough(context, client, output);
}

const WELCOMED_KEY = 'engraphy.welcomed.v1';
const PROVIDER_SIGNAL_KEY = 'engraphy.mcp.providerSignal.v1';

// ---- provider-consumption signal -------------------------------------------
//
// The record of whether VS Code has ever ASKED this extension for its MCP
// server definitions. Kept in globalState rather than memory on purpose: the
// question worth answering is "does anything on this machine consume the MCP
// registry at all", and a fresh window that has not yet seen a chat message
// would answer that wrongly from memory alone.

function readProviderSignal(context: vscode.ExtensionContext): ProviderSignal {
	return context.globalState.get<ProviderSignal>(PROVIDER_SIGNAL_KEY) ?? {};
}

async function recordProviderSignal(
	context: vscode.ExtensionContext,
	count: number
): Promise<void> {
	const prev = readProviderSignal(context);
	await context.globalState.update(PROVIDER_SIGNAL_KEY, {
		lastProvidedAt: new Date().toISOString(),
		lastCount: count,
		calls: (prev.calls ?? 0) + 1,
	} satisfies ProviderSignal);
}

// ---- the no-agent-path warning ---------------------------------------------

const AGENT_WARNED_KEY = 'engraphy.agentGapWarned.v1';

/**
 * Say out loud, once, when the memory server is reachable but nothing an agent
 * reads points at it.
 *
 * This is the incident state, and it is the one that has to interrupt. A
 * silently-unregistered MCP server plus an agent that narrates saves it never
 * made loses data with nothing on screen disagreeing.
 *
 * Nagging is bounded two ways: the flag suppresses repeats, and it is CLEARED
 * the moment an agent path goes live, so a setup that later breaks warns again
 * rather than staying quiet because it was once fine.
 */
async function warnIfNoAgentPath(
	context: vscode.ExtensionContext,
	status: StatusBar,
	log: vscode.OutputChannel
): Promise<void> {
	const vm = status.capability;
	if (!vm) {
		return;
	}
	if (vm.phase !== 'no-agent-path') {
		if (vm.phase === 'ready' && context.globalState.get<boolean>(AGENT_WARNED_KEY)) {
			await context.globalState.update(AGENT_WARNED_KEY, false);
		}
		return;
	}
	log.appendLine('No agent path to Engraphy: ' + (vm.detail ?? vm.title));
	if (context.globalState.get<boolean>(AGENT_WARNED_KEY)) {
		return;
	}
	await context.globalState.update(AGENT_WARNED_KEY, true);
	const pick = await vscode.window.showWarningMessage(
		'Engraphy: the memory server is reachable, but no coding agent on this machine is registered to use it. ' +
			'An agent asked to save a memory right now would not reach it.',
		'Register with your coding agent',
		'Details'
	);
	if (pick === 'Register with your coding agent') {
		await vscode.commands.executeCommand('engraphy.registerWithAgent');
	} else if (pick === 'Details') {
		log.show(true);
	}
}

// ---- write confirmation ----------------------------------------------------

/**
 * Ask the SERVER when memory last actually changed.
 *
 * An agent can only report a save it made; it cannot report one it never made,
 * and when it holds no Engraphy tools at all it may narrate a save that never
 * left the model. Nothing the agent says is evidence. This command asks the
 * server directly, so the claim becomes checkable in one click.
 *
 * `stats` is the right probe. Metric increments happen only at the write and
 * search chokepoints (core/dedup.py and core/search.py call metrics.bump_safe);
 * the stats handler in server/tools/read.py is a pure read of the rollup and
 * bumps nothing, so this check cannot inflate the numbers the Impact & usage
 * panel reports. Do not substitute `search`, which does record usage.
 */
async function verifyWrites(client: EngraphyClient, log: vscode.OutputChannel): Promise<void> {
	const res = await client.stats(WRITE_CHECK_RANGE_DAYS, 'space');
	// The server stamps generated_at, so the "today" comparison uses the
	// server's calendar rather than this machine's, which is the one the day
	// buckets were written against.
	const today = (res.generated_at || '').slice(0, 10);
	const freshness = buildWriteFreshness(res.series, today);
	log.appendLine(`verifyWrites: ${freshness.summary}`);
	if (freshness.rangeTotal === 0) {
		void vscode.window.showWarningMessage(
			`Engraphy: nothing has been written to this space in the last ${WRITE_CHECK_RANGE_DAYS} days. ` +
				'If an agent told you it saved something, that write did not reach this server.',
			'Register with your coding agent'
		).then((pick) => {
			if (pick) {
				void vscode.commands.executeCommand('engraphy.registerWithAgent');
			}
		});
		return;
	}
	void vscode.window.showInformationMessage('Engraphy: ' + freshness.summary);
}

const WRITE_CHECK_RANGE_DAYS = 7;

// ---- one-click registration with a coding agent ----------------------------

/**
 * Register Engraphy with the agent the user actually runs, by writing that
 * agent's own MCP config.
 *
 * This exists because `vscode.lm.registerMcpServerDefinitionProvider` reaches
 * VS Code and nothing else. Claude Code, Cursor and every other third-party
 * agent read their own config file, so for those users the VS Code API is a
 * no-op and the only remedy used to be hand-editing an mcp json. This is that
 * edit, done for them, verified afterwards by reading the file back.
 */
async function registerWithAgent(
	log: vscode.OutputChannel,
	conn: EngraphyConnection
): Promise<void> {
	if (!conn.serverUrl) {
		const pick = await vscode.window.showWarningMessage(
			'Engraphy has no server URL yet, so there is nothing to register. Set one first.',
			'Connect to a server'
		);
		if (pick) {
			await vscode.commands.executeCommand('engraphy.configureServer');
		}
		return;
	}

	const detected = detectRuntimes();
	const byId = new Map(detected.map((r) => [r.id, r]));
	const items = RUNTIMES.map((spec) => {
		const found = byId.get(spec.id);
		const registered = found?.registered ?? false;
		return {
			label: spec.label,
			description: registered ? 'already registered' : found?.detected ? 'detected' : 'not detected',
			detail: found?.configPath,
			picked: !registered && (found?.detected ?? false),
			spec,
		};
	});

	const chosen = await vscode.window.showQuickPick(items, {
		title: 'Engraphy: register with your coding agent',
		placeHolder: 'Pick every agent that should be able to read and write Engraphy memory',
		canPickMany: true,
		ignoreFocusOut: true,
	});
	if (!chosen || chosen.length === 0) {
		return;
	}

	// Writing a bearer token into a plaintext config is a real trade against the
	// keychain storage 0.5.0 introduced, so it is never done silently.
	if (conn.token) {
		const ok = await vscode.window.showWarningMessage(
			`Register Engraphy with ${chosen.map((c) => c.spec.label).join(', ')}?`,
			{ modal: true, detail: TOKEN_PLAINTEXT_WARNING },
			'Register'
		);
		if (ok !== 'Register') {
			return;
		}
	}

	const done: string[] = [];
	const failed: string[] = [];
	for (const item of chosen) {
		const spec: RuntimeSpec = item.spec;
		const outcome = registerRuntime(spec, conn.serverUrl, conn.token);
		if (!outcome.ok) {
			log.appendLine(`register ${spec.id}: ${outcome.problem ?? 'failed'} (${outcome.path})`);
			failed.push(`${spec.label}: ${outcome.problem ?? 'write failed'}`);
			continue;
		}
		// Never report a registration on the strength of the write alone. Reading
		// it back is the whole point: this bug class is what unverified success
		// looks like.
		if (!verifyRegistration(spec, conn.serverUrl)) {
			log.appendLine(`register ${spec.id}: wrote ${outcome.path} but the entry did not read back`);
			failed.push(`${spec.label}: the entry did not read back from ${outcome.path}`);
			continue;
		}
		log.appendLine(
			`register ${spec.id}: verified in ${outcome.path}` +
				(outcome.unchanged ? ' (already current)' : '') +
				(outcome.backup ? `, backup at ${outcome.backup}` : '')
		);
		done.push(spec.label);
	}

	if (done.length > 0) {
		const pick = await vscode.window.showInformationMessage(
			`Engraphy registered with ${done.join(', ')}. Restart that agent so it reloads its MCP config.` +
				(failed.length > 0 ? ` ${failed.length} could not be written.` : ''),
			'Show details'
		);
		if (pick) {
			log.show(true);
		}
	}
	if (failed.length > 0 && done.length === 0) {
		void vscode.window.showErrorMessage(`Engraphy could not register: ${failed.join('; ')}`);
	}
}

/**
 * On the first-ever activation, if no server answers, open the setup
 * walkthrough. Guarded by globalState so it shows at most once, and gated on an
 * actual (short-timeout) health probe so an already-working install is never
 * interrupted. The welcomed flag is set unconditionally the first time, so this
 * never fires on later launches regardless of outcome.
 */
async function maybeOfferFirstRunWalkthrough(
	context: vscode.ExtensionContext,
	client: EngraphyClient,
	log: vscode.OutputChannel
): Promise<void> {
	if (context.globalState.get<boolean>(WELCOMED_KEY)) {
		return;
	}
	await context.globalState.update(WELCOMED_KEY, true);
	const reachable = await probeReachable(client);
	if (!reachable) {
		log.appendLine('First run: no server reachable, opening the setup walkthrough.');
		openWalkthrough();
	}
}

/**
 * True if the server answers an AUTHENTICATED read within a short budget.
 *
 * Deliberately not /healthz: that endpoint is unauthenticated, so a fresh
 * install pointed at the default URL with no token would look "reachable" and
 * skip the setup walkthrough, which is precisely the person who needs it.
 */
async function probeReachable(client: EngraphyClient, timeoutMs = 3000): Promise<boolean> {
	const timeout = new Promise<never>((_, reject) =>
		setTimeout(() => reject(new Error('timeout')), timeoutMs)
	);
	try {
		await Promise.race([client.scopeList(), timeout]);
		return true;
	} catch {
		return false;
	}
}

export function deactivate(): void {
	// context.subscriptions handles disposal.
}

// ---- error-guarded command runner ------------------------------------------

async function runSafely(log: vscode.OutputChannel, fn: () => Promise<void>): Promise<void> {
	try {
		await fn();
	} catch (e) {
		const msg = e instanceof EngraphyToolError ? `${e.code}: ${e.message}` : String(e);
		log.appendLine(`command failed: ${msg}`);
		void vscode.window.showErrorMessage(`Engraphy: ${msg}`);
	}
}

// ---- Tier-2 command implementations ----------------------------------------
//
// Reads and row actions for the confirm queue now live in the webview provider
// (confirmWebview.ts). What remains here is the memory-explorer search, node
// detail view, the native promote (authoring) flow the webview delegates to, and
// the palette-safe manual resolve-by-id.

async function searchCommand(client: EngraphyClient, explorer: ExplorerProvider): Promise<void> {
	const query = await vscode.window.showInputBox({
		title: 'Engraphy: Search memory',
		prompt: 'Search query',
		ignoreFocusOut: true,
	});
	if (!query) {
		return;
	}
	const scope = await pickScope(client);
	if (scope === undefined) {
		return;
	}
	const res = await client.search({ scope, query });
	explorer.setResults(query, res);
	await vscode.commands.executeCommand('engraphyExplorer.focus');
}

/** Offer the token's readable scopes plus 'all'; fall back to 'all' if scope_list fails. */
async function pickScope(client: EngraphyClient): Promise<string | undefined> {
	let scopes: string[] = [];
	try {
		const res = (await client.scopeList()) as { scopes?: Array<string | { id?: string; name?: string }> };
		scopes = (res.scopes ?? []).map((s) => (typeof s === 'string' ? s : (s.name ?? s.id ?? ''))).filter(Boolean);
	} catch {
		// scope_list unavailable — just use 'all'.
	}
	const options = ['all', ...scopes];
	const pick = await vscode.window.showQuickPick(options, {
		title: 'Engraphy: scope to search',
		placeHolder: 'all',
	});
	return pick;
}

async function viewNodeDetails(client: EngraphyClient, id: string): Promise<void> {
	const res = await client.get([id]);
	const doc = await vscode.workspace.openTextDocument({
		language: 'json',
		content: JSON.stringify(res, null, 2),
	});
	await vscode.window.showTextDocument(doc, { preview: true });
}

/**
 * Native promote (authoring) flow for one inbox item. Invoked by the webview's
 * Promote button (confirmWebview delegates here).
 *
 * Promotion is AUTHORING: the write is the user's, not a verbatim replay of the
 * captured payload. Historically NOTHING was pre-filled (core/inbox.py D1 —
 * "the payload is never the source of the node"). Devon approved SOFTENING that:
 * title/body now open pre-filled from the payload (see promoteDefaults) and the
 * node type is a dropdown of the pack's types, so nothing has to be memorised —
 * but every field is still shown and editable before the write is issued. The
 * full payload is opened to the side for reference. The webview re-reads both
 * bands after this resolves.
 */
async function promoteItem(
	client: EngraphyClient,
	log: vscode.OutputChannel,
	item: InboxItemData
): Promise<void> {
	const payloadDoc = await vscode.workspace.openTextDocument({
		language: 'json',
		content: `// Captured payload for inbox item ${item.id} (reference only; title/body below are pre-filled from it, and editable)\n${JSON.stringify(item.payload, null, 2)}`,
	});
	await vscode.window.showTextDocument(payloadDoc, { preview: true, viewColumn: vscode.ViewColumn.Beside });

	// Scope is SKIPPED when the item already carries one (pre-filled from it), so
	// the total is 3 or 4. Label from a running index so "N/total" stays honest.
	const total = item.scope ? 3 : 4;
	let step = 0;
	const label = (name: string): string => `Promote ${++step}/${total} → ${name}`;

	const type = await pickNodeType(label('node type'));
	if (!type) {
		return;
	}

	let scope: string | undefined = item.scope ?? undefined;
	if (!scope) {
		scope = await pickPromoteScope(client, label('scope'));
		if (!scope) {
			return;
		}
	}

	const defaults = promoteDefaults(item.payload);
	const title = await vscode.window.showInputBox({
		title: label('title'),
		value: defaults.title,
		ignoreFocusOut: true,
	});
	if (!title) {
		return;
	}
	const body = await vscode.window.showInputBox({
		title: label('body'),
		value: defaults.body,
		ignoreFocusOut: true,
	});
	if (body === undefined) {
		return;
	}

	const res = (await client.inboxPromote({ id: item.id, type, scope, title, body })) as {
		outcome?: string;
		pending_id?: string;
		expires_at?: string;
	};

	if (res.outcome === 'needs_confirmation' && res.pending_id) {
		// Q3a: the inbox row stays 'pending' server-side — don't remove it
		// optimistically. The parked write now shows up in pending_list, so a
		// pending refresh surfaces it in the Pending duplicates band.
		void vscode.window.showWarningMessage(
			'Engraphy: promotion parked as a pending duplicate. Resolve it in the "Pending duplicates" band.'
		);
	} else {
		void vscode.window.showInformationMessage(`Engraphy: promoted (${res.outcome ?? 'ok'}).`);
	}
	log.appendLine(`promoted inbox ${item.id} → ${res.outcome ?? 'ok'}`);
}

/**
 * Node-type dropdown for promote. Offers the STARTER pack's types (the demo
 * space's pack) plus an "Other…" free-text escape hatch for any other pack —
 * see STARTER_NODE_TYPES for why this is static rather than server-sourced.
 */
async function pickNodeType(title: string): Promise<string | undefined> {
	const OTHER = 'Other…';
	const picks: vscode.QuickPickItem[] = [
		...STARTER_NODE_TYPES.map((o) => ({ label: o.type, description: o.description })),
		{ label: OTHER, description: 'Type a custom node type (e.g. for a non-starter pack)' },
	];
	const pick = await vscode.window.showQuickPick(picks, {
		title,
		placeHolder: 'Pick a node type (starter pack), or Other… for a custom one',
		ignoreFocusOut: true,
	});
	if (!pick) {
		return undefined;
	}
	if (pick.label === OTHER) {
		const custom = await vscode.window.showInputBox({
			title,
			prompt: 'Custom node type (must exist in the connected space’s pack)',
			ignoreFocusOut: true,
		});
		return custom || undefined;
	}
	return pick.label;
}

/**
 * Scope dropdown for promote, used only when the inbox item has no scope of its
 * own. Offers this token's readable scopes from `scope_list` plus an "Other…"
 * hatch. CAVEAT: scope_list returns READABLE scopes and does not encode write
 * access (that is admin_grant's surface, not exposed here), so a non-writable
 * pick is refused server-side — the hatch and the server's ENGRAPHY_* error cover
 * that. Falls back to a free-text input when no scopes are enumerable.
 */
async function pickPromoteScope(
	client: EngraphyClient,
	title: string
): Promise<string | undefined> {
	const OTHER = 'Other…';
	let scopes: string[] = [];
	try {
		const res = (await client.scopeList()) as { scopes?: Array<{ id?: string; display_name?: string }> };
		scopes = (res.scopes ?? []).map((s) => s.id ?? '').filter(Boolean);
	} catch {
		// scope_list unavailable — fall through to free-text.
	}
	if (scopes.length === 0) {
		const typed = await vscode.window.showInputBox({
			title,
			prompt: 'Scope id to write into',
			ignoreFocusOut: true,
		});
		return typed || undefined;
	}
	const picks: vscode.QuickPickItem[] = [
		...scopes.map((s) => ({ label: s })),
		{ label: OTHER, description: 'Type a scope id' },
	];
	const pick = await vscode.window.showQuickPick(picks, {
		title,
		placeHolder: 'Pick a scope (readable scopes), or Other…',
		ignoreFocusOut: true,
	});
	if (!pick) {
		return undefined;
	}
	if (pick.label === OTHER) {
		const typed = await vscode.window.showInputBox({ title, prompt: 'Scope id', ignoreFocusOut: true });
		return typed || undefined;
	}
	return pick.label;
}

/**
 * Manual entry point (palette-safe): resolve a pending_id typed by the user.
 * Reads the id + resolution via native inputs, then refreshes the webview.
 */
async function resolveByIdCommand(
	client: EngraphyClient,
	confirmProvider: ConfirmWebviewProvider,
	log: vscode.OutputChannel
): Promise<void> {
	const pendingId = await vscode.window.showInputBox({
		title: 'Resolve pending duplicate',
		prompt: 'pending_id (from a write/promote that returned needs_confirmation)',
		ignoreFocusOut: true,
	});
	if (!pendingId) {
		return;
	}
	const resolution = await vscode.window.showQuickPick(['distinct', 'merge'], {
		title: 'Resolution',
		placeHolder: 'distinct = keep as a new node; merge = fold into an existing one',
	});
	if (resolution !== 'distinct' && resolution !== 'merge') {
		return;
	}
	let mergeInto: string | undefined;
	if (resolution === 'merge') {
		mergeInto = await vscode.window.showInputBox({
			title: 'Merge into node id',
			prompt: 'Enter the node id to merge this pending write into.',
			ignoreFocusOut: true,
		});
		if (!mergeInto) {
			return;
		}
	}
	try {
		await client.resolveDuplicate(pendingId, resolution, mergeInto);
		void vscode.window.showInformationMessage(`Engraphy: pending write resolved as ${resolution}.`);
	} catch (e) {
		if (e instanceof EngraphyToolError && e.code === 'ENGRAPHY_PENDING_EXPIRED') {
			void vscode.window.showWarningMessage(
				'Engraphy: that pending write has expired. Re-issue the write for a fresh confirmation window.'
			);
		} else {
			throw e;
		}
	} finally {
		await confirmProvider.refresh();
	}
}

// ---- onboarding: walkthrough + configure + honest cloud placeholder --------

// `${publisher}.${name}#${walkthroughId}` from package.json.
const WALKTHROUGH_ID = 'engraphy.engraphy#engraphySetup';
const REPO_URL = 'https://github.com/devon-clarkk/engraphy';

/** Open the "Set up Engraphy" Getting Started walkthrough. */
function openWalkthrough(): void {
	void vscode.commands.executeCommand('workbench.action.openWalkthrough', WALKTHROUGH_ID, false);
}

/**
 * Prompt for serverUrl + token, save them to user settings, then validate by
 * hitting /healthz and reporting success/failure. The config-change listener
 * also reconnects + refreshes; the inline health check is what gives immediate
 * connected/unreachable feedback.
 */
async function configureServer(
	context: vscode.ExtensionContext,
	client: EngraphyClient,
	confirmProvider: ConfirmWebviewProvider,
	log: vscode.OutputChannel
): Promise<void> {
	const cfg = vscode.workspace.getConfiguration(CONFIG_SECTION);
	const currentUrl = (cfg.get<string>('serverUrl') ?? '').trim();
	const url = await vscode.window.showInputBox({
		title: 'Engraphy → server URL',
		value: currentUrl || 'http://127.0.0.1:8000/mcp/',
		prompt: 'The MCP endpoint (Streamable HTTP). Keep the trailing slash.',
		ignoreFocusOut: true,
		validateInput: (v) =>
			isValidServerUrl(v) ? undefined : 'Enter a valid http(s) URL, e.g. http://127.0.0.1:8000/mcp/',
	});
	if (!url) {
		return;
	}
	const hasExisting = getToken().length > 0;
	const token = await vscode.window.showInputBox({
		title: 'Engraphy → token',
		prompt: hasExisting
			? 'Bearer token from `engraphy-admin token create`. Leave blank to keep the stored one.'
			: 'Bearer token from `engraphy-admin token create`. Stored in the OS keychain, not in settings.json.',
		password: true,
		ignoreFocusOut: true,
	});
	if (token === undefined) {
		return; // Esc, don't save
	}

	await cfg.update('serverUrl', url.trim(), vscode.ConfigurationTarget.Global);
	if (token.trim() !== '') {
		try {
			await setToken(context, token);
		} catch (e) {
			log.appendLine(`configureServer: SecretStorage write failed: ${String(e)}`);
			void vscode.window.showErrorMessage(
				`Engraphy: could not save the token to the OS keychain (${String(e)}). The URL was saved.`
			);
			return;
		}
	}

	await client.reconnect();
	// Validate with BOTH probes. /healthz is unauthenticated, so reporting
	// "connected" off it alone would confirm a setup that cannot read a byte.
	const [healthRes, authRes] = await Promise.allSettled([client.health(), client.scopeList()]);
	const host = hostLabel(url.trim());
	if (authRes.status === 'fulfilled') {
		const h = healthRes.status === 'fulfilled' ? healthRes.value : undefined;
		void vscode.window.showInformationMessage(
			`Engraphy: connected to ${host}` +
				(h ? `: ${h.status}, v${h.version ?? '?'}, ${h.spaces ?? '?'} space(s).` : '.')
		);
	} else {
		const d = describeError(authRes.reason, host);
		log.appendLine(`configureServer: authenticated probe failed for ${url.trim()}: ${d.detail}`);
		if (d.class === 'auth') {
			void vscode.window.showWarningMessage(
				`Engraphy: ${host} is running but rejected this token. ` +
					'Mint a fresh one with `engraphy-admin token create` and run Connect again.'
			);
		} else {
			void vscode.window.showWarningMessage(
				`Engraphy: settings saved, but ${d.summary} ` +
					'Start a server (Run locally with Docker) or check the URL, then run Reconnect.'
			);
		}
	}
	await confirmProvider.refresh();
}

/** Honest placeholder for the not-yet-available hosted service. No fake signup. */
async function cloudComingSoon(): Promise<void> {
	const pick = await vscode.window.showInformationMessage(
		'Engraphy Cloud (hosted) isn’t available yet. For now, run locally with Docker or connect your own server.',
		'Set up locally',
		'Follow on GitHub'
	);
	if (pick === 'Set up locally') {
		openWalkthrough();
	} else if (pick === 'Follow on GitHub') {
		void vscode.env.openExternal(vscode.Uri.parse(REPO_URL));
	}
}

// ---- Tier 1: provider + local server (unchanged behavior) -------------------

function registerMcpProvider(
	context: vscode.ExtensionContext,
	output: vscode.OutputChannel,
	extensionVersion: string,
	didChangeEmitter: vscode.EventEmitter<void>,
	recordSignal: (count: number) => void
): boolean {
	if (typeof vscode.lm?.registerMcpServerDefinitionProvider !== 'function') {
		output.appendLine(
			'Native MCP provider API not available (needs VS Code 1.101+). ' +
				'The memory UI still works; MCP auto-registration is skipped.'
		);
		return false;
	}

	const provider: vscode.McpServerDefinitionProvider = {
		onDidChangeMcpServerDefinitions: didChangeEmitter.event,

		provideMcpServerDefinitions(_token: vscode.CancellationToken): vscode.McpServerDefinition[] {
			// Being ASKED is the only certain evidence that something in the editor
			// consumes the MCP registry. Record every call, including the ones that
			// return nothing, so "VS Code never asked" stays distinguishable from
			// "VS Code asked and Engraphy withheld a definition". Without this the
			// two look identical from the UI, and both look like success.
			const conn = connection();
			if (!conn.serverUrl) {
				output.appendLine('engraphy.serverUrl is empty, so no Engraphy MCP server was provided.');
				recordSignal(0);
				return [];
			}
			let uri: vscode.Uri;
			try {
				uri = vscode.Uri.parse(conn.serverUrl, true);
			} catch {
				void vscode.window.showErrorMessage(
					`Engraphy: engraphy.serverUrl is not a valid URL: "${conn.serverUrl}"`
				);
				recordSignal(0);
				return [];
			}
			const headers: Record<string, string> = {};
			if (conn.token) {
				headers['Authorization'] = `Bearer ${conn.token}`;
			}
			const label = conn.space ? `Engraphy (${conn.space})` : 'Engraphy';
			const digest = crypto
				.createHash('sha256')
				.update(`${conn.serverUrl}|${conn.space}|${conn.token}`)
				.digest('hex')
				.slice(0, 12);
			const version = `${extensionVersion}-${digest}`;
			output.appendLine(`VS Code asked for MCP definitions; provided 1 (${label}).`);
			recordSignal(1);
			return [new vscode.McpHttpServerDefinition(label, uri, headers, version)];
		},

		resolveMcpServerDefinition(
			server: vscode.McpServerDefinition,
			_token: vscode.CancellationToken
		): vscode.McpServerDefinition {
			return server;
		},
	};

	// A throwing registration must not read as a successful one. The return value
	// feeds the capability verdict, which is the thing the user sees.
	try {
		context.subscriptions.push(
			vscode.lm.registerMcpServerDefinitionProvider(PROVIDER_ID, provider)
		);
		return true;
	} catch (e) {
		output.appendLine(
			`Registering the MCP provider with VS Code failed: ${e instanceof Error ? e.message : String(e)}`
		);
		return false;
	}
}

function resolveComposeDir(output: vscode.OutputChannel): string | undefined {
	const configured = (vscode.workspace
		.getConfiguration(CONFIG_SECTION)
		.get<string>('composeWorkingDirectory') ?? '').trim();
	if (configured) {
		if (!fs.existsSync(configured)) {
			void vscode.window.showErrorMessage(
				`Engraphy: engraphy.composeWorkingDirectory does not exist: ${configured}`
			);
			return undefined;
		}
		return configured;
	}
	const folder = vscode.workspace.workspaceFolders?.[0];
	if (folder && folder.uri.scheme === 'file') {
		return folder.uri.fsPath;
	}
	void vscode.window.showErrorMessage(
		'Engraphy: set "engraphy.composeWorkingDirectory" to the folder containing compose.yaml, ' +
			'or open that folder as a workspace.'
	);
	return undefined;
}

function findComposeFile(dir: string): string | undefined {
	for (const name of ['compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml']) {
		if (fs.existsSync(path.join(dir, name))) {
			return name;
		}
	}
	return undefined;
}

async function dockerComposeAvailable(): Promise<boolean> {
	try {
		await execAsync('docker compose version', { timeout: 15000 });
		return true;
	} catch {
		return false;
	}
}

async function startLocalServer(
	output: vscode.OutputChannel,
	didChangeEmitter: vscode.EventEmitter<void>
): Promise<void> {
	const dir = resolveComposeDir(output);
	if (!dir) {
		return;
	}
	const composeFile = findComposeFile(dir);
	if (!composeFile) {
		void vscode.window.showErrorMessage(
			`Engraphy: no compose file (compose.yaml) found in ${dir}. ` +
				'Set "engraphy.composeWorkingDirectory" to your Engraphy checkout.'
		);
		return;
	}
	if (!(await dockerComposeAvailable())) {
		const pick = await vscode.window.showErrorMessage(
			'Engraphy: Docker Compose was not found on PATH. Install Docker Desktop / the ' +
				'Compose plugin, then try again.',
			'Open install docs'
		);
		if (pick === 'Open install docs') {
			void vscode.env.openExternal(vscode.Uri.parse(DOCKER_INSTALL_DOCS));
		}
		return;
	}
	const envPath = path.join(dir, '.env');
	if (!fs.existsSync(envPath)) {
		const checklist = path.join(dir, 'deploy', 'checklist.md');
		const actions = fs.existsSync(checklist) ? ['Open deploy/checklist.md'] : [];
		const pick = await vscode.window.showErrorMessage(
			`Engraphy: no .env found in ${dir}. Create one next to compose.yaml with ` +
				'POSTGRES_PASSWORD and ENGRAPHY_APP_ROLE_PASSWORD set to your own secrets, then retry.',
			...actions
		);
		if (pick === 'Open deploy/checklist.md') {
			void vscode.window.showTextDocument(vscode.Uri.file(checklist));
		}
		return;
	}

	output.appendLine(`Starting Engraphy via ${composeFile} in ${dir} ...`);
	const terminal = vscode.window.createTerminal({ name: 'Engraphy (compose up)', cwd: dir });
	terminal.show();
	terminal.sendText('docker compose up');

	const pick = await vscode.window.showInformationMessage(
		'Engraphy is starting in the terminal. First boot downloads the embedding model ' +
			'(~523MB) before it serves requests. Once healthy, run "Engraphy: Reconnect".',
		'Reconnect'
	);
	if (pick === 'Reconnect') {
		didChangeEmitter.fire();
		await vscode.commands.executeCommand('engraphy.reconnect');
	}
}
