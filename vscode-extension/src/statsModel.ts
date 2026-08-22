// Pure, dependency-free logic for the Stats panel: the wire types for the
// server's `stats` tool, a defensive parser, the view-model the webview renders
// (tiles + answer rate + normalized sparkline series + scope labels), and a
// strict webview→host message parser. No `vscode` / SDK / DOM imports so this is
// unit-testable with plain Node (scripts/test-client.js).

import { computeConnectionState, type ConnectionVM, type ThemeKind } from './webviewMessages';

// ---- wire types (FROZEN server contract) -----------------------------------

export interface StatsMetrics {
	questions_asked: number;
	answered: number;
	memory_reused: number;
	facts_stored: number;
	duplicates_prevented: number;
	promotes: number;
}
export interface StatsDay extends StatsMetrics {
	date: string;
}
export interface StatsResult {
	v: number;
	space: string;
	group_by: 'space' | 'user';
	principal: string | null;
	range_days: number;
	generated_at: string;
	totals: StatsMetrics;
	series: StatsDay[];
}

export const METRIC_KEYS: Array<keyof StatsMetrics> = [
	'questions_asked',
	'answered',
	'memory_reused',
	'facts_stored',
	'duplicates_prevented',
	'promotes',
];

function num(v: unknown): number {
	const n = typeof v === 'number' ? v : Number(v);
	return Number.isFinite(n) ? n : 0;
}
function metricsFrom(o: unknown): StatsMetrics {
	const r = (o ?? {}) as Record<string, unknown>;
	return {
		questions_asked: num(r.questions_asked),
		answered: num(r.answered),
		memory_reused: num(r.memory_reused),
		facts_stored: num(r.facts_stored),
		duplicates_prevented: num(r.duplicates_prevented),
		promotes: num(r.promotes),
	};
}

/** Defensive parse of a stats tool result — never throws, never NaNs. */
export function parseStatsResult(raw: unknown): StatsResult {
	const r = (raw ?? {}) as Record<string, unknown>;
	const group_by = r.group_by === 'user' ? 'user' : 'space';
	return {
		v: num(r.v) || 1,
		space: typeof r.space === 'string' ? r.space : '',
		group_by,
		principal: typeof r.principal === 'string' ? r.principal : null,
		range_days: num(r.range_days),
		generated_at: typeof r.generated_at === 'string' ? r.generated_at : '',
		totals: metricsFrom(r.totals),
		series: Array.isArray(r.series)
			? r.series.map((d) => {
					const row = (d ?? {}) as Record<string, unknown>;
					return { date: typeof row.date === 'string' ? row.date : '', ...metricsFrom(row) };
				})
			: [],
	};
}

// ---- pure calculations ------------------------------------------------------

/** answered ÷ asked as a whole-percent 0..100; 0 when nothing was asked. */
export function answerRate(answered: number, asked: number): number {
	if (!(asked > 0)) {
		return 0;
	}
	return Math.round((answered / asked) * 100);
}

/**
 * Normalize a daily series to 0..1 against its own max, for sparkline heights.
 * An all-zero (or empty) series returns all zeros — the webview draws that as a
 * flat baseline rather than dividing by zero.
 */
export function normalizeSeries(values: number[]): number[] {
	const max = values.reduce((m, v) => (v > m ? v : m), 0);
	if (max <= 0) {
		return values.map(() => 0);
	}
	return values.map((v) => {
		const f = v / max;
		return f < 0 ? 0 : f > 1 ? 1 : f;
	});
}

/** Human scope label + help, branched off group_by / principal (per contract). */
export function scopeLabels(
	groupBy: 'space' | 'user',
	principal: string | null,
	space: string
): { label: string; help: string } {
	if (groupBy === 'user') {
		return {
			label: 'You',
			help: `Just your activity${principal ? ` (${principal})` : ''}.`,
		};
	}
	return {
		label: 'Whole space',
		help: `Everyone in the ${space ? `“${space}”` : ''} space.`.replace('  ', ' '),
	};
}

// ---- view-model -------------------------------------------------------------

export interface StatTileVM {
	key: keyof StatsMetrics;
	label: string;
	value: number;
	hero: boolean;
	/** Secondary line under the value (e.g. "72% answer rate"). */
	sub?: string;
	/** Honest-labeling footnote marker (proxy definitions). */
	note?: string;
	/** Normalized 0..1 daily series for the sparkline. */
	spark: number[];
}

