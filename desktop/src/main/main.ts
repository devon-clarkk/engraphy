// Engraphy desktop — Electron main process.
//
// Plays the same role the VS Code extension host played (confirmWebview.ts /
// statsWebview.ts / explorerView.ts / extension.ts): it owns the single
// EngraphyClient, makes every MCP call, builds the render-ready view-models with
// the copied pure builders (the trust boundary), and pushes state to the
// renderer. The renderer never talks to the server directly and never sees the
// token.
//
// Transport to the renderer mirrors the extension's webview postMessage model:
//   • renderer → main  fire-and-forget:  ipcRenderer.send('engraphy:msg', {channel, msg})
//   • main → renderer   pushes:           webContents.send('engraphy:msg', {channel, msg})
//   • renderer → main  request/response:  ipcRenderer.invoke('engraphy:invoke', {channel, msg})
// so the copied confirm.js / stats.js render code ports with a tiny header change.
//
// Every `invoke` handler returns the discriminated contract from ipcResult.ts
// ({ok:true, ...} | {ok:false, error}). It used to return bare payloads, and
// because the ipcMain wrapper catches and RESOLVES on failure, a renderer's
// try/catch never fired: the explorer rendered "No results" for a dead server
// and for a rejected token alike.

import { app, BrowserWindow, ipcMain, dialog, shell, nativeTheme, Menu, screen } from 'electron';
import * as fs from 'fs';
import * as path from 'path';

import { EngraphyClient, type EngraphyConnection } from './client/mcpClient';
import {
	EngraphyToolError,
	inboxItemsFrom,
	pendingItemsFrom,
	type InboxItemData,
	type PendingListItem,
} from './client/toolResult';
import {
	buildInboxCard,
	buildPendingCard,
	promoteDefaults,
	STARTER_NODE_TYPES,
	type InboxCardVM,
	type PendingCardVM,
} from './client/webviewMessages';
import { buildStatsView, type StatsViewVM } from './client/statsModel';
import {
	buildHealthVM,
	computeConnection,
	describeError,
	hostLabel,
	type DescribedError,
	type DesktopConnectionVM,
	type HealthVM,
} from './connection';
import { fail, ok } from './ipcResult';
import {
	mergeNodeTypes,
	missingIdsFrom,
	nodeDetailFrom,
	nodeRefsFromSearch,
	nodeRefsFromTraverse,
	observedTypesFrom,
	scopeIdsFrom,
} from './explorerModel';
import { firstError, validateSettingsInput } from './validation';
import {
	isRenderableNode,
	parseGraphMessage,
	parseSnapshot,
	pruneDanglingEdges,
	type GraphProgress,
	type GraphSnapshot,
	type GraphStateVM,
} from './graphModel';
import { Cancelled, harvestGraph, type GraphTools } from './graphHarvest';
import { parseAppCommand, parseConfirmCommand, parseStatsCommand, parseUpdateCommand } from './messages';
import {
	CHECK_INTERVAL_MS,
	DEFAULT_MANIFEST_URL,
	PRODUCT_KEY,
	checkForUpdate,
	isDismissed,
	shouldCheck,
	type Fetcher,
	type UpdateVerdict,
} from './versionCheck';
import {
	buildUpdateBanner,
	detectChannel,
	type UpdateBannerVM,
} from './updateModel';
import {
	MIN_HEIGHT,
	MIN_WIDTH,
	restoreBounds,
	sanitizeSavedBounds,
} from './windowState';
import {
	DEFAULT_SERVER_URL,
	importBootstrapFile,
	isOnboardingCompleted,
	loadSafeSettings,
	loadSettings,
	loadUpdateState,
	loadWindowBounds,
	saveSettings,
	saveWindowBounds,
	setOnboardingCompleted,
	setUpdateCheckEnabled,
	setUpdateChecked,
	setUpdateDismissed,
	settingsFilePath,
	type SafeSettings,
} from './settings';

const APP_VERSION = (app.getVersion && app.getVersion()) || '0.1.0';
const ENGRAPHY_REPO_URL = 'https://github.com/devon-clarkk/engraphy';

let win: BrowserWindow | undefined;

/**
 * Smoke-only environment seeding. Runs at module load, before app.whenReady, so
 * the userData path is redirected BEFORE anything reads or writes settings.
 *
 * This is what lets one scripted run exercise each connection state against a
 * real server: a fresh profile per scenario (so the first-run flag and saved
 * settings never leak between them) plus a pre-seeded serverUrl/token. All of
 * it is inert unless ENGRAPHY_SMOKE is set, which only scripts/smoke.js does.
 */
function applySmokeEnv(): void {
	if (!process.env.ENGRAPHY_SMOKE) {
		return;
	}
	const dir = process.env.ENGRAPHY_USER_DATA;
	if (dir) {
		app.setPath('userData', dir);
	}
	if (process.env.ENGRAPHY_SMOKE_SEED) {
		const fs = require('fs') as typeof import('fs');
		const seed: Record<string, unknown> = {
			serverUrl: process.env.ENGRAPHY_SMOKE_URL ?? '',
			space: process.env.ENGRAPHY_SMOKE_SPACE ?? '',
			onboardingCompleted: process.env.ENGRAPHY_SMOKE_ONBOARDING ? false : true,
		};
		if (process.env.ENGRAPHY_SMOKE_TOKEN) {
			seed.tokenPlain = process.env.ENGRAPHY_SMOKE_TOKEN;
		}
		const target = settingsFilePath();
		fs.mkdirSync(path.dirname(target), { recursive: true });
		fs.writeFileSync(target, JSON.stringify(seed, null, 2), 'utf8');
	}
}
applySmokeEnv();

function connection(): EngraphyConnection {
	const s = loadSettings();
	return { serverUrl: s.serverUrl, token: s.token, space: s.space };
}
function serverConfigured(): boolean {
	return connection().serverUrl.trim().length > 0;
}
function currentHost(): string {
	return hostLabel(connection().serverUrl);
}

const client = new EngraphyClient(connection, APP_VERSION);

/**
 * Node types seen in this space, learned from search results.
 *
 * No MCP tool exposes a space's node types, and the static STARTER_NODE_TYPES
 * list is the starter pack's. A real space often differs: the live space this
 * was diagnosed against holds `note` and `project`, while the starter list
 * offers `project_ref`, so the promote form showed a type the space does not
 * use. Whatever search returned is the one honest signal available.
 */
let observedNodeTypes: string[] = [];

// ---- per-window panel state (single window) --------------------------------

const INBOX_LIMIT = 50;
const PENDING_LIMIT = 50;

const confirmState = {
	pending: [] as PendingListItem[],
	inbox: [] as InboxItemData[],
	inboxTruncated: false,
	/** Raw thrown values, kept unclassified so connection.ts can read error.code. */
	pendingError: null as unknown,
	inboxError: null as unknown,
	loading: false,
	/** False until the first reload finishes, so the first paint is a skeleton. */
	loaded: false,
};

const statsState = {
	groupBy: 'space' as 'space' | 'user',
	rangeDays: 30,
	vm: null as StatsViewVM | null,
	error: null as unknown,
	loading: false,
	loaded: false,
};

const graphState = {
	snapshot: null as GraphSnapshot | null,
	fromCache: false,
	building: false,
	cancelRequested: false,
	progress: null as GraphProgress | null,
	error: null as unknown,
	/** False until the cache has been consulted once, so the first paint is honest. */
	loaded: false,
};

/** Desktop supersets of the frozen ConfirmStateVM / StatsStateVM. */
interface DesktopConfirmStateVM {
	connection: DesktopConnectionVM;
	pending: PendingCardVM[];
	inbox: InboxCardVM[];
	pendingError: DescribedError | null;
	inboxError: DescribedError | null;
	inboxTruncated: boolean;
	loading: boolean;
	loaded: boolean;
}
interface DesktopStatsStateVM {
	connection: DesktopConnectionVM;
	loading: boolean;
	loaded: boolean;
	error: DescribedError | null;
	groupBy: 'space' | 'user';
	rangeDays: number;
	view: StatsViewVM | null;
}

// ---- push helpers ----------------------------------------------------------

/**
 * Push to the renderer. Guarded: `win` can be set but already destroyed while an
 * in-flight MCP call resolves during shutdown, and webContents.send on a
 * destroyed window throws.
 */
function push(channel: string, msg: unknown): void {
	if (win && !win.isDestroyed() && !win.webContents.isDestroyed()) {
		win.webContents.send('engraphy:msg', { channel, msg });
	}
}

function toast(text: string, level: 'info' | 'warn' | 'error' = 'info'): void {
	push('app', { type: 'toast', level, text });
}

function described(e: unknown): DescribedError | null {
	return e == null ? null : describeError(e, currentHost());
}


// ---- update checking -------------------------------------------------------
//
// versionCheck.ts decides whether a newer version is published; updateModel.ts
// decides what that means for this install, which depends on how it was
// installed. The whole thing is best-effort: it runs after the window is up,
// once a day at most, and every failure leaves the banner hidden.
//
// The renderer cannot do this itself. Its CSP is `connect-src 'none'`, so the
// window makes no network requests at all, and that stays true: the check runs
// here and the result arrives as a view-model like every other push.

const UPDATE_CHANNEL = detectChannel();
/** Give the window time to paint and connect before spending anything here. */
const UPDATE_STARTUP_DELAY_MS = 15_000;

let updateBanner: UpdateBannerVM | null = null;
let updateTimer: ReturnType<typeof setInterval> | undefined;

