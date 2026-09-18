// Update checking: the manifest shape, the comparison, and the poll schedule.
//
// Pure except for `fetchManifest`, which takes its fetcher as an argument so
// the whole module runs in plain Node under scripts/test-client.js. The desktop
// app carries the same module at src/main/versionCheck.ts, identical apart from
// PRODUCT_KEY, so a change here belongs there too and both suites cover it.
//
// WHY A MANIFEST RATHER THAN THE GITHUB RELEASE API
//
// One release stream carries three independently versioned products. The tag on
// devon-clarkk/engraphy is the ENGINE version: at v0.2.0 the desktop app is
// 0.1.0 and this extension is 0.5.2. A client comparing itself to the newest tag
// would read an update that does not exist, or a downgrade. The manifest states
// each product under its own key, so a client compares like with like. It is
// generated from the surfaces that already publish those numbers
// (engraphy-web/build-version.py), so a release bumps one place.
//
// PRIVACY
//
// The check is a plain GET of a static path. Nothing about the running client
// goes into the URL, the query string, or a header, so the request carries no
// more than any other fetch of a public file, and there is no version telemetry
// to opt out of. Every failure is swallowed: offline is a normal state, not an
// error worth a message.

/** Where the manifest lives. Overridable so a self-hoster can serve their own. */
export const DEFAULT_MANIFEST_URL = 'https://engraphy.tech/version.json';

/** This client's key inside the manifest's `products` map. */
export const PRODUCT_KEY = 'vscode-extension';

/** How long a check stays fresh. One a day is enough to be useful and not felt. */
export const CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;

/** Network budget. An update check must never be something the user waits on. */
export const FETCH_TIMEOUT_MS = 5000;

export interface ManifestDownload {
	kind?: string;
	url: string;
	size?: number;
	/** False until a signed artifact is actually published under this URL. */
	signed?: boolean;
	platform?: string;
	arch?: string;
	sha256?: string;
}

export interface ManifestProduct {
	latest: string | null;
	minimumSupported: string | null;
	notes: string | null;
	downloads: ManifestDownload[];
	/** Registry landing pages. A null value means that registry does not carry it. */
	registries: Record<string, string | null>;
}

export type UpdateState =
	/** Running the published version. */
	| 'current'
	/** Something newer is published. */
	| 'update'
	/** Older than the oldest version still supported. */
	| 'unsupported'
	/** Running ahead of what is published, which is what a local build looks like. */
	| 'ahead'
	/** No usable answer: offline, malformed, or nothing published for this product. */
	| 'unknown';

export interface UpdateVerdict {
	state: UpdateState;
	current: string;
	latest: string | null;
	minimumSupported: string | null;
	notes: string | null;
	downloads: ManifestDownload[];
	registries: Record<string, string | null>;
}

// ---------------------------------------------------------------------------
// version comparison
// ---------------------------------------------------------------------------

interface Parsed {
	core: number[];
	pre: string[];
}

/**
 * Parse a semver-shaped string. Build metadata (`+sha`) is dropped, because it
 * carries no ordering. Returns null for anything that is not a version, which
 * is what keeps a malformed manifest value from being read as 0.0.0 and
 * announcing an update to everyone.
 */
export function parseVersion(raw: unknown): Parsed | null {
	if (typeof raw !== 'string') {
		return null;
	}
	const text = raw.trim().replace(/^v/i, '');
	const m = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(text);
	if (!m) {
		return null;
	}
	return {
		core: [Number(m[1]), Number(m[2]), Number(m[3])],
		pre: m[4] ? m[4].split('.') : [],
	};
}

/** True when the string is a version this module can order. */
export function isVersion(raw: unknown): boolean {
	return parseVersion(raw) !== null;
}

function comparePre(a: string[], b: string[]): number {
	// A release outranks any pre-release of the same core, so 0.6.0 beats
	// 0.6.0-rc.1. This matters here: 0.6.0 sat unreleased in the changelog while
	// 0.5.2 was the published version, and a pre-release build of it must not
	// read as newer than the release that follows.
	if (a.length === 0 && b.length === 0) {
		return 0;
	}
	if (a.length === 0) {
		return 1;
	}
	if (b.length === 0) {
		return -1;
	}
	const n = Math.max(a.length, b.length);
	for (let i = 0; i < n; i++) {
		const x = a[i];
		const y = b[i];
		if (x === undefined) {
			return -1;
		}
		if (y === undefined) {
			return 1;
		}
		const xn = /^\d+$/.test(x);
		const yn = /^\d+$/.test(y);
		if (xn && yn) {
			const d = Number(x) - Number(y);
			if (d !== 0) {
				return d < 0 ? -1 : 1;
			}
			continue;
		}
		// Numeric identifiers rank below alphanumeric ones.
		if (xn !== yn) {
			return xn ? -1 : 1;
		}
		if (x !== y) {
			return x < y ? -1 : 1;
		}
	}
	return 0;
}

