// The renderer -> main message trust boundary.
//
// WHY THIS EXISTS: the copied client ships `parseWebviewMessage` and
// `parseStatsMessage`, whose own doc comments call them the trust boundary and
// say "the host must never act on an unparsed message". Neither was actually
// called. `handleMessage` in main.ts read `msg.pendingId` / `msg.mergeInto` /
// `msg.inboxId` straight off the raw payload, so those parsers were tested but
// dead, which made the suite's coverage look larger than it was.
//
// They cannot simply be dropped in, because the desktop app added two commands
// the extension never had (`promoteSubmit`, which carries an authored node, and
// `reconnect`). So this module DELEGATES to the frozen parsers for every command
// they already cover, and validates only the desktop-only additions itself. That
// keeps one implementation of the shared rules, keeps their tests meaningful,
// and keeps `client/` frozen (DECISIONS.md §3).
//
// Pure: no electron, no DOM. Covered by scripts/test-client.js.

import { parseWebviewMessage } from './client/webviewMessages';
import { parseStatsMessage } from './client/statsModel';

/** Commands the confirm panel may issue. */
export type ConfirmCommand =
	| { type: 'ready' }
	| { type: 'refresh' }
	| { type: 'reconnect' }
	| { type: 'approve'; pendingId: string }
	| { type: 'merge'; pendingId: string; mergeInto: string }
	| { type: 'discard'; inboxId: string }
	| { type: 'promoteSubmit'; inboxId: string; nodeType: string; scope: string; title: string; body: string }
	| { type: 'openWalkthrough' }
	| { type: 'configureServer' };

/** Commands the stats panel may issue. */
export type StatsCommand =
	| { type: 'ready' }
	| { type: 'refresh' }
	| { type: 'reconnect' }
	| { type: 'setRange'; rangeDays: number }
	| { type: 'setGroup'; groupBy: 'space' | 'user' }
	| { type: 'openWalkthrough' }
	| { type: 'configureServer' };

export type AppCommand = { type: 'reconnect' } | { type: 'refreshAll' };

/** Commands the update banner may issue. */
export type UpdateCommand = { type: 'ready' } | { type: 'check' } | { type: 'dismiss' };

function str(v: unknown): string | undefined {
	return typeof v === 'string' && v.length > 0 ? v : undefined;
}

/**
 * Validate a confirm-panel message.
 *
 * Anything the extension already models is handed to the frozen parser. Only
 * `promoteSubmit` and `reconnect` are validated here.
 */
export function parseConfirmCommand(raw: unknown): ConfirmCommand | null {
	if (!raw || typeof raw !== 'object') {
		return null;
	}
	const m = raw as Record<string, unknown>;

	if (m.type === 'reconnect') {
		return { type: 'reconnect' };
	}

	if (m.type === 'promoteSubmit') {
		// The node type travels as `nodeType`, NOT `type`. It used to be `type`,
		// which collided with the message's own `type` discriminator: the object
		// literal set `type:'promoteSubmit'` and then a shorthand `type` for the
		// node type, so the second silently won and the message arrived as
		// {type:'note',...}. main's switch never matched it and Promote did
		// nothing at all, with no error anywhere. Keep these names distinct.
		const inboxId = str(m.inboxId);
		const nodeType = str(m.nodeType);
		const scope = str(m.scope);
		const title = str(m.title);
		// Body may legitimately be empty (a title-only memory is valid) but must
		// still be a string, never an object.
		const body = typeof m.body === 'string' ? m.body : undefined;
		if (!inboxId || !nodeType || !scope || !title || body === undefined) {
			return null;
		}
		return { type: 'promoteSubmit', inboxId, nodeType, scope, title, body };
	}

	// `promote` (open the authoring UI) is a webview-local action on the desktop:
	// the view opens its own modal via an invoke, so it never arrives here.
	const parsed = parseWebviewMessage(raw);
	if (!parsed || parsed.type === 'promote') {
		return null;
	}
	return parsed as ConfirmCommand;
}

/** Validate a stats-panel message. Only `reconnect` is desktop-only. */
export function parseStatsCommand(raw: unknown): StatsCommand | null {
	if (!raw || typeof raw !== 'object') {
		return null;
	}
	if ((raw as Record<string, unknown>).type === 'reconnect') {
		return { type: 'reconnect' };
	}
	return parseStatsMessage(raw);
}

/** Validate an app-channel message. */
export function parseAppCommand(raw: unknown): AppCommand | null {
	if (!raw || typeof raw !== 'object') {
		return null;
	}
	const t = (raw as Record<string, unknown>).type;
	if (t === 'reconnect') {
		return { type: 'reconnect' };
	}
	if (t === 'refreshAll') {
		return { type: 'refreshAll' };
	}
	return null;
}

/**
 * Validate an update-banner message.
 *
 * Turning the check off is NOT here: it is a persisted preference, so it goes
 * through the settings channel with the rest of them, and there is one path to
 * it rather than two.
 */
export function parseUpdateCommand(raw: unknown): UpdateCommand | null {
	if (!raw || typeof raw !== 'object') {
		return null;
	}
	const m = raw as Record<string, unknown>;
	if (m.type === 'ready' || m.type === 'check' || m.type === 'dismiss') {
		return { type: m.type };
	}
	return null;
}
