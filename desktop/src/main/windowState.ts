// Window geometry restore, kept pure so it can be tested.
//
// Restoring saved bounds is one of those features that works on the developer's
// machine and breaks on the user's: the window was last closed on a second
// monitor that is no longer attached, or on a display whose resolution changed,
// and the app reopens entirely off-screen with no way to drag it back. This
// module decides what is safe to restore; main.ts supplies the real display list
// from `screen.getAllDisplays()`.
//
// Pure: no electron import. Covered by scripts/test-client.js.

export interface Bounds {
	x?: number;
	y?: number;
	width: number;
	height: number;
	maximized?: boolean;
}

/** The part of a display an app may use (Electron's Display.workArea). */
export interface WorkArea {
	x: number;
	y: number;
	width: number;
	height: number;
}

export const DEFAULT_BOUNDS = { width: 1080, height: 760 };
export const MIN_WIDTH = 760;
export const MIN_HEIGHT = 540;

/** How much of the window must overlap a display for the position to be usable. */
const MIN_VISIBLE_X = 80;
const MIN_VISIBLE_Y = 40;

export interface RestoredBounds {
	width: number;
	height: number;
	x?: number;
	y?: number;
}

function usableNumber(v: unknown): v is number {
	return typeof v === 'number' && Number.isFinite(v);
}

/**
 * True when enough of the window would land inside this work area for the user
 * to grab its title bar. A window whose top-left is off-screen is fine as long
 * as a usable strip overlaps.
 */
export function intersectsWorkArea(b: Bounds, wa: WorkArea): boolean {
	if (!usableNumber(b.x) || !usableNumber(b.y)) {
		return false;
	}
	const overlapsX = b.x < wa.x + wa.width && b.x + Math.max(b.width, MIN_VISIBLE_X) > wa.x;
	const overlapsY = b.y < wa.y + wa.height && b.y + MIN_VISIBLE_Y > wa.y;
	return overlapsX && overlapsY;
}

/**
 * Turn saved bounds into constructor options.
 *
 * Size is always clamped to the app's minimum so a corrupt or ancient file can
 * never produce an unusably tiny window. Position is only restored when it still
 * lands on a display that exists RIGHT NOW; otherwise it is dropped and the OS
 * centres the window, which is the recoverable outcome.
 */
export function restoreBounds(saved: Bounds | undefined, displays: WorkArea[]): RestoredBounds {
	if (!saved || !usableNumber(saved.width) || !usableNumber(saved.height)) {
		return { ...DEFAULT_BOUNDS };
	}
	const width = Math.max(MIN_WIDTH, Math.round(saved.width));
	const height = Math.max(MIN_HEIGHT, Math.round(saved.height));
	const sized: RestoredBounds = { width, height };

	if (!usableNumber(saved.x) || !usableNumber(saved.y)) {
		return sized;
	}
	const probe: Bounds = { x: saved.x, y: saved.y, width, height };
	if (displays.some((d) => intersectsWorkArea(probe, d))) {
		sized.x = Math.round(saved.x);
		sized.y = Math.round(saved.y);
	}
	return sized;
}

/** Reject junk read back from disk before it reaches the window. */
export function sanitizeSavedBounds(raw: unknown): Bounds | undefined {
	if (!raw || typeof raw !== 'object') {
		return undefined;
	}
	const b = raw as Bounds;
	if (!usableNumber(b.width) || !usableNumber(b.height)) {
		return undefined;
	}
	if (b.width < 200 || b.height < 200 || b.width > 20000 || b.height > 20000) {
		return undefined;
	}
	return {
		width: b.width,
		height: b.height,
		x: usableNumber(b.x) ? b.x : undefined,
		y: usableNumber(b.y) ? b.y : undefined,
		maximized: !!b.maximized,
	};
}
