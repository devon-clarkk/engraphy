// Assemble a whole-graph snapshot out of Engraphy's small, capped reads.
//
// ============================================================================
// WHY THIS IS HARDER THAN "GET THE GRAPH"
// ============================================================================
// Engraphy's MCP surface has no whole-graph read, and every read that exists is
// capped on purpose:
//
//   search    ≤ 25 results per call
//   traverse  ≤ 50 WALK ROWS per call, max_depth ≤ 4
//   briefing  non-semantic sections cap at 10; the semantic section returns []
//             unless you pass a `hint`
//
// On top of that the server enforces a per-token sliding window of 60 reads per
// minute (config `rate.read_per_min`), so call COUNT — not latency — is the cost
// that matters. Each call takes ~10ms against a local server; the wall clock is
// almost entirely the rate-limit pacing.
//
// ============================================================================
// TWO CONSTRAINTS THAT PICKED THE ALGORITHM
// ============================================================================
//
// 1. THE HARVEST MUST NOT MOVE THE USAGE COUNTERS. `search` is what the `stats`
//    tool counts as `questions_asked` / `answered`, and every result it returns
//    adds to `memory_reused`. A 16-scope search sweep would add ~16 questions
//    and ~390 reuses — which, against this space's real 30-day totals (48 and
//    268), would MORE THAN DOUBLE the numbers on the Impact & usage panel.
//    Drawing a picture must not corrupt the measurement. `briefing` and
//    `traverse` are explicitly NOT counted (engraphy/core/metrics.py says so in
//    as many words), so the default harvest uses only those two, and the
//    `search` sweep is opt-in, off by default, and labelled in the UI.
//
// 2. DEPTH 2, NOT 3. `traverse` orders walk rows by (depth, src, dst, type)
//    BEFORE applying its 50-row LIMIT. So on truncation the shallow rows are the
//    ones that survived, and a start node's own edges — all of which are depth-1
//    rows — are still complete unless the node alone has ≥50 of them. That makes
//    "the start is edge-complete" safe even on a truncated walk, and lets a
//    NON-truncated depth-2 walk close the start AND all its neighbours at once.
//    Depth 3 was measured and is strictly worse: the frontier at this graph's
//    average degree (~3.7) blows past 50 rows on 95% of calls, which collapses
//    the rule back to "one node closed per call" while wasting the rows.
//
// The residual honesty: nothing a walk cannot reach from a seed will ever appear.
// That is not only isolated nodes — it is any small ISLAND of nodes linked to
// each other and to nothing else, which is what the live space's remaining gap
// turned out to be. Seeding each scope with several hints (SEED_HINTS) shrinks
// that gap to a handful; closing it entirely is what the opt-in `search` sweep
// is for. Measured on the live space: 234 of 239 memories and 434 of 437 links
// by default, all 16 scopes.

import {
	edgeKey,
	nodesFromBriefing,
	nodesFromSearch,
	parseScopeList,
	parseTraverse,
	type GraphEdge,
	type GraphNode,
	type GraphProgress,
	type GraphScope,
	type GraphSnapshot,
} from './graphModel';

/** The four reads a harvest needs, injected so this module stays testable. */
export interface GraphTools {
	scopeList(): Promise<unknown>;
	briefing(scope: string, hint?: string): Promise<unknown>;
	traverse(opts: {
		startId: string;
		direction: 'out' | 'in' | 'both';
		maxDepth?: number;
		limit?: number;
		detail?: 'summary' | 'full';
		edgeTypes?: string[];
	}): Promise<unknown>;
	search(opts: { scope: string; query: string; limit?: number; detail?: 'full' | 'summary' }): Promise<unknown>;
}

export interface HarvestOptions {
	space: string;
	/** Run the opt-in `search` sweep. Moves the usage counters — see above. */
	deepSweep?: boolean;
	/**
	 * Client-side pacing target. Deliberately BELOW the server's 60/min default:
	 * the app's own health probe spends 3 reads a minute on the same token, and a
	 * harvest that claimed all 60 starved it into RATE_LIMITED — which painted a
	 * red "Server error" badge over a panel that was reading the server fine.
	 */
	readsPerMin?: number;
	onProgress?: (p: GraphProgress) => void;
	/** Polled between calls; a true return aborts and returns what is known. */
	isCancelled?: () => boolean;
	sleep?: (ms: number) => Promise<void>;
	now?: () => number;
}

