// Noticing writes that are waiting for review.
//
// A write that lands in the dedup band is parked in the pending queue and
// expires 24 hours later, so a pending item the user never sees is a memory
// that is quietly lost. main.ts polls pending_list in the background and hands
// every SUCCESSFUL result here; this module decides what is new.
//
// Pure, so it is covered by scripts/test-client.js. The rules that keep it
// quiet:
//   • Expired rows are dropped before anything is counted. The server returns
//     them (there is no sweeper) and resolve_duplicate refuses them, so they are
//     not an action anyone can take.
//   • Only successful fetches are observed. A failed fetch says nothing about
//     the queue, and treating it as empty would announce every item again the
//     moment the server came back.
//   • The first observation seeds silently. Items already waiting when the app
//     starts are counted on the badge, not announced.
//   • An id is announced once. Ids that leave the queue are forgotten, so the
//     seen set is bounded by the queue itself.

import { isExpired, type PendingListItem } from './client/toolResult';

export interface PendingObservation {
	/** Non-expired items in the queue right now. Drives the badge. */
	active: number;
	/** Ids that were not in the queue at the previous observation. */
	fresh: string[];
	/** True when this observation should be announced to the user. */
	announce: boolean;
}

export class PendingWatch {
	private seen = new Set<string>();
	private seeded = false;

	/**
	 * Record one successful pending_list result. `announceable` is false for
	 * fetches the user caused (opening or refreshing the panel, resolving an
	 * item): they are looking at the queue already.
	 */
	observe(items: PendingListItem[], announceable: boolean, now: number = Date.now()): PendingObservation {
		const live = items.filter((i) => i.id && !isExpired(i, now));
		const ids = new Set(live.map((i) => i.id));
		const fresh = [...ids].filter((id) => !this.seen.has(id));
		const announce = announceable && this.seeded && fresh.length > 0;
		this.seen = ids;
		this.seeded = true;
		return { active: ids.size, fresh, announce };
	}

	/** Forget everything, so a new connection seeds silently again. */
	reset(): void {
		this.seen = new Set();
		this.seeded = false;
	}
}

/** One coalesced line for however many items arrived together. */
export function pendingNotificationBody(count: number): string {
	return count === 1
		? 'A memory write is waiting for your review.'
		: `${count} memory writes are waiting for your review.`;
}

/** Badge text for the nav item. Empty hides it. */
export function pendingBadgeLabel(active: number): string {
	if (active <= 0) {
		return '';
	}
	return active > 99 ? '99+' : String(active);
}