/**
 * Order two versions: negative when a is older, 0 when equal, positive when a
 * is newer. Returns null when either side is not a version, so a caller has to
 * handle "cannot tell" rather than receive a confident wrong answer.
 */
export function compareVersions(a: unknown, b: unknown): number | null {
	const pa = parseVersion(a);
	const pb = parseVersion(b);
	if (!pa || !pb) {
		return null;
	}
	for (let i = 0; i < 3; i++) {
		if (pa.core[i] !== pb.core[i]) {
			return pa.core[i] < pb.core[i] ? -1 : 1;
		}
	}
	return comparePre(pa.pre, pb.pre);
}

// ---------------------------------------------------------------------------
// manifest parsing
// ---------------------------------------------------------------------------

function str(v: unknown): string | null {
	return typeof v === 'string' && v.trim().length > 0 ? v.trim() : null;
}

function downloads(v: unknown): ManifestDownload[] {
	if (!Array.isArray(v)) {
		return [];
	}
	const out: ManifestDownload[] = [];
	for (const raw of v) {
		if (!raw || typeof raw !== 'object') {
			continue;
		}
		const d = raw as Record<string, unknown>;
		const url = str(d.url);
		// Only https. A manifest is fetched over the network, so treating its
		// URLs as trusted input would let a bad answer point the user's one-click
		// download anywhere.
		if (!url || !/^https:\/\//i.test(url)) {
			continue;
		}
		out.push({
			kind: str(d.kind) ?? undefined,
			url,
			size: typeof d.size === 'number' && d.size > 0 ? d.size : undefined,
			signed: d.signed === true,
			platform: str(d.platform) ?? undefined,
			arch: str(d.arch) ?? undefined,
			sha256: str(d.sha256) ?? undefined,
		});
	}
	return out;
}

function registries(v: unknown): Record<string, string | null> {
	if (!v || typeof v !== 'object' || Array.isArray(v)) {
		return {};
	}
	const out: Record<string, string | null> = {};
	for (const [k, raw] of Object.entries(v as Record<string, unknown>)) {
		const url = str(raw);
		out[k] = url && /^https:\/\//i.test(url) ? url : null;
	}
	return out;
}

/**
 * Read one product out of a manifest document. Returns null when the document
 * is not a manifest, is a schema this client does not read, or says nothing
 * about this product. A null is always treated as "no answer", never as
 * "up to date" and never as "update available".
 */
export function parseManifest(raw: unknown, productKey: string): ManifestProduct | null {
	if (!raw || typeof raw !== 'object') {
		return null;
	}
	const doc = raw as Record<string, unknown>;
	// An unknown schema is a document written for a client this is not. Reading
	// it anyway is how a future field change turns into a wrong prompt on every
	// old install still in the field.
	if (doc.schema !== 1) {
		return null;
	}
	const products = doc.products;
	if (!products || typeof products !== 'object') {
		return null;
	}
	const entry = (products as Record<string, unknown>)[productKey];
	if (!entry || typeof entry !== 'object') {
		return null;
	}
	const p = entry as Record<string, unknown>;
	const latest = str(p.latest);
	return {
		latest: isVersion(latest) ? latest : null,
		minimumSupported: isVersion(str(p.minimumSupported)) ? str(p.minimumSupported) : null,
		notes: str(p.notes),
		downloads: downloads(p.downloads),
		registries: registries(p.registries),
	};
}

// ---------------------------------------------------------------------------
// the verdict
// ---------------------------------------------------------------------------

/**
 * What to tell the user, given what is running and what the manifest says.
 *
 * `ahead` exists so a local build never reads as out of date: during
 * development the running version routinely exceeds the published one, and a
 * client that prompted then would be wrong every day of a release cycle.
 */