const TRAVERSE_LIMIT = 50;
const WALK_DEPTH = 2;
/**
 * If a truncated depth-2 walk returned nearly a full page of depth-1 rows, the
 * start's OWN edge list may itself have been cut, so the "the start is complete"
 * rule stops holding and the node is re-read one hop out (see `readHubEdges`).
 * Only a node with ~50+ edges can trigger this, so the extra calls are rare
 * rather than routine.
 */
const DEPTH1_SUSPECT = TRAVERSE_LIMIT - 5;

/**
 * The hints each scope's briefing is asked with, in order.
 *
 * WHY MORE THAN ONE. A briefing's `relevant` section is semantic and returns
 * only what its hint is close to (top_k 5), and the pack's other section is
 * `recent_notes`, filtered to type `note` within 14 days. One hint per scope
 * therefore misses whole POCKETS of a scope — and the miss is not random: the
 * nodes it leaves behind tend to be small groups linked only to each other, so
 * the walk cannot reach them either and they never appear at all.
 *
 * Measured against the live space: one hint reached 229 of 239 memories and 15
 * of 16 scopes; these three reach 234 and all 16, for 33 extra reads. A fourth
 * hint bought one more memory for another 16 reads, which is where this stops.
 *
 * `@name` is the scope's own display name. The rest are deliberately broad and
 * aimed at the node types the pack's semantic section covers (preference, note,
 * concept, strategy). This is a HEURISTIC over a semantic search, not a
 * guarantee: a space with a different briefing pack will want different hints,
 * and total coverage is what the opt-in `search` sweep is for.
 */
const SEED_HINTS = [
	'@name',
	'preferences, decisions and how I like to work',
	'study units, courses and strategies',
];

/**
 * Where each phase sits on the OVERALL 0..1 bar.
 *
 * Walking owns most of the span because it is most of the work: seeding is a
 * fixed 3 reads per scope (48 on a 16-scope space) while the walk is one read
 * per memory, which on this space is around 170. The numbers are a rough
 * apportionment of typical cost, not a promise.
 */
const PHASE_SPAN: Record<string, [number, number]> = {
	scopes: [0, 0.02],
	seeding: [0.02, 0.2],
	walking: [0.2, 0.94],
	sweeping: [0.94, 0.99],
	done: [1, 1],
};

function clamp01(n: number): number {
	return n < 0 ? 0 : n > 1 ? 1 : n;
}

const defaultSleep = (ms: number): Promise<void> =>
	new Promise((r) => setTimeout(r, Math.max(0, ms)));

/** Pull `retry after <n>ms` out of an ENGRAPHY_RATE_LIMITED tool error. */
export function retryAfterMs(err: unknown): number | null {
	const text = err instanceof Error ? err.message : String(err ?? '');
	if (!/RATE_LIMITED/.test(text)) {
		return null;
	}
	const m = /retry after (\d+)\s*ms/i.exec(text);
	return m ? Number(m[1]) : 1000;
}

/**
 * Client-side sliding window over the server's per-token read budget.
 *
 * Pacing PROACTIVELY matters because a rejected call is a wasted round trip:
 * the server raises before it records the event, so a refusal costs latency and
 * buys nothing. The reactive `retryAfterMs` path stays as the backstop for when
 * something else is spending the same token's budget.
 */
class ReadPacer {
	private readonly stamps: number[] = [];
	waitedMs = 0;

	constructor(
		private readonly limit: number,
		private readonly now: () => number,
		private readonly sleep: (ms: number) => Promise<void>,
		/**
		 * Called with the length of a wait before it starts, and 0 when it ends.
		 *
		 * A minute-long sleep is the single longest stretch in which the harvest
		 * produces no observable change, so it is also the stretch most likely to
		 * be mistaken for a crash. The pacer announces it rather than going quiet.
		 */
		private readonly onWait: (ms: number) => void = () => {}
	) {}

	private async pause(ms: number): Promise<void> {
		this.waitedMs += ms;
		this.onWait(ms);
		try {
			await this.sleep(ms);
		} finally {
			this.onWait(0);
		}
	}

	async take(): Promise<void> {
		for (;;) {
			const t = this.now();
			while (this.stamps.length && this.stamps[0] <= t - 60_000) {
				this.stamps.shift();
			}
			if (this.stamps.length < this.limit) {
				this.stamps.push(t);
				return;
			}
			await this.pause(this.stamps[0] + 60_000 - t + 50);
		}
	}

	/** Charge a wait the SERVER asked for (the reactive path). */
	async serverWait(ms: number): Promise<void> {
		await this.pause(ms);
	}
}

class Cancelled extends Error {}