export interface StatsViewVM {
	space: string;
	groupBy: 'space' | 'user';
	principal: string | null;
	scopeLabel: string;
	scopeHelp: string;
	rangeDays: number;
	generatedAt: string;
	answerRate: number;
	tiles: StatTileVM[];
}

const MEMORY_REUSED_NOTE =
	'Proxy: facts returned in searches, not distinct-node reuse.';

/** Build the render-ready view-model from a parsed stats result. */
export function buildStatsView(r: StatsResult): StatsViewVM {
	const seriesFor = (k: keyof StatsMetrics): number[] =>
		normalizeSeries(r.series.map((d) => d[k]));
	const rate = answerRate(r.totals.answered, r.totals.questions_asked);
	const { label, help } = scopeLabels(r.group_by, r.principal, r.space);

	const tile = (
		key: keyof StatsMetrics,
		label_: string,
		hero: boolean,
		extra?: Partial<StatTileVM>
	): StatTileVM => ({
		key,
		label: label_,
		value: r.totals[key],
		hero,
		spark: seriesFor(key),
		...extra,
	});

	const tiles: StatTileVM[] = [
		// Hero — the value story.
		tile('duplicates_prevented', 'Duplicates prevented', true),
		tile('memory_reused', 'Memory reused', true, { note: MEMORY_REUSED_NOTE }),
		// Supporting.
		tile('questions_asked', 'Questions asked', false),
		tile('answered', 'Answered', false, { sub: `${rate}% answer rate` }),
		tile('facts_stored', 'Facts stored', false),
		tile('promotes', 'Promotes', false),
	];

	return {
		space: r.space,
		groupBy: r.group_by,
		principal: r.principal,
		scopeLabel: label,
		scopeHelp: help,
		rangeDays: r.range_days,
		generatedAt: r.generated_at,
		answerRate: rate,
		tiles,
	};
}

// ---- webview state + message protocol --------------------------------------

export interface StatsStateVM {
	connection: ConnectionVM;
	loading: boolean;
	error: string | null;
	groupBy: 'space' | 'user';
	rangeDays: number;
	view: StatsViewVM | null;
	/** True while the FIRST load is in flight; drives skeletons over a spinner. */
	firstLoad?: boolean;
}

export const STATS_RANGES = [7, 14, 30];

export type StatsHostToWebview =
	| { type: 'state'; state: StatsStateVM }
	| { type: 'theme'; kind: ThemeKind };

export type StatsWebviewToHost =
	| { type: 'ready' }
	| { type: 'refresh' }
	| { type: 'setRange'; rangeDays: number }
	| { type: 'setGroup'; groupBy: 'space' | 'user' }
	| { type: 'openWalkthrough' }
	| { type: 'configureServer' }
	| { type: 'reconnect' };

/**
 * SUPERSEDED by computeConnection in ./connection (see the note on
 * isConnectionError in webviewMessages.ts for why this is kept rather than
 * deleted). Nothing in the extension calls it any more.
 *
 * Reuse the confirm-queue's connection classifier for the single-error Stats
 * case: a connection/auth failure (or unconfigured URL) swaps the panel for the
 * branded no-server block. The single error is fed to both slots so one dead
 * server reads as no-server (a plain tool error stays inline).
 */
export function statsConnectionState(serverConfigured: boolean, error: string | null): ConnectionVM {
	return computeConnectionState({
		serverConfigured,
		pendingError: error,
		inboxError: error,
	});
}

/** Strict parse of an untrusted webview→host message, or null. */
export function parseStatsMessage(raw: unknown): StatsWebviewToHost | null {
	if (!raw || typeof raw !== 'object') {
		return null;
	}
	const m = raw as Record<string, unknown>;
	switch (m.type) {
		case 'ready':
			return { type: 'ready' };
		case 'refresh':
			return { type: 'refresh' };
		case 'openWalkthrough':
			return { type: 'openWalkthrough' };
		case 'configureServer':
			return { type: 'configureServer' };
		case 'reconnect':
			return { type: 'reconnect' };
		case 'setRange': {
			const n = typeof m.rangeDays === 'number' ? m.rangeDays : Number(m.rangeDays);
			return Number.isInteger(n) && n > 0 && n <= 3650 ? { type: 'setRange', rangeDays: n } : null;
		}
		case 'setGroup':
			return m.groupBy === 'space' || m.groupBy === 'user'
				? { type: 'setGroup', groupBy: m.groupBy }
				: null;
		default:
			return null;
	}
}