export function evaluate(current: unknown, product: ManifestProduct | null): UpdateVerdict {
	const running = str(current) ?? '0.0.0';
	const base: UpdateVerdict = {
		state: 'unknown',
		current: running,
		latest: product?.latest ?? null,
		minimumSupported: product?.minimumSupported ?? null,
		notes: product?.notes ?? null,
		downloads: product?.downloads ?? [],
		registries: product?.registries ?? {},
	};
	if (!product || !product.latest) {
		return base;
	}
	const cmp = compareVersions(running, product.latest);
	if (cmp === null) {
		return base;
	}
	if (cmp > 0) {
		return { ...base, state: 'ahead' };
	}
	if (cmp === 0) {
		return { ...base, state: 'current' };
	}
	// Older than what is published. Below the supported floor is a firmer
	// message than "something newer exists", so the two are separate states.
	const floor = product.minimumSupported;
	if (floor) {
		const belowFloor = compareVersions(running, floor);
		if (belowFloor !== null && belowFloor < 0) {
			return { ...base, state: 'unsupported' };
		}
	}
	return { ...base, state: 'update' };
}

/** Pick the artifact for this machine, or null when the release carries none. */
export function pickDownload(
	list: ManifestDownload[],
	platform: string,
	arch: string
): ManifestDownload | null {
	const named = list.filter((d) => d.platform !== undefined || d.arch !== undefined);
	const exact = named.find((d) => d.platform === platform && d.arch === arch);
	if (exact) {
		return exact;
	}
	const byPlatform = named.find((d) => d.platform === platform && d.arch === undefined);
	if (byPlatform) {
		return byPlatform;
	}
	// An entry that names no platform is platform-independent, which is what a
	// .vsix is. An entry that names a DIFFERENT platform is never a fallback.
	return list.find((d) => d.platform === undefined && d.arch === undefined) ?? null;
}

// ---------------------------------------------------------------------------
// scheduling and dismissal
// ---------------------------------------------------------------------------

/** True when the last check is older than the interval, or never happened. */
export function shouldCheck(
	lastCheckedMs: unknown,
	nowMs: number,
	intervalMs: number = CHECK_INTERVAL_MS
): boolean {
	if (typeof lastCheckedMs !== 'number' || !Number.isFinite(lastCheckedMs)) {
		return true;
	}
	// A clock that moved backwards (a timezone fix, a restored machine) would
	// otherwise park the next check arbitrarily far in the future.
	if (lastCheckedMs > nowMs) {
		return true;
	}
	return nowMs - lastCheckedMs >= intervalMs;
}

/**
 * True when the user has already dismissed this exact version.
 *
 * Dismissal is per version, not forever: saying "not now" to 0.6.0 is not
 * saying it about 0.7.0, and a permanent dismissal would quietly turn the
 * check off for good.
 */
export function isDismissed(dismissedVersion: unknown, latest: unknown): boolean {
	const d = str(dismissedVersion);
	const l = str(latest);
	if (!d || !l) {
		return false;
	}
	if (d === l) {
		return true;
	}
	// Dismissing 0.7.0 also covers 0.6.0 arriving late from a stale cache.
	const cmp = compareVersions(d, l);
	return cmp !== null && cmp >= 0;
}

// ---------------------------------------------------------------------------
// the fetch
// ---------------------------------------------------------------------------

export type Fetcher = (url: string, init: { signal: AbortSignal }) => Promise<{
	ok: boolean;
	status: number;
	json: () => Promise<unknown>;
}>;

/**
 * GET the manifest. Returns null on every failure, including a non-200, a body
 * that is not JSON, and a timeout. Offline is the common case and it is not an
 * error: the caller shows nothing and tries again tomorrow.
 */
export async function fetchManifest(
	url: string,
	fetcher: Fetcher,
	timeoutMs: number = FETCH_TIMEOUT_MS
): Promise<unknown | null> {
	if (!/^https:\/\//i.test(url)) {
		return null;
	}
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const res = await fetcher(url, { signal: controller.signal });
		if (!res.ok) {
			return null;
		}
		return await res.json();
	} catch {
		return null;
	} finally {
		clearTimeout(timer);
	}
}

/** The whole check, end to end. Never throws and never resolves to a surprise. */
export async function checkForUpdate(
	current: string,
	url: string,
	fetcher: Fetcher,
	productKey: string = PRODUCT_KEY,
	timeoutMs: number = FETCH_TIMEOUT_MS
): Promise<UpdateVerdict> {
	const doc = await fetchManifest(url, fetcher, timeoutMs);
	return evaluate(current, parseManifest(doc, productKey));
}