export async function harvestGraph(
	tools: GraphTools,
	opts: HarvestOptions
): Promise<GraphSnapshot> {
	const now = opts.now ?? (() => Date.now());
	const sleep = opts.sleep ?? defaultSleep;
	const isCancelled = opts.isCancelled ?? (() => false);
	const startedAt = now();
	/** Last phase/label emitted, so a wait can be reported without losing them. */
	let lastPhase = 'scopes';
	let lastLabel = 'Starting…';
	let lastLocal: number | null = 0;
	let waitingMs = 0;
	const pacer = new ReadPacer(opts.readsPerMin ?? 52, now, sleep, (ms) => {
		waitingMs = ms;
		report(lastPhase, lastLabel, lastLocal);
	});

	const nodes = new Map<string, GraphNode>();
	const edges = new Map<string, GraphEdge>();
	/** Nodes whose FULL edge list is known — the harvest's termination set. */
	const closed = new Set<string>();
	let briefingCalls = 0;
	let traverseCalls = 0;
	let searchCalls = 0;
	let truncatedWalks = 0;
	/** Hubs whose edge list could not be read in full even after partitioning. */
	const unreadableHubs = new Set<string>();
	/** Relationship names seen so far, used to partition an oversized hub read. */
	const edgeTypesSeen = new Set<string>();

	const addNode = (n: GraphNode): void => {
		const prev = nodes.get(n.id);
		// A `summary` envelope and a `full` one carry the same fields we keep, so
		// first write wins; this only guards against a later, emptier record.
		if (!prev) {
			nodes.set(n.id, n);
		}
	};
	const addEdge = (e: GraphEdge): void => {
		edges.set(edgeKey(e), e);
		edgeTypesSeen.add(e.type);
	};

	/**
	 * `local` is progress WITHIN the current phase (0..1, or null when unknown);
	 * it is mapped onto the phase's slice of the overall bar before it leaves
	 * here, so the renderer never sees a per-phase number it could misread.
	 */
	function report(phase: string, label: string, local: number | null): void {
		lastPhase = phase;
		lastLabel = label;
		lastLocal = local;
		const span = PHASE_SPAN[phase];
		const fraction =
			span && local !== null ? span[0] + (span[1] - span[0]) * clamp01(local) : span ? span[0] : null;
		opts.onProgress?.({
			phase,
			label,
			nodes: nodes.size,
			edges: edges.size,
			calls: briefingCalls + traverseCalls + searchCalls,
			fraction,
			waitingMs,
			elapsedMs: now() - startedAt,
		});
	}

	/** One paced read, with the server's own backoff honoured on refusal. */
	const read = async <T>(fn: () => Promise<T>): Promise<T> => {
		for (;;) {
			if (isCancelled()) {
				throw new Cancelled();
			}
			await pacer.take();
			try {
				return await fn();
			} catch (e) {
				const wait = retryAfterMs(e);
				if (wait === null) {
					throw e;
				}
				await pacer.serverWait(wait + 250);
			}
		}
	};

	// ---- 1. scopes ----------------------------------------------------------
	report('scopes', 'Reading scopes…', 0);
	const scopes: GraphScope[] = parseScopeList(await read(() => tools.scopeList()));

	// ---- 2. seed from briefings (metrics-free) ------------------------------
	//
	// The `hint` is what makes this worth doing. Without one, briefing runs only
	// its non-semantic sections — here a `recent_notes` section filtered to type
	// `note` within 14 days — so any scope whose memories are older or a different
	// type returns nothing at all. Passing the scope's display name as the hint
	// fires the `relevant` semantic section too, which has no recency filter.
	// Measured on this space: 43 seeds without hints (13 of 16 scopes reached),
	// 80 seeds with them (15 of 16).
	const seedSteps = scopes.length * SEED_HINTS.length;
	let seedStep = 0;
	for (let i = 0; i < scopes.length; i++) {
		const s = scopes[i];
		for (const hint of SEED_HINTS) {
			seedStep++;
			report(
				'seeding',
				`Seeding from scope ${i + 1} of ${scopes.length}: ${s.id}`,
				seedStep / seedSteps
			);
			try {
				const res = await read(() =>
					tools.briefing(s.id, hint === '@name' ? s.displayName || s.id : hint)
				);
				briefingCalls++;
				for (const n of nodesFromBriefing(res)) {
					addNode(n);
				}
			} catch (e) {
				if (e instanceof Cancelled) {
					throw e;
				}
				// A scope that refuses a briefing (pack quirk, permissions) must not
				// abort the whole build — the walk can still reach its nodes by edge.
			}
		}
	}

	// ---- 3. close every known node's edge list ------------------------------
	const closure = async (): Promise<void> => {
		for (;;) {
			let target: string | undefined;
			for (const id of nodes.keys()) {
				if (!closed.has(id)) {
					target = id;
					break;
				}
			}
			if (target === undefined) {
				return;
			}
			const done = closed.size;
			const known = nodes.size;
			report(
				'walking',
				`Walking links: ${done} of ${known} memories mapped`,
				known ? done / known : null
			);

			const walk = parseTraverse(
				await read(() =>
					tools.traverse({
						startId: target!,
						direction: 'both',
						maxDepth: WALK_DEPTH,
						limit: TRAVERSE_LIMIT,
						detail: 'summary',
					})
				)
			);
			traverseCalls++;
			for (const n of walk.nodes) {
				addNode(n);
			}
			for (const e of walk.edges) {
				addEdge(e);
			}

			if (!walk.truncated) {
				// Nothing was cut, so the walk expanded every depth-1 node fully:
				// each of them has had all of its own edges emitted.
				for (const n of walk.nodes) {
					if (n.depth <= 1) {
						closed.add(n.id);
					}
				}
				closed.add(target);
				continue;
			}

			truncatedWalks++;
			const depth1 = walk.nodes.filter((n) => n.depth === 1).length;
			if (depth1 >= DEPTH1_SUSPECT) {
				await readHubEdges(target);
			}
			// The start's depth-1 rows sort ahead of everything else, so they
			// survived the cut: the start is closed either way. Its neighbours are
			// NOT, because their own expansion is what got dropped.
			closed.add(target);
		}
	};

	/**
	 * Read the whole edge list of a node too busy to fit one 50-row page.
	 *
	 * Partitioning, in widening steps, because ONE page is all the tool will ever
	 * give: first by direction (out / in), then — for a half that is still too big
	 * — by relationship name. A node with 70 edges that all point the same way
	 * defeats the direction split on its own (both halves are 70 and 0), which is
	 * exactly the case that made this more than a two-line fallback: the split has
	 * to be along an axis the data actually varies on.
	 *
	 * If a single (direction, relationship) slice STILL overflows — one node with
	 * 50+ edges of one type in one direction — the frozen tool surface has nothing
	 * finer to slice by, so the node is recorded in `unreadableHubs` and reported
	 * rather than silently under-drawn.
	 */
	async function readHubEdges(target: string): Promise<void> {
		const walkOnce = async (
			direction: 'out' | 'in',
			edgeTypes?: string[]
		): Promise<boolean> => {
			const res = parseTraverse(
				await read(() =>
					tools.traverse({
						startId: target,
						direction,
						maxDepth: 1,
						limit: TRAVERSE_LIMIT,
						detail: 'summary',
						edgeTypes,
					})
				)
			);
			traverseCalls++;
			for (const n of res.nodes) {
				addNode(n);
			}
			for (const e of res.edges) {
				addEdge(e);
			}
			return res.truncated;
		};

		for (const direction of ['out', 'in'] as const) {
			if (!(await walkOnce(direction))) {
				continue;
			}
			// Snapshot the names first: walkOnce adds to the live set as it reads.
			const types = [...edgeTypesSeen];
			if (!types.length) {
				unreadableHubs.add(target);
				continue;
			}
			for (const t of types) {
				if (await walkOnce(direction, [t])) {
					unreadableHubs.add(target);
				}
			}
		}
	}
	await closure();

	// ---- 4. optional search sweep (opt-in; moves the usage counters) --------
	if (opts.deepSweep) {
		for (let i = 0; i < scopes.length; i++) {
			const s = scopes[i];
			report('sweeping', `Deep sweep ${i + 1} of ${scopes.length}: ${s.id}`, (i + 1) / scopes.length);
			try {
				const res = await read(() =>
					tools.search({ scope: s.id, query: s.displayName || s.id, limit: 25, detail: 'summary' })
				);
				searchCalls++;
				for (const n of nodesFromSearch(res)) {
					addNode(n);
				}
			} catch (e) {
				if (e instanceof Cancelled) {
					throw e;
				}
			}
		}
		// Anything the sweep newly found still needs its edges walked.
		await closure();
	}

	report('done', 'Done', 1);
	return {
		v: 1,
		space: opts.space,
		builtAt: new Date(now()).toISOString(),
		scopes,
		nodes: [...nodes.values()],
		edges: [...edges.values()],
		stats: {
			briefingCalls,
			traverseCalls,
			searchCalls,
			rateLimitWaitsMs: Math.round(pacer.waitedMs),
			truncatedWalks,
			unreadableHubs: unreadableHubs.size,
			durationMs: now() - startedAt,
			deepSweep: opts.deepSweep === true,
		},
	};
}

export { Cancelled };