/** Node's fetch, typed to the narrow shape versionCheck asks for. */
const updateFetch = (globalThis as { fetch?: unknown }).fetch as Fetcher | undefined;

function pushUpdateState(): void {
	push('update', { type: 'state', vm: updateBanner ?? { visible: false } });
}

function manifestUrl(): string {
	return process.env.ENGRAPHY_MANIFEST_URL?.trim() || DEFAULT_MANIFEST_URL;
}

/**
 * Look for a newer published version.
 *
 * `manual` shows the answer whatever it is, because the user asked. The
 * scheduled path stays silent unless there is something to act on, and honours
 * a version the user has already dismissed.
 */
async function runUpdateCheck(manual: boolean): Promise<UpdateVerdict | null> {
	const state = loadUpdateState();
	if (!state.enabled && !manual) {
		return null;
	}
	if (!updateFetch) {
		return null;
	}
	const verdict = await checkForUpdate(
		app.getVersion(),
		manifestUrl(),
		updateFetch,
		PRODUCT_KEY
	);
	setUpdateChecked(Date.now());

	if (!manual && isDismissed(state.dismissed, verdict.latest)) {
		updateBanner = null;
		pushUpdateState();
		return verdict;
	}
	updateBanner = buildUpdateBanner(verdict, UPDATE_CHANNEL, process.platform, process.arch);
	pushUpdateState();
	if (manual && !updateBanner.visible) {
		toast(
			verdict.state === 'current'
				? `Engraphy ${verdict.current} is the current version.`
				: verdict.state === 'ahead'
					? `Engraphy ${verdict.current} is ahead of the published ${verdict.latest}.`
					: 'No update information is available right now.'
		);
	}
	return verdict;
}

/**
 * Arrange the background checks. The interval covers the app being left open
 * for days at a time, which is how a desktop app is actually used; shouldCheck
 * is what keeps a long-running window to one check a day.
 */
function scheduleUpdateChecks(): void {
	// A scripted smoke run seeds a fresh profile and takes screenshots, so it
	// must not race a banner onto the window.
	if (process.env.ENGRAPHY_SMOKE) {
		return;
	}
	setTimeout(() => {
		void runUpdateCheck(false).catch(() => undefined);
	}, UPDATE_STARTUP_DELAY_MS);
	updateTimer = setInterval(
		() => {
			const state = loadUpdateState();
			if (state.enabled && shouldCheck(state.lastChecked, Date.now(), CHECK_INTERVAL_MS)) {
				void runUpdateCheck(false).catch(() => undefined);
			}
		},
		60 * 60 * 1000
	);
	// An interval is the only thing here that would hold the process open.
	updateTimer.unref?.();
}

// ---- confirm queue (mirrors confirmWebview.ts) -----------------------------

function buildConfirmState(): DesktopConfirmStateVM {
	return {
		connection: computeConnection({
			serverConfigured: serverConfigured(),
			errors: [confirmState.pendingError ?? null, confirmState.inboxError ?? null],
			host: currentHost(),
			hasToken: connection().token.length > 0,
		}),
		pending: confirmState.pending.map((p) => buildPendingCard(p)),
		inbox: confirmState.inbox.map(buildInboxCard),
		pendingError: described(confirmState.pendingError),
		inboxError: described(confirmState.inboxError),
		inboxTruncated: confirmState.inboxTruncated,
		loading: confirmState.loading,
		loaded: confirmState.loaded,
	};
}
function pushConfirmState(): void {
	push('confirm', { type: 'state', state: buildConfirmState() });
}

async function reloadPending(): Promise<void> {
	try {
		const res = await client.pendingList(PENDING_LIMIT);
		confirmState.pending = pendingItemsFrom(res);
		confirmState.pendingError = null;
	} catch (e) {
		confirmState.pending = [];
		confirmState.pendingError = e;
	}
}
async function reloadInbox(): Promise<void> {
	try {
		const res = await client.inboxList(INBOX_LIMIT);
		const { items, truncated } = inboxItemsFrom(res);
		confirmState.inbox = items;
		confirmState.inboxTruncated = truncated;
		confirmState.inboxError = null;
	} catch (e) {
		confirmState.inbox = [];
		confirmState.inboxError = e;
	}
}
async function confirmReload(): Promise<void> {
	confirmState.loading = true;
	pushConfirmState();
	await Promise.all([reloadPending(), reloadInbox()]);
	confirmState.loading = false;
	confirmState.loaded = true;
	pushConfirmState();
	void refreshHealth();
}

async function resolve(pendingId: string, resolution: 'distinct' | 'merge', mergeInto?: string): Promise<void> {
	try {
		await client.resolveDuplicate(pendingId, resolution, mergeInto);
		toast(resolution === 'merge' ? 'Merged into the existing memory.' : 'Kept as a new, distinct memory.');
	} catch (e) {
		if (e instanceof EngraphyToolError && e.code === 'ENGRAPHY_PENDING_EXPIRED') {
			toast(
				'That pending write has expired. Re-issue the write for a fresh confirmation window.',
				'warn'
			);
		} else {
			toast('Engraphy: ' + describeError(e, currentHost()).summary, 'error');
		}
	} finally {
		await confirmReload();
	}
}

async function onDiscard(inboxId: string): Promise<void> {
	const item = confirmState.inbox.find((i) => i.id === inboxId);
	const label = item ? previewLabel(item) : inboxId;
	if (!win || win.isDestroyed()) {
		return;
	}
	const { response } = await dialog.showMessageBox(win, {
		type: 'warning',
		buttons: ['Cancel', 'Discard'],
		defaultId: 1,
		cancelId: 0,
		message: 'Discard inbox item?',
		detail: '"' + label + '" will be dropped from the inbox. This cannot be undone.',
	});
	if (response !== 1) {
		pushConfirmState(); // clear the view's optimistic busy state
		return;
	}
	try {
		await client.inboxDiscard(inboxId);
		toast('Item discarded.');
	} catch (e) {
		toast('Engraphy: ' + describeError(e, currentHost()).summary, 'error');
	} finally {
		await confirmReload();
	}
}

async function onPromoteSubmit(p: {
	inboxId: string;
	nodeType: string;
	scope: string;
	title: string;
	body: string;
}): Promise<void> {
	try {
		const res = (await client.inboxPromote({
			id: p.inboxId,
			type: p.nodeType,
			scope: p.scope,
			title: p.title,
			body: p.body,
		})) as { outcome?: string; pending_id?: string };
		if (res.outcome === 'needs_confirmation' && res.pending_id) {
			toast(
				'Promotion parked as a pending duplicate. Resolve it in the Pending duplicates band.',
				'warn'
			);
		} else {
			toast('Promoted (' + (res.outcome ?? 'ok') + ').');
		}
	} catch (e) {
		toast('Engraphy: ' + describeError(e, currentHost()).summary, 'error');
	} finally {
		await confirmReload();
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

// ---- stats (mirrors statsWebview.ts) ---------------------------------------

function buildStatsState(): DesktopStatsStateVM {
	return {
		connection: computeConnection({
			serverConfigured: serverConfigured(),
			errors: [statsState.error ?? null],
			host: currentHost(),
			hasToken: connection().token.length > 0,
		}),
		loading: statsState.loading,
		loaded: statsState.loaded,
		error: described(statsState.error),
		groupBy: statsState.groupBy,
		rangeDays: statsState.rangeDays,
		view: statsState.vm,
	};
}
function pushStatsState(): void {
	push('stats', { type: 'state', state: buildStatsState() });
}
async function statsReload(): Promise<void> {
	statsState.loading = true;
	pushStatsState();
	try {
		const res = await client.stats(statsState.rangeDays, statsState.groupBy);
		statsState.vm = buildStatsView(res);
		statsState.error = null;
	} catch (e) {
		statsState.vm = null;
		statsState.error = e;
	}
	statsState.loading = false;
	statsState.loaded = true;
	pushStatsState();
	void refreshHealth();
}

// ---- graph -----------------------------------------------------------------
//
// The snapshot is EXPENSIVE to build (graphHarvest.ts explains why: Engraphy has
// no whole-graph read, and the per-token budget is 60 reads a minute), so it is
// cached to disk per space and only rebuilt when the user asks. The panel opens
// on the cache instantly and shows how old it is.

function graphCachePath(space: string): string {
	// One file per space: the same app pointed at a different space must not read
	// the previous space's graph, and RLS means a token can only ever see one.
	const safe = (space || 'default').replace(/[^a-zA-Z0-9._-]/g, '_');
	return path.join(app.getPath('userData'), `engraphy-graph-${safe}.json`);
}

function loadGraphCache(space: string): GraphSnapshot | null {
	try {
		const snap = parseSnapshot(JSON.parse(fs.readFileSync(graphCachePath(space), 'utf8')));
		// A file written for another space is not this space's graph.
		return snap && snap.space === space ? snap : null;
	} catch {
		return null;
	}
}

function saveGraphCache(snap: GraphSnapshot): void {
	try {
		const file = graphCachePath(snap.space);
		fs.mkdirSync(path.dirname(file), { recursive: true });
		fs.writeFileSync(file, JSON.stringify(snap), { encoding: 'utf8', mode: 0o600 });
	} catch {
		// A cache that will not persist is a slow next open, never a failure.
	}
}

/**
 * Strip what does not belong on a user-facing graph, then drop edges whose far
 * endpoint is not in the set. Applied on the way OUT rather than during the
 * harvest so the cache keeps everything the walk actually saw.
 */
function renderableSnapshot(snap: GraphSnapshot | null): GraphSnapshot | null {
	if (!snap) {
		return null;
	}
	const nodes = snap.nodes.filter(isRenderableNode);
	return { ...snap, nodes, edges: pruneDanglingEdges(nodes, snap.edges) };
}

function buildGraphState(): GraphStateVM {
	return {
		building: graphState.building,
		progress: graphState.progress,
		error: described(graphState.error)?.summary ?? null,
		snapshot: renderableSnapshot(graphState.snapshot),
		fromCache: graphState.fromCache,
		serverConfigured: serverConfigured(),
	};
}

function pushGraphState(): void {
	push('graph', { type: 'state', state: buildGraphState() });
}

/** Read the cache once per app run, so opening the panel is instant. */
function graphLoadCache(): void {
	if (graphState.loaded) {
		return;
	}
	graphState.loaded = true;
	const snap = loadGraphCache(connection().space);
	if (snap) {
		graphState.snapshot = snap;
		graphState.fromCache = true;
	}
}

/** The harvest's four reads, bound to the shared MCP client. */
const graphTools: GraphTools = {
	scopeList: () => client.scopeList(),
	briefing: (scope, hint) => client.briefing(scope, hint),
	traverse: (o) =>
		client.traverse({
			startId: o.startId,
			direction: o.direction,
			maxDepth: o.maxDepth,
			limit: o.limit,
			detail: o.detail,
			edgeTypes: o.edgeTypes,
		}),
	search: (o) => client.search({ scope: o.scope, query: o.query, limit: o.limit, detail: o.detail }),
};

async function graphBuild(deepSweep: boolean): Promise<void> {
	if (graphState.building) {
		return;
	}
	if (!serverConfigured()) {
		graphState.error = new Error('engraphy.serverUrl is not set.');
		pushGraphState();
		return;
	}
	graphState.building = true;
	graphState.cancelRequested = false;
	graphState.error = null;
	graphState.progress = null;
	pushGraphState();

	const space = connection().space;
	try {
		const snap = await harvestGraph(graphTools, {
			space,
			deepSweep,
			onProgress: (p) => {
				graphState.progress = p;
				push('graph', { type: 'progress', progress: p });
			},
			isCancelled: () => graphState.cancelRequested,
		});
		graphState.snapshot = snap;
		graphState.fromCache = false;
		saveGraphCache(snap);
		const c = renderableSnapshot(snap);
		toast(
			`Graph rebuilt: ${c?.nodes.length ?? 0} memories, ${c?.edges.length ?? 0} links, ` +
				`${snap.stats.briefingCalls + snap.stats.traverseCalls + snap.stats.searchCalls} reads.`
		);
	} catch (e) {
		if (e instanceof Cancelled) {
			toast('Graph build cancelled. The previous graph is still shown.', 'warn');
		} else {
			graphState.error = e;
			// Also toasted, because a build runs for minutes and the user is very
			// likely looking at another panel when it gives up.
			toast('Graph index failed: ' + (described(e)?.summary ?? 'unknown error'), 'error');
		}
	}
	graphState.building = false;
	graphState.progress = null;
	pushGraphState();
	void refreshHealth();
}

// ---- health ----------------------------------------------------------------

let healthInFlight = false;
let lastHealth: HealthVM | null = null;

/**
 * Probe the server two ways and push a single badge state.
 *
 * Both probes matter. /healthz is UNAUTHENTICATED on Engraphy, so on its own it
 * reports a cheerful green "Connected" for a server whose every tool call 401s
 * (exactly what happens on a fresh install pointed at a running local server
 * with no token yet). scope_list is the probe that actually decides whether the
 * app can read anything.
 *
 * WHY scope_list SPECIFICALLY, and do not swap it for `search`: this runs every
 * 20 seconds for as long as the app is open. engraphy/core/scopes.py's scope_list
 * is a plain RLS-filtered SELECT that records NO metrics. `search`, by contrast,
 * increments ANSWERED and MEMORY_REUSED (engraphy/core/search.py), so polling it
 * would have the app quietly inflating the very numbers it shows the user on the
 * Impact panel. Keep the health probe on a tool that records nothing.
 */
async function refreshHealth(): Promise<void> {
	if (!serverConfigured()) {
		lastHealth = buildHealthVM({
			configured: false,
			hasToken: false,
			space: '',
			serverUrl: '',
			info: null,
			authOk: false,
		});
		push('health', lastHealth);
		return;
	}
	if (healthInFlight) {
		return;
	}
	healthInFlight = true;
	const conn = connection();
	try {
		let info: { status?: string; version?: string; spaces?: number } | null = null;
		let healthzError: unknown;
		try {
			info = await client.health();
		} catch (e) {
			healthzError = e;
		}

		let authOk = false;
		let authError: unknown;
		if (graphState.building) {
			// A graph index spends the whole per-token read budget on purpose, so a
			// probe fired mid-build loses the race and comes back RATE_LIMITED. That
			// used to paint a red "Server error" badge and a disconnected banner over
			// a panel that was, at that exact moment, successfully reading the
			// server. The build IS the liveness proof; the probe resumes when it ends.
			authOk = true;
		} else {
			try {
				await client.scopeList();
				authOk = true;
			} catch (e) {
				authError = e;
			}
		}

		lastHealth = buildHealthVM({
			configured: true,
			hasToken: conn.token.length > 0,
			space: conn.space,
			serverUrl: conn.serverUrl,
			info,
			healthzError,
			authOk,
			authError,
		});
		push('health', lastHealth);
	} finally {
		healthInFlight = false;
	}
}

/** Drop the MCP session and re-probe. The user-facing "Reconnect". */
async function reconnect(): Promise<void> {
	await client.reconnect();
	push('health', { phase: 'checking', label: 'Reconnecting…', title: 'Reconnecting…', usable: false });
	await refreshHealth();
	await Promise.all([confirmReload(), statsReload()]);
}

/**
 * Probe an arbitrary URL/token WITHOUT persisting anything.
 *
 * Saving first and probing second is the naive shape, and it means testing a
 * typo destroys the working connection you already had. This builds a throwaway
 * client bound to the candidate values instead.
 */
async function testConnection(candidate: {
	serverUrl: string;
	token?: string;
	space?: string;
}): Promise<
	| { ok: true; info: unknown; scopes: string[]; authOk: boolean }
	| { ok: false; error: DescribedError }
> {
	const stored = loadSettings();
	const conn: EngraphyConnection = {
		serverUrl: candidate.serverUrl,
		// undefined means "use the token already stored", matching save semantics.
		token: candidate.token === undefined ? stored.token : candidate.token,
		space: candidate.space ?? stored.space,
	};
	const host = hostLabel(conn.serverUrl);
	const probe = new EngraphyClient(() => conn, APP_VERSION);
	try {
		let info: unknown = null;
		try {
			info = await probe.health();
		} catch {
			// /healthz is a nice-to-have; the authenticated call below is the real test.
		}
		const scopes = scopeIdsFrom(await probe.scopeList());
		return { ok: true, info, scopes, authOk: true };
	} catch (e) {
		return { ok: false, error: describeError(e, host) };
	} finally {
		await probe.close();
	}
}

// ---- fire-and-forget message router (renderer → main) ----------------------

/**
 * Route a renderer message.
 *
 * Every payload goes through a parser first. The copied client ships
 * `parseWebviewMessage` / `parseStatsMessage`, whose own comments call them the
 * trust boundary, but nothing called them: this router read `msg.pendingId` and
 * friends straight off the raw object. src/main/messages.ts now delegates to
 * those parsers for the commands they cover and validates the two desktop-only
 * additions itself, so an unparseable message is dropped rather than acted on.
 */
async function handleMessage(channel: string, msg: unknown): Promise<void> {
	if (channel === 'confirm') {
		const cmd = parseConfirmCommand(msg);
		if (!cmd) {
			return;
		}
		switch (cmd.type) {
			case 'ready':
			case 'refresh':
				return void confirmReload();
			case 'approve':
				return void resolve(cmd.pendingId, 'distinct');
			case 'merge':
				return void resolve(cmd.pendingId, 'merge', cmd.mergeInto);
			case 'discard':
				return void onDiscard(cmd.inboxId);
			case 'promoteSubmit':
				return void onPromoteSubmit(cmd);
			case 'reconnect':
				return void reconnect();
			case 'openWalkthrough':
				return void push('app', { type: 'onboarding' });
			case 'configureServer':
				return void push('app', { type: 'navigate', to: 'settings' });
		}
		return;
	}

	if (channel === 'update') {
		const cmd = parseUpdateCommand(msg);
		if (!cmd) {
			return;
		}
		switch (cmd.type) {
			case 'ready':
				return pushUpdateState();
			case 'check':
				return void runUpdateCheck(true).catch(() => undefined);
			case 'dismiss':
				// Per version: saying "not now" to this one says nothing about
				// the next one, so the check keeps running.
				if (updateBanner?.latest) {
					setUpdateDismissed(updateBanner.latest);
				}
				updateBanner = null;
				return pushUpdateState();
		}
		return;
	}

	if (channel === 'stats') {
		const cmd = parseStatsCommand(msg);
		if (!cmd) {
			return;
		}
		switch (cmd.type) {
			case 'ready':
			case 'refresh':
				return void statsReload();
			case 'setRange':
				statsState.rangeDays = cmd.rangeDays;
				return void statsReload();
			case 'setGroup':
				statsState.groupBy = cmd.groupBy;
				return void statsReload();
			case 'reconnect':
				return void reconnect();
			case 'openWalkthrough':
				return void push('app', { type: 'onboarding' });
			case 'configureServer':
				return void push('app', { type: 'navigate', to: 'settings' });
		}
		return;
	}

	if (channel === 'graph') {
		const cmd = parseGraphMessage(msg);
		if (!cmd) {
			return;
		}
		switch (cmd.type) {
			case 'ready':
				graphLoadCache();
				return void pushGraphState();
			case 'build':
				return void graphBuild(cmd.deepSweep);
			case 'cancel':
				graphState.cancelRequested = true;
				return;
			case 'clearCache':
				try {
					fs.rmSync(graphCachePath(connection().space), { force: true });
				} catch {
					// Nothing to clear is the same outcome as clearing it.
				}
				graphState.snapshot = null;
				graphState.fromCache = false;
				return void pushGraphState();
		}
		return;
	}

	if (channel === 'app') {
		const cmd = parseAppCommand(msg);
		if (!cmd) {
			return;
		}
		if (cmd.type === 'reconnect') {
			return void reconnect();
		}
		if (cmd.type === 'refreshAll') {
			void confirmReload();
			void statsReload();
		}
	}
}

// ---- request/response handlers (renderer → main → result) ------------------

async function handleInvoke(channel: string, msg: any): Promise<unknown> {
	if (channel === 'settings') {
		return handleSettingsInvoke(msg);
	}
	if (channel === 'onboarding') {
		return handleOnboardingInvoke(msg);
	}
	if (channel === 'health') {
		if (msg?.type === 'check') {
			void refreshHealth();
			return ok({});
		}
		if (msg?.type === 'reconnect') {
			await reconnect();
			return ok({ health: lastHealth });
		}
		if (msg?.type === 'last') {
			return ok({ health: lastHealth });
		}
	}
	if (channel === 'explorer') {
		return handleExplorerInvoke(msg);
	}
	if (channel === 'graph') {
		// The snapshot carries titles only (traverse/briefing return `summary`
		// envelopes), so opening a node in the inspector fetches its body on
		// demand. One `get` per click is cheap and keeps the snapshot small.
		if (msg?.type === 'get') {
			if (!serverConfigured()) {
				return fail(new Error('engraphy.serverUrl is not set.'), currentHost());
			}
			const id = String(msg.id);
			try {
				const res = await client.get([id]);
				return ok({ node: nodeDetailFrom(res, id), missing: missingIdsFrom(res) });
			} catch (e) {
				return fail(e, currentHost());
			}
		}
	}
	if (channel === 'confirm') {
		if (msg?.type === 'promotePrepare') {
			const item = confirmState.inbox.find((i) => i.id === msg.inboxId);
			if (!item) {
				return fail(new Error('That inbox item is no longer in the queue. Refresh and try again.'));
			}
			const defaults = promoteDefaults(item.payload);
			const scopes = item.scope ? [] : await readableScopes();
			return ok({
				nodeTypes: mergeNodeTypes(observedNodeTypes, STARTER_NODE_TYPES),
				defaults,
				scope: item.scope ?? null,
				needsScope: !item.scope,
				scopes,
			});
		}
	}
	return fail(new Error('Unhandled request ' + channel + '/' + (msg?.type ?? '?')));
}

async function handleSettingsInvoke(msg: any): Promise<unknown> {
	if (msg?.type === 'load') {
		return ok({ settings: loadSafeSettings(), defaultServerUrl: DEFAULT_SERVER_URL });
	}

	if (msg?.type === 'setUpdateCheck') {
		// Read strictly, not for truthiness: a malformed payload must not be
		// able to turn the check off.
		const enabled = msg.enabled === true;
		setUpdateCheckEnabled(enabled);
		if (!enabled) {
			updateBanner = null;
			pushUpdateState();
		}
		return ok({ settings: loadSafeSettings() });
	}

	if (msg?.type === 'validate') {
		// Live feedback as the user types. Never touches disk or the network.
		return ok({
			validation: validateSettingsInput({
				serverUrl: String(msg.serverUrl ?? ''),
				token: msg.token === undefined ? undefined : String(msg.token),
				space: String(msg.space ?? ''),
			}),
		});
	}

	if (msg?.type === 'test') {
		const v = validateSettingsInput({
			serverUrl: String(msg.serverUrl ?? ''),
			token: msg.token === undefined ? undefined : String(msg.token),
			space: String(msg.space ?? ''),
		});
		if (!v.ok) {
			return ok({ validation: v, result: null, blocked: firstError(v) });
		}
		const result = await testConnection({
			serverUrl: v.serverUrl.normalized,
			token: msg.token === undefined ? undefined : v.token.normalized,
			space: v.space.normalized,
		});
		return ok({ validation: v, result, blocked: null });
	}

	if (msg?.type === 'save') {
		const v = validateSettingsInput({
			serverUrl: String(msg.serverUrl ?? ''),
			token: msg.token === undefined ? undefined : String(msg.token),
			space: String(msg.space ?? ''),
		});
		if (!v.ok) {
			return ok({ saved: false, validation: v, blocked: firstError(v) });
		}
		const settings: SafeSettings = saveSettings({
			serverUrl: v.serverUrl.normalized,
			space: v.space.normalized,
			// undefined = keep existing; '' = clear; string = set
			token: msg.token === undefined ? undefined : v.token.normalized,
		});
		await client.reconnect();

		// Report the post-save reality with the same two-probe logic the badge uses.
		let info: unknown = null;
		let probeError: DescribedError | null = null;
		try {
			info = await client.health();
		} catch {
			// non-fatal; the authenticated probe below decides
		}
		let authOk = false;
		try {
			await client.scopeList();
			authOk = true;
		} catch (e) {
			probeError = describeError(e, hostLabel(v.serverUrl.normalized));
		}
		void refreshHealth();
		void confirmReload();
		void statsReload();
		// The graph is per-space and RLS means a new token can see a different
		// one, so the in-memory snapshot is dropped and the new space's cache
		// consulted instead of showing the previous space's picture.
		graphState.loaded = false;
		graphState.snapshot = null;
		graphState.fromCache = false;
		graphLoadCache();
		pushGraphState();
		return ok({ saved: true, validation: v, settings, info, authOk, probeError });
	}

	if (msg?.type === 'revealSettingsFile') {
		shell.showItemInFolder(settingsFilePath());
		return ok({ path: settingsFilePath() });
	}

	return fail(new Error('Unhandled settings request ' + (msg?.type ?? '?')));
}

async function handleOnboardingInvoke(msg: any): Promise<unknown> {
	if (msg?.type === 'state') {
		return ok({
			completed: isOnboardingCompleted(),
			settings: loadSafeSettings(),
			defaultServerUrl: DEFAULT_SERVER_URL,
			repoUrl: ENGRAPHY_REPO_URL,
			version: APP_VERSION,
		});
	}
	if (msg?.type === 'complete') {
		setOnboardingCompleted(true);
		return ok({ completed: true });
	}
	if (msg?.type === 'reopen') {
		setOnboardingCompleted(false);
		return ok({ completed: false });
	}
	return fail(new Error('Unhandled onboarding request ' + (msg?.type ?? '?')));
}

async function handleExplorerInvoke(msg: any): Promise<unknown> {
	const host = currentHost();
	if (!serverConfigured()) {
		return fail(new Error('engraphy.serverUrl is not set.'), host);
	}
	try {
		if (msg?.type === 'search') {
			const res = await client.search({
				scope: String(msg.scope || 'all'),
				query: String(msg.query || ''),
			});
			const nodes = nodeRefsFromSearch(res);
			const seen = observedTypesFrom(nodes);
			if (seen.length) {
				observedNodeTypes = Array.from(new Set([...observedNodeTypes, ...seen])).sort();
			}
			return ok({ nodes });
		}
		if (msg?.type === 'traverse') {
			const id = String(msg.id);
			const res = await client.traverse({
				startId: id,
				direction: 'both',
				maxDepth: 1,
				detail: 'summary',
			});
			return ok({ nodes: nodeRefsFromTraverse(res, id) });
		}
		if (msg?.type === 'get') {
			const id = String(msg.id);
			const res = await client.get([id]);
			// A missing id is information, not an error (Engraphy collapses "unknown"
			// and "not readable by your token" on purpose), so this is ok:true with
			// a null node, not a failure.
			return ok({ node: nodeDetailFrom(res, id), missing: missingIdsFrom(res) });
		}
		if (msg?.type === 'scopes') {
			return ok({ scopes: scopeIdsFrom(await client.scopeList()) });
		}
	} catch (e) {
		return fail(e, host);
	}
	return fail(new Error('Unhandled explorer request ' + (msg?.type ?? '?')));
}

async function readableScopes(): Promise<string[]> {
	try {
		return scopeIdsFrom(await client.scopeList());
	} catch {
		return [];
	}
}

// ---- application menu ------------------------------------------------------

/**
 * A real menu, because `win.removeMenu()` is a shipping defect on macOS: with no
 * menu there is no Cmd+Q, Cmd+C/V, or Cmd+W, and those are not optional on that
 * platform. Windows/Linux keep a hidden menu (accelerators still fire) so the
 * custom title bar stays clean.
 */
function buildMenu(): void {
	const isMac = process.platform === 'darwin';
	const navItem = (label: string, to: string, accel: string) => ({
		label,
		accelerator: accel,
		click: () => push('app', { type: 'navigate', to }),
	});

	const template: Electron.MenuItemConstructorOptions[] = [
		...(isMac
			? [
					{
						role: 'appMenu' as const,
						submenu: [
							{ role: 'about' as const },
							{ type: 'separator' as const },
							{
								label: 'Settings…',
								accelerator: 'Cmd+,',
								click: () => push('app', { type: 'navigate', to: 'settings' }),
							},
							{ type: 'separator' as const },
							{ role: 'services' as const },
							{ type: 'separator' as const },
							{ role: 'hide' as const },
							{ role: 'hideOthers' as const },
							{ role: 'unhide' as const },
							{ type: 'separator' as const },
							{ role: 'quit' as const },
						],
					},
				]
			: []),
		{
			label: 'File',
			submenu: [
				...(isMac
					? []
					: [
							{
								label: 'Settings',
								accelerator: 'Ctrl+,',
								click: () => push('app', { type: 'navigate', to: 'settings' }),
							},
							{ type: 'separator' as const },
						]),
				isMac ? { role: 'close' as const } : { role: 'quit' as const },
			],
		},
		{ role: 'editMenu' },
		{
			label: 'View',
			submenu: [
				navItem('Memories', 'explorer', 'CmdOrCtrl+1'),
				navItem('Graph', 'graph', 'CmdOrCtrl+2'),
				navItem('Impact & usage', 'stats', 'CmdOrCtrl+3'),
				navItem('Confirm-write queue', 'confirm', 'CmdOrCtrl+4'),
				navItem('Settings', 'settings', 'CmdOrCtrl+5'),
				{ type: 'separator' },
				{
					label: 'Refresh',
					accelerator: 'CmdOrCtrl+R',
					click: () => {
						void confirmReload();
						void statsReload();
					},
				},
				{
					label: 'Reconnect',
					accelerator: 'CmdOrCtrl+Shift+R',
					click: () => void reconnect(),
				},
				{ type: 'separator' },
				{ role: 'resetZoom' },
				{ role: 'zoomIn' },
				{ role: 'zoomOut' },
				{ type: 'separator' },
				{ role: 'togglefullscreen' },
			],
		},
		{ role: 'windowMenu' },
		{
			role: 'help',
			submenu: [
				{
					label: 'Set up Engraphy…',
					click: () => push('app', { type: 'onboarding' }),
				},
				{
					label: 'Engraphy on GitHub',
					click: () => void shell.openExternal(ENGRAPHY_REPO_URL),
				},
				{
					label: 'Show settings file',
					click: () => shell.showItemInFolder(settingsFilePath()),
				},
			],
		},
	];

	Menu.setApplicationMenu(Menu.buildFromTemplate(template));
	if (!isMac) {
		// Keep accelerators, drop the visible bar (the custom title bar owns the top).
		win?.setMenuBarVisibility(false);
	}
}

// ---- window chrome ---------------------------------------------------------

const CHROME = {
	light: { bg: '#f3f1e6', symbol: '#2a3626' },
	dark: { bg: '#191d18', symbol: '#e8eae0' },
};

function chromeColors() {
	return nativeTheme.shouldUseDarkColors ? CHROME.dark : CHROME.light;
}

function persistBounds(): void {
	if (!win || win.isDestroyed()) {
		return;
	}
	const maximized = win.isMaximized();
	// Store the restored geometry, not the maximized rect, so unmaximizing later
	// lands somewhere sensible.
	const b = maximized ? win.getNormalBounds() : win.getBounds();
	saveWindowBounds({ x: b.x, y: b.y, width: b.width, height: b.height, maximized });
}

function createWindow(): void {
	const saved = sanitizeSavedBounds(loadWindowBounds());
	const colors = chromeColors();
	const isMac = process.platform === 'darwin';
	const isWin = process.platform === 'win32';

	win = new BrowserWindow({
		// Position is only restored when it still lands on a display that exists
		// right now, so a window last closed on a since-unplugged monitor does not
		// reopen off-screen with no way to drag it back.
		...restoreBounds(saved, screen.getAllDisplays().map((d) => d.workArea)),
		minWidth: MIN_WIDTH,
		minHeight: MIN_HEIGHT,
		show: false,
		title: 'Engraphy',
		backgroundColor: colors.bg,
		icon: path.join(__dirname, 'renderer', 'assets', 'icon.png'),
		// Brand chrome: the app paints its own title bar on Windows and macOS and
		// keeps the OS window controls. Linux keeps the native frame (no overlay
		// API there).
		...(isMac ? { titleBarStyle: 'hiddenInset' as const, trafficLightPosition: { x: 14, y: 15 } } : {}),
		...(isWin
			? {
					titleBarStyle: 'hidden' as const,
					titleBarOverlay: { color: colors.bg, symbolColor: colors.symbol, height: 38 },
				}
			: {}),
		webPreferences: {
			preload: path.join(__dirname, 'preload.js'),
			contextIsolation: true,
			nodeIntegration: false,
			sandbox: true,
		},
	});

	if (saved?.maximized) {
		win.maximize();
	}
	win.once('ready-to-show', () => {
		// Show only once the renderer has painted, so no white/cream flash.
		if (!process.env.ENGRAPHY_SMOKE) {
			win?.show();
		}
	});

	void win.loadFile(path.join(__dirname, 'renderer', 'index.html'));

	let boundsTimer: NodeJS.Timeout | undefined;
	const scheduleBoundsSave = () => {
		clearTimeout(boundsTimer);
		boundsTimer = setTimeout(persistBounds, 400);
	};
	win.on('resize', scheduleBoundsSave);
	win.on('move', scheduleBoundsSave);
	win.on('maximize', scheduleBoundsSave);
	win.on('unmaximize', scheduleBoundsSave);
	win.on('close', () => {
		clearTimeout(boundsTimer);
		persistBounds();
	});
	win.on('closed', () => {
		win = undefined;
	});

	// Anything the app itself did not render opens in the real browser, never in
	// an app window.
	win.webContents.setWindowOpenHandler(({ url }) => {
		if (/^https?:\/\//i.test(url)) {
			void shell.openExternal(url);
		}
		return { action: 'deny' };
	});
	win.webContents.on('will-navigate', (e, url) => {
		if (!url.startsWith('file://')) {
			e.preventDefault();
			if (/^https?:\/\//i.test(url)) {
				void shell.openExternal(url);
			}
		}
	});

	// A renderer crash must not leave a blank window with no way out.
	win.webContents.on('render-process-gone', (_e, details) => {
		if (!win || win.isDestroyed()) {
			return;
		}
		void dialog
			.showMessageBox(win, {
				type: 'error',
				buttons: ['Reload', 'Quit'],
				defaultId: 0,
				message: 'Engraphy stopped responding',
				detail: 'The window process ended (' + details.reason + '). Reload to continue.',
			})
			.then(({ response }) => {
				if (response === 0) {
					win?.reload();
				} else {
					app.quit();
				}
			});
	});

	if (process.env.ENGRAPHY_SMOKE) {
		installSmokeHarness();
	}
}

function applyThemeToChrome(): void {
	const colors = chromeColors();
	if (!win || win.isDestroyed()) {
		return;
	}
	win.setBackgroundColor(colors.bg);
	if (process.platform === 'win32') {
		try {
			win.setTitleBarOverlay({ color: colors.bg, symbolColor: colors.symbol, height: 38 });
		} catch {
			// setTitleBarOverlay throws if the window was not created with an overlay
		}
	}
	push('app', { type: 'theme', dark: nativeTheme.shouldUseDarkColors });
}

// ---- app lifecycle ---------------------------------------------------------

// One window only. A second launch focuses the existing one instead of opening a
// duplicate that would fight over the same settings file.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
	app.quit();
} else {
	app.on('second-instance', () => {
		if (win && !win.isDestroyed()) {
			if (win.isMinimized()) {
				win.restore();
			}
			win.show();
			win.focus();
		}
	});

	app.whenReady().then(() => {
		// Before the window exists, so a machine that was set up by the Windows
		// installer opens straight into a connected app rather than into
		// onboarding that asks for a token the installer already minted. A no-op
		// on every launch after the first, and on every platform that has no
		// installer handoff. See bootstrap.ts.
		importBootstrapFile();

		ipcMain.on('engraphy:msg', (_e, payload: { channel: string; msg: unknown }) => {
			void handleMessage(payload.channel, payload.msg);
		});
		ipcMain.handle('engraphy:invoke', async (_e, payload: { channel: string; msg: unknown }) => {
			try {
				return await handleInvoke(payload.channel, payload.msg);
			} catch (e) {
				return fail(e, currentHost());
			}
		});
		ipcMain.handle('engraphy:openExternal', (_e, url: string) => {
			if (typeof url === 'string' && /^https?:\/\//i.test(url)) {
				void shell.openExternal(url);
			}
		});

		createWindow();
		buildMenu();
		nativeTheme.on('updated', applyThemeToChrome);
		scheduleUpdateChecks();

		app.on('activate', () => {
			if (BrowserWindow.getAllWindows().length === 0) {
				createWindow();
				buildMenu();
			}
		});
	});
}

app.on('window-all-closed', () => {
	void client.close();
	if (process.platform !== 'darwin') {
		app.quit();
	}
});

// ---- headless self-check (dev only, env-gated) -----------------------------

/**
 * With ENGRAPHY_SMOKE unset (every normal and packaged launch) none of this
 * runs. With it set, the app drives itself, prints a DOM/state snapshot per
 * panel, optionally captures screenshots, and exits. This is how each new
 * empty / loading / disconnected / unauthorized state is verified, since those
 * states cannot be asserted from a unit test.
 */
function installSmokeHarness(): void {
	if (!win) {
		return;
	}
	win.webContents.on('did-finish-load', () => {
		// The loading scenario must look while the first reads are still in
		// flight; every other scenario wants them settled.
		const delay = process.env.ENGRAPHY_SMOKE_LOADING ? 500 : 3000;
		setTimeout(async () => {
			try {
				await runSmoke();
			} catch (e) {
				// eslint-disable-next-line no-console
				console.log('ENGRAPHY_SMOKE_ERROR ' + String(e));
			}
			app.exit(0);
		}, delay);
	});
}

async function runSmoke(): Promise<void> {
	const w = win!;
	const js = (code: string) => w.webContents.executeJavaScript(code);
	const log = (tag: string, v: unknown) => {
		// eslint-disable-next-line no-console
		console.log(tag + ' ' + JSON.stringify(v));
	};

	// With a slow server, snapshot BEFORE the first reads land, so the loading
	// skeletons are actually observed. Without this the panels have long since
	// finished by the time the harness looks, and the skeleton path has zero
	// coverage in either suite.
	if (process.env.ENGRAPHY_SMOKE_LOADING) {
		const loading = await js(`(function(){
			const q = (sel) => document.querySelector(sel);
			const count = (sel) => document.querySelectorAll(sel).length;
			return {
				confirmSkeletons: count('.panel[data-panel="confirm"] .skeleton-card'),
				statsSkeletons: count('.panel[data-panel="stats"] .skeleton-card'),
				confirmCards: count('.panel[data-panel="confirm"] .card'),
				healthPhase: q('#health') ? q('#health').getAttribute('data-phase') : null,
				blank: document.body.innerText.trim().length === 0
			};
		})()`);
		// eslint-disable-next-line no-console
		console.log('ENGRAPHY_SMOKE_LOADING ' + JSON.stringify(loading));
	}

	// Dismiss the first-run overlay unless we are explicitly shooting it.
	if (!process.env.ENGRAPHY_SMOKE_ONBOARDING) {
		await js(`(function(){
			var s = document.getElementById('onboarding-host');
			if (s) { s.textContent = ''; }
			return true;
		})()`);
	}

	const report = await js(`(function(){
		const q = (sel) => document.querySelector(sel);
		const txt = (sel) => (q(sel) ? q(sel).textContent.trim() : null);
		const count = (sel) => document.querySelectorAll(sel).length;
		return {
			health: txt('#health .health-text'),
			healthPhase: q('#health') ? q('#health').getAttribute('data-phase') : null,
			banner: txt('#conn-banner .banner-text'),
			confirmCards: count('.panel[data-panel="confirm"] .card'),
			confirmSections: count('.panel[data-panel="confirm"] .section'),
			confirmRecovery: count('.panel[data-panel="confirm"] [data-recovery]'),
			confirmRecoveryKind: q('.panel[data-panel="confirm"] [data-recovery]') ? q('.panel[data-panel="confirm"] [data-recovery]').getAttribute('data-recovery') : null,
			confirmEmpty: count('.panel[data-panel="confirm"] .empty'),
			statsTiles: count('.panel[data-panel="stats"] .tile'),
			statsRecovery: count('.panel[data-panel="stats"] [data-recovery]'),
			explorerHasSearch: !!q('.panel[data-panel="explorer"] .explorer-search input'),
			settingsUrl: q('.panel[data-panel="settings"] input') ? q('.panel[data-panel="settings"] input').value : null,
			onboardingOpen: !!q('#onboarding-host .ob-panel'),
			onboardingTitle: txt('#onboarding-host .ob-title'),
			onboardingSteps: count('#onboarding-host .ob-dot'),
			titlebar: !!q('#titlebar'),
			blank: document.body.innerText.trim().length === 0,
			errorText: (q('.note.error') ? q('.note.error').textContent.trim().slice(0,120) : null)
		};
	})()`);
	log('ENGRAPHY_SMOKE_REPORT', report);

	const searchResult = await js(`(async function(){
		const p = document.querySelector('.panel[data-panel="explorer"]');
		document.querySelector('.nav-item[data-nav="explorer"]').click();
		const input = p.querySelector('.explorer-search input');
		// What the user sees on OPEN, before typing anything. The panel used to sit
		// on an idle prompt here and run no query, which is why a working server
		// looked empty. It now auto-browses, so these rows should already exist.
		const autoRows = p.querySelectorAll('.node-row').length;
		const autoStateTitle = (p.querySelector('.state-block .state-title')||{}).textContent || null;
		input.value = ${JSON.stringify(process.env.ENGRAPHY_SMOKE_QUERY ?? 'ada')};
		p.querySelector('.explorer-search .btn').click();
		await new Promise(r => setTimeout(r, 1200));
		const out = {
			autoRows: autoRows,
			autoStateTitle: autoStateTitle,
			rows: p.querySelectorAll('.node-row').length,
			first: (p.querySelector('.node-title')||{}).textContent || null,
			state: (p.querySelector('.state-block .state-title')||{}).textContent || null,
			blank: p.textContent.trim().length === 0
		};
		if (!out.rows) { return out; }

		// Open the record (get) and follow the links (traverse). Both were
		// rewritten this round, and both are joins between a unit-tested shaper
		// and untested render code, which is exactly the shape the Promote bug
		// took: every function individually correct, the wiring between them
		// never exercised.
		p.querySelector('.node-main').click();
		await new Promise(r => setTimeout(r, 1100));
		const rec = p.querySelector('.node-detail .record');
		out.recordRendered = !!rec;
		out.recordTitle = rec && rec.querySelector('.record-title') ? rec.querySelector('.record-title').textContent.trim() : null;
		out.recordBody = rec && rec.querySelector('.record-body') ? rec.querySelector('.record-body').textContent.trim().length : 0;
		out.recordLinks = rec ? rec.querySelectorAll('.record-link').length : 0;
		// The old code dumped the whole {v,nodes,missing} envelope into a <pre>.
		out.rawEnvelopeLeak = /"nodes"\s*:/.test(p.textContent) && !rec;

		p.querySelector('.node-chev').click();
		await new Promise(r => setTimeout(r, 1100));
		out.linkedRows = p.querySelectorAll('.node-children .node-row').length;
		return out;
	})()`);
	log('ENGRAPHY_SMOKE_SEARCH', searchResult);

	// The graph index is minutes long against a real server (60 reads/min), so it
	// only runs when asked for, with its own budget. Everything it asserts is
	// about what the CANVAS actually got: cytoscape reports its own element
	// counts, so a layout that silently rendered nothing cannot pass.
	// The FAILURE end state, driven against a server that will refuse the read.
	// A build that cannot start must land on an error with a retry; the failure
	// this guards is the opposite, an indexing spinner that never resolves.
	if (process.env.ENGRAPHY_SMOKE_GRAPH_ERROR) {
		const graphErr = await js(`(async function(){
			const p = document.querySelector('.panel[data-panel="graph"]');
			document.querySelector('.nav-item[data-nav="graph"]').click();
			await new Promise(r => setTimeout(r, 400));
			const build = p.querySelector('.state-block .btn-approve');
			const out = { buildOffered: !!build };
			if (build) { build.click(); }
			// Long enough that a hung spinner would still be spinning here.
			await new Promise(r => setTimeout(r, 6000));
			const block = p.querySelector('.state-block');
			out.stillSpinning = !!p.querySelector('.graph-progress');
			out.errorShown = !!(block && block.classList.contains('tone-error'));
			out.errorTitle = block ? (block.querySelector('.state-title')||{}).textContent : null;
			out.retryOffered = !!(block && Array.from(block.querySelectorAll('button'))
				.some(b => /try again/i.test(b.textContent||'')));
			return out;
		})()`);
		log('ENGRAPHY_SMOKE_GRAPH_ERROR', graphErr);
	}

	if (process.env.ENGRAPHY_SMOKE_GRAPH) {
		const budgetMs = Number(process.env.ENGRAPHY_SMOKE_GRAPH_MS || 420000);
		// Screenshots of the loading state, taken from the main side while the
		// renderer's own probe loop runs: one early (seeding) and one past the
		// first rate-limit pause, which is the stretch that looked like a freeze.
		const loadingShot = process.env.ENGRAPHY_SMOKE_GRAPH_LOADING_SHOT;
		if (process.env.ENGRAPHY_SMOKE_LIGHT_FORCE) {
			await js(
				`document.body.classList.remove('vscode-dark');
				 document.body.classList.add('vscode-light'); true;`
			);
		}
		if (loadingShot) {
			void (async () => {
				const fsx = require('fs');
				const shoot = async (tag: string): Promise<void> => {
					if (w.isDestroyed()) {
						return;
					}
					fsx.writeFileSync(loadingShot.replace('STAGE', tag), (await w.capturePage()).toPNG());
				};
				try {
					await new Promise((r) => setTimeout(r, 4000));
					await shoot('early');
					// The paused state cannot be caught by guessing a delay: the first
					// rate-limit wait starts whenever the 52nd read of the minute lands.
					// Poll for the notice and shoot the frame that actually has it.
					const until = Date.now() + 180000;
					while (Date.now() < until && !w.isDestroyed()) {
						const paused = await w.webContents.executeJavaScript(
							`!!document.querySelector('.graph-progress-wait:not(.hidden)')`
						);
						if (paused) {
							await shoot('ratelimited');
							return;
						}
						await new Promise((r) => setTimeout(r, 700));
					}
				} catch {
					// a shot that cannot be taken must not fail the scenario
				}
			})();
		}

		// One shot of the FINISHED graph, against whatever server this scenario
		// is pointed at. Deliberately separate from loadingShot rather than a
		// third stage of it: this one is meant to be run on demand, against the
		// dev stub, to regenerate docs/graph-{dark,light}.png without touching a
		// real space. Fit is clicked first so the capture matches what a person
		// opening the panel and pressing Fit would see, not whatever frame the
		// layout algorithm happened to settle on first.
		const doneShot = process.env.ENGRAPHY_SMOKE_GRAPH_DONE_SHOT;
		if (doneShot) {
			void (async () => {
				try {
					const until = Date.now() + budgetMs;
					while (Date.now() < until && !w.isDestroyed()) {
						const rendered = await w.webContents.executeJavaScript(
							`!!(document.querySelector('.panel[data-panel="graph"] .graph-canvas canvas') && !document.querySelector('.graph-overlay:not(.hidden)'))`
						);
						if (rendered) {
							break;
						}
						await new Promise((r) => setTimeout(r, 700));
					}
					if (w.isDestroyed()) {
						return;
					}
					await w.webContents.executeJavaScript(
						`document.querySelector('.panel[data-panel="graph"] [data-graph-fit]')?.click();`
					);
					// fcose settles asynchronously; give it a moment before capturing.
					await new Promise((r) => setTimeout(r, 2500));
					if (!w.isDestroyed()) {
						const fsx = require('fs');
						fsx.writeFileSync(doneShot, (await w.capturePage()).toPNG());
					}
				} catch {
					// a shot that cannot be taken must not fail the scenario
				}
			})();
		}

		const graph = await js(`(async function(){
			const p = document.querySelector('.panel[data-panel="graph"]');
			document.querySelector('.nav-item[data-nav="graph"]').click();
			await new Promise(r => setTimeout(r, 400));
			const out = { libs: !!window.cytoscape && !!window.cytoscapeFcose };
			const build = p.querySelector('.state-block .btn-approve') ||
				p.querySelector('[data-state-block="graph-idle"] button');
			out.idleShown = !!build;
			// The dev loop re-runs against an already-cached graph; only a real
			// scenario run pays the multi-minute index again.
			if (build && !${JSON.stringify(!!process.env.ENGRAPHY_SMOKE_GRAPH_NOBUILD)}) { build.click(); }
			// Sample the LOADING state while the index is still running. This is the
			// state that was broken: the card was painted into a hidden subtree, so
			// offsetParent is the assertion that matters. A node can exist, carry
			// the right text, and still be invisible to the user.
			await new Promise(r => setTimeout(r, 2600));
			const card = p.querySelector('.graph-progress');
			out.loading = {
				cardPresent: !!card,
				cardVisible: !!(card && card.offsetParent !== null && card.getBoundingClientRect().height > 0),
				spinnerVisible: !!(card && card.querySelector('.spinner') &&
					card.querySelector('.spinner').offsetParent !== null),
				label: card ? (card.querySelector('.graph-progress-label')||{}).textContent : null,
				sub: card ? (card.querySelector('.graph-progress-sub')||{}).textContent : null,
				barWidth: card ? (card.querySelector('.graph-progress-bar')||{}).style.width : null,
				cancelPresent: !!(card && card.querySelector('.btn'))
			};
			const deadline = Date.now() + ${budgetMs};
			let sawWaitNotice = false;
			let lastBar = 0;
			let barWentBackwards = false;
			while (Date.now() < deadline) {
				await new Promise(r => setTimeout(r, 1500));
				const c = p.querySelector('.graph-progress');
				if (c) {
					const w = parseFloat(((c.querySelector('.graph-progress-bar')||{}).style||{}).width) || 0;
					if (w + 0.001 < lastBar) { barWentBackwards = true; }
					lastBar = Math.max(lastBar, w);
					const note = c.querySelector('.graph-progress-wait:not(.hidden)');
					if (note && note.textContent) { sawWaitNotice = true; }
				}
				if (!p.querySelector('.graph-overlay:not(.hidden)') && p.querySelector('.graph-canvas canvas')) {
					break;
				}
			}
			out.sawRateLimitNotice = sawWaitNotice;
			out.shotAt = window.__engraphyLoadingShotAt || null;
			out.barWentBackwards = barWentBackwards;
			out.barReached = lastBar;
			out.cardGoneWhenDone = !p.querySelector('.graph-progress');
			out.statusText = (p.querySelector('.graph-status-counts')||{}).textContent || null;
			out.scopeRows = p.querySelectorAll('.graph-rail-section')[0].querySelectorAll('.graph-rail-row').length;
			out.typeRows = p.querySelectorAll('.graph-rail-section')[1].querySelectorAll('.graph-rail-row').length;
			out.edgeRows = p.querySelectorAll('.graph-rail-section')[2].querySelectorAll('.graph-rail-row').length;
			return out;
		})()`);
		// The element counts come from cytoscape itself, reached through the view's
		// own instance rather than re-parsing the DOM.
		const cyCounts = await js(`(function(){
			const c = window.__engraphyCy;
			if (!c) { return null; }
			return {
				memories: c.nodes('[kind = "memory"]').length,
				scopeClusters: c.nodes('[kind = "scope"]').length,
				links: c.edges().length,
				labelled: c.nodes('[kind = "memory"]').filter(n => !!n.data('label')).length,
				parented: c.nodes('[kind = "memory"]').filter(n => !!n.data('parent')).length,
				laidOut: c.nodes('[kind = "memory"]').filter(n => {
					const p = n.position();
					return isFinite(p.x) && isFinite(p.y) && (p.x !== 0 || p.y !== 0);
				}).length
			};
		})()`);
		log('ENGRAPHY_SMOKE_GRAPH', { ...(graph as object), cy: cyCounts });

		// Reading the graph, not just drawing it: zoom in far enough that titles
		// resolve, open the busiest memory, and check its record and its links
		// actually render. `auto` labels are the default, so "the label exists in
		// the data" is not the same claim as "the user can read it".
		const graphRead = await js(`(async function(){
			const c = window.__engraphyCy;
			const p = document.querySelector('.panel[data-panel="graph"]');
			if (!c) { return null; }
			const hub = c.nodes('[kind = "memory"]').max(n => n.data('deg')).ele;
			c.zoom({ level: 1.1, renderedPosition: { x: 400, y: 300 } });
			c.center(hub);
			await new Promise(r => setTimeout(r, 250));
			const out = {
				hubDegree: hub.data('deg'),
				hubScope: hub.data('scope'),
				// Cytoscape hides a label whose on-screen size is under
				// min-zoomed-font-size, so comparing effective font size against that
				// threshold is what "the label is readable right now" means.
				labelsVisibleAtZoom: c.nodes('[kind = "memory"]')
					.filter(n => n.renderedStyle('font-size') &&
						parseFloat(n.renderedStyle('font-size')) >=
						parseFloat(n.style('min-zoomed-font-size') || 0)).length,
				scopeChipsOnScreen: p.querySelectorAll('.graph-scope-chip:not(.hidden)').length
			};
			hub.emit('tap');
			await new Promise(r => setTimeout(r, 1400));
			const insp = p.querySelector('.graph-inspector:not(.hidden)');
			out.inspectorOpen = !!insp;
			out.inspectorTitle = insp && insp.querySelector('h2') ? insp.querySelector('h2').textContent : null;
			out.inspectorBodyChars = insp && insp.querySelector('.graph-node-text')
				? insp.querySelector('.graph-node-text').textContent.trim().length : 0;
			out.neighbourRows = insp ? insp.querySelectorAll('.graph-neighbour').length : 0;
			out.relLabels = insp ? Array.from(insp.querySelectorAll('.graph-neighbour-rel'))
				.map(e => e.textContent).filter((v,i,a) => a.indexOf(v)===i) : [];
			// Focus mode should dim everything outside the selection's neighbourhood.
			out.dimmed = c.elements('.dim').length;
			return out;
		})()`);
		log('ENGRAPHY_SMOKE_GRAPH_READ', graphRead);

		// A scope filter must actually remove that scope from the canvas.
		const graphFilter = await js(`(async function(){
			const p = document.querySelector('.panel[data-panel="graph"]');
			const c = window.__engraphyCy;
			const before = c.nodes('[kind = "memory"]').not('.hidden').length;
			const box = p.querySelectorAll('.graph-rail-section')[0].querySelector('.graph-rail-row input');
			box.click();
			await new Promise(r => setTimeout(r, 400));
			const after = c.nodes('[kind = "memory"]').not('.hidden').length;
			const status = (p.querySelector('.graph-status-counts')||{}).textContent || '';
			box.click();
			await new Promise(r => setTimeout(r, 400));
			return { before, after, restored: c.nodes('[kind = "memory"]').not('.hidden').length, status };
		})()`);
		log('ENGRAPHY_SMOKE_GRAPH_FILTER', graphFilter);

		// The controls that must survive HAVING a graph.
		//
		// This exists because the deep sweep did not: its checkbox lived inside the
		// first-run block, which stops rendering the moment a snapshot exists, so
		// after the very first index the sweep was documented-but-unreachable and
		// Rebuild could only ever ask for a non-sweep build. Asserting the switch
		// is present WHILE a graph is on screen is the check that catches it.
		//
		// The sweep is deliberately not RUN here: it spends `search` calls, which
		// move the same usage counters the Impact & usage panel reports.
		const graphControls = await js(`(async function(){
			const p = document.querySelector('.panel[data-panel="graph"]');
			const sweep = p.querySelector('.graph-sweep-toggle input');
			const out = {
				sweepReachableWithGraph: !!sweep && !sweep.disabled,
				sweepDefaultsOff: !!sweep && sweep.checked === false,
				clearActionPresent: !!p.querySelector('.graph-status-action')
			};
			if (sweep) {
				sweep.click();
				out.sweepTogglesOn = sweep.checked === true;
				sweep.click();
				out.sweepTogglesOff = sweep.checked === false;
			}
			// Clearing must round-trip all the way to disk and back to the build
			// screen — the message existed on both sides but nothing ever sent it.
			const clear = p.querySelector('.graph-status-action');
			if (clear) { clear.click(); }
			await new Promise(r => setTimeout(r, 900));
			out.idleAfterClear = !!p.querySelector('[data-state-block="graph-idle"]');
			out.sweepStillReachable = !!p.querySelector('.graph-sweep-toggle input');
			return out;
		})()`);
		log('ENGRAPHY_SMOKE_GRAPH_CONTROLS', graphControls);
	}

	const approve = await js(`(async function(){
		const p = document.querySelector('.panel[data-panel="confirm"]');
		document.querySelector('.nav-item[data-nav="confirm"]').click();
		await new Promise(r => setTimeout(r, 300));
		const before = p.querySelectorAll('.card').length;
		const btn = p.querySelector('.btn-approve');
		if (btn) btn.click();
		await new Promise(r => setTimeout(r, 1400));
		return { before, after: p.querySelectorAll('.card').length };
	})()`);
	log('ENGRAPHY_SMOKE_APPROVE', approve);

	// Merge and Promote are the other two review actions, and both were edited
	// this round. Approve alone does not exercise them: merge carries a second id
	// (mergeInto) through the router, and Promote opens a modal whose submit
	// builds an authored node. A wrong field name in either reads to the user as
	// "the button does nothing", which is exactly what a DOM snapshot catches and
	// a unit test does not.
	const merge = await js(`(async function(){
		const p = document.querySelector('.panel[data-panel="confirm"]');
		const before = p.querySelectorAll('.card').length;
		const btn = p.querySelector('[data-action="merge"]');
		if (!btn) return { skipped: 'no merge button' };
		btn.click();
		await new Promise(r => setTimeout(r, 1600));
		return { before, after: p.querySelectorAll('.card').length };
	})()`);
	log('ENGRAPHY_SMOKE_MERGE', merge);

	const promote = await js(`(async function(){
		const p = document.querySelector('.panel[data-panel="confirm"]');
		const before = p.querySelectorAll('.card').length;
		const btn = p.querySelector('[data-action="promote"]');
		if (!btn) return { skipped: 'no promote button' };
		btn.click();
		await new Promise(r => setTimeout(r, 900));
		const modal = document.querySelector('#modal-host .modal');
		if (!modal) return { before, modalOpened: false };
		// Target fields by data-field. querySelector('input') returns the HIDDEN
		// "Other node type" box, not the title, so reading a prefilled title off
		// it silently asserted nothing.
		const sel = modal.querySelector('[data-field="type"]');
		const title = modal.querySelector('[data-field="title"]');
		const opened = {
			modalOpened: true,
			nodeTypes: sel ? sel.options.length : 0,
			prefilledTitle: title ? title.value : null
		};
		if (title && !title.value) { title.value = 'Smoke authored title'; }
		const body = modal.querySelector('[data-field="body"]');
		if (body && !body.value) { body.value = 'Smoke authored body'; }
		modal.querySelector('[data-action="promote-submit"]').click();
		await new Promise(r => setTimeout(r, 1800));
		return Object.assign(opened, {
			before,
			after: p.querySelectorAll('.card').length,
			modalClosed: !document.querySelector('#modal-host .modal')
		});
	})()`);
	log('ENGRAPHY_SMOKE_PROMOTE', promote);

	// The first promote consumed the inbox item that CARRIES a scope, so the one
	// left has scope:null and takes the other branch: a scope <select> plus an
	// "Other..." free-text fallback and a different scopeGetter. That is the more
	// complex half of the modal, so drive it too.
	const promoteNoScope = await js(`(async function(){
		const p = document.querySelector('.panel[data-panel="confirm"]');
		const before = p.querySelectorAll('.card').length;
		const btn = p.querySelector('[data-action="promote"]');
		if (!btn) return { skipped: 'no inbox item left' };
		btn.click();
		await new Promise(r => setTimeout(r, 900));
		const modal = document.querySelector('#modal-host .modal');
		if (!modal) return { before, modalOpened: false };
		const out = {
			before,
			modalOpened: true,
			// The distinguishing feature of this branch: a scope chooser exists.
			hasScopeChooser: !!modal.querySelector('[data-field="scope"]'),
			scopeOptions: modal.querySelector('[data-field="scope"]')
				? modal.querySelector('[data-field="scope"]').options.length
				: 0
		};
		const title = modal.querySelector('[data-field="title"]');
		if (title && !title.value) { title.value = 'Authored from a scopeless item'; }
		const body = modal.querySelector('[data-field="body"]');
		if (body && !body.value) { body.value = 'Body for the scopeless item'; }
		modal.querySelector('[data-action="promote-submit"]').click();
		await new Promise(r => setTimeout(r, 1800));
		out.after = p.querySelectorAll('.card').length;
		out.modalClosed = !document.querySelector('#modal-host .modal');
		return out;
	})()`);
	log('ENGRAPHY_SMOKE_PROMOTE2', promoteNoScope);

	// Menu.buildFromTemplate throws on a malformed template, so simply reporting
	// the built menu proves it constructed. The macOS-only branches cannot be
	// exercised here; see README "left for Devon".
	const menu = Menu.getApplicationMenu();
	log('ENGRAPHY_SMOKE_MENU', {
		built: !!menu,
		top: menu ? menu.items.map((i) => i.label).filter(Boolean) : [],
		accelerators: menu
			? menu.items
					.flatMap((i) => (i.submenu ? i.submenu.items : []))
					.map((i) => i.accelerator)
					.filter(Boolean)
			: [],
	});

	if (process.env.ENGRAPHY_SMOKE_SETTINGS) {
		const saved = (await handleInvoke('settings', {
			type: 'save',
			serverUrl: process.env.ENGRAPHY_SMOKE_SAVE_URL || process.env.ENGRAPHY_SMOKE_URL || 'http://127.0.0.1:8000/mcp/',
			space: 'team',
			token: 'smoke-secret-token',
		})) as any;
		const loaded = (await handleInvoke('settings', { type: 'load' })) as any;
		log('ENGRAPHY_SMOKE_SETTINGS', {
			savedOk: saved?.saved,
			hasToken: loaded?.settings?.hasToken,
			tokenInsecure: loaded?.settings?.tokenInsecure,
			space: loaded?.settings?.space,
			authOk: saved?.authOk,
			probeError: saved?.probeError?.class ?? null,
		});
	}

	if (process.env.ENGRAPHY_SMOKE_SHOT) {
		w.show();
		w.focus();
		await new Promise((r) => setTimeout(r, 700));
		const themes = process.env.ENGRAPHY_SMOKE_LIGHT
			? [
					{ tag: 'light', cls: 'vscode-light' },
					{ tag: 'dark', cls: 'vscode-dark' },
				]
			: [{ tag: '', cls: '' }];
		const panels = (process.env.ENGRAPHY_SMOKE_PANELS || 'confirm,stats,explorer,settings').split(',');
		for (const th of themes) {
			if (th.cls) {
				await js(
					`document.body.classList.remove('vscode-dark','vscode-light');
					 document.body.classList.add('${th.cls}'); true;`
				);
			}
			for (const panel of panels) {
				if (panel === 'graph') {
					// The graph keeps whatever pan/zoom the session left it at, so a
					// screenshot has to frame it explicitly or it captures wherever the
					// last interaction happened to leave the viewport.
					await js(`(function(){
						document.querySelector('.nav-item[data-nav="graph"]').click();
						const b = document.querySelector('[data-graph-fit]');
						if (b) { b.click(); }
						return true;
					})()`);
					await new Promise((r) => setTimeout(r, 900));
					const img = await w.capturePage();
					const fs2 = require('fs');
					fs2.writeFileSync(
						process.env.ENGRAPHY_SMOKE_SHOT!.replace(
							'PANEL',
							(th.tag ? th.tag + '-' : '') + panel
						),
						img.toPNG()
					);
					continue;
				}
				if (panel === 'onboarding') {
					await js(`window.ENGRAPHY.showOnboarding(); true;`);
					await new Promise((r) => setTimeout(r, 500));
				} else {
					await js(`document.querySelector('.nav-item[data-nav="${panel}"]').click(); true;`);
					await new Promise((r) => setTimeout(r, 500));
				}
				const img = await w.capturePage();
				const fs = require('fs');
				const name = process.env.ENGRAPHY_SMOKE_SHOT!.replace(
					'PANEL',
					(th.tag ? th.tag + '-' : '') + panel
				);
				fs.writeFileSync(name, img.toPNG());
			}
		}
	}
}
