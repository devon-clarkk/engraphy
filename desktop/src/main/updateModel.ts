// The update banner's view-model: what the window says when a newer version is
// published, and what its button does.
//
// Pure, so scripts/test-client.js covers it. versionCheck.ts decides whether an
// update exists; this decides what that means for THIS install, which is not
// the same question. The distribution channel changes the answer completely:
//
//   Microsoft Store   the Store delivers new versions on its own, so the app
//                     says a newer version is published and offers no download.
//                     Handing an MSIX install an .exe would put a second,
//                     separately-updated copy of Engraphy on the machine.
//   installer         a direct download, where the app can take the user to the
//                     installer and no further: Windows runs it, not Engraphy.
//
// `process.windowsStore` is the signal, and Electron sets it, so the answer
// comes from the running process rather than from a guess.

import { pickDownload, type ManifestDownload, type UpdateVerdict } from './versionCheck';

/** How this copy of Engraphy was installed, which decides how it updates. */
export type UpdateChannel = 'microsoft-store' | 'installer';

export interface UpdateAction {
	label: string;
	url: string;
}

export interface UpdateBannerVM {
	visible: boolean;
	tone: 'info' | 'warn';
	/** The headline, which always names both versions. */
	text: string;
	/** A second line when there is something the user needs to know first. */
	detail: string | null;
	/** The download, or null when this channel updates itself. */
	action: UpdateAction | null;
	notes: UpdateAction | null;
	latest: string | null;
	current: string;
}

const HIDDEN: UpdateBannerVM = {
	visible: false,
	tone: 'info',
	text: '',
	detail: null,
	action: null,
	notes: null,
	latest: null,
	current: '',
};

/**
 * Read the channel off the running process.
 *
 * Electron sets `process.windowsStore` to true inside an AppX/MSIX container.
 * Everything else is a direct install.
 */
export function detectChannel(
	// `windowsStore` is Electron's addition to the process object, so the cast
	// is what lets this module compile standalone for the test run as well as
	// inside the app.
	proc: { windowsStore?: boolean } = process as unknown as { windowsStore?: boolean }
): UpdateChannel {
	return proc.windowsStore === true ? 'microsoft-store' : 'installer';
}

/** How large the download is, in the units a person reads. */
export function formatSize(bytes: number | undefined): string | null {
	if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes <= 0) {
		return null;
	}
	const mb = bytes / (1024 * 1024);
	if (mb >= 1) {
		return `${mb.toFixed(0)} MB`;
	}
	return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function detailFor(channel: UpdateChannel, download: ManifestDownload | null): string | null {
	if (channel === 'microsoft-store') {
		return 'The Microsoft Store delivers new versions to this install.';
	}
	if (!download) {
		return null;
	}
	const size = formatSize(download.size);
	const parts: string[] = [];
	if (size) {
		parts.push(`The installer is ${size}.`);
	}
	if (download.signed !== true) {
		// Stated because it is what the user meets next, and knowing it in
		// advance is the difference between finishing the update and stopping.
		parts.push('Windows asks for confirmation before running it: choose More info, then Run anyway.');
	}
	return parts.length > 0 ? parts.join(' ') : null;
}

/**
 * Build the banner.
 *
 * Hidden for every state except a published version newer than the running one:
 * a local build ahead of the release, a client already current, and a check
 * that reached nothing all show the user nothing.
 */
export function buildUpdateBanner(
	verdict: UpdateVerdict,
	channel: UpdateChannel,
	platform: string,
	arch: string
): UpdateBannerVM {
	if (verdict.state !== 'update' && verdict.state !== 'unsupported') {
		return { ...HIDDEN, current: verdict.current, latest: verdict.latest };
	}

	const notes: UpdateAction | null = verdict.notes
		? { label: 'Release notes', url: verdict.notes }
		: null;

	const headline =
		verdict.state === 'unsupported'
			? `Engraphy ${verdict.latest} is available. This app is running ${verdict.current}, which is below the oldest supported version.`
			: `Engraphy ${verdict.latest} is available. This app is running ${verdict.current}.`;

	if (channel === 'microsoft-store') {
		// No download button on purpose: an .exe here installs a second copy
		// beside the one the Store manages.
		return {
			visible: true,
			tone: verdict.state === 'unsupported' ? 'warn' : 'info',
			text: headline,
			detail: detailFor(channel, null),
			action: null,
			notes,
			latest: verdict.latest,
			current: verdict.current,
		};
	}

	const download = pickDownload(verdict.downloads, platform, arch);
	return {
		visible: true,
		tone: verdict.state === 'unsupported' ? 'warn' : 'info',
		text: headline,
		detail: detailFor(channel, download),
		// A release with no build for this machine offers the notes rather than
		// a button that cannot deliver what its label promises.
		action: download ? { label: 'Download update', url: download.url } : null,
		notes,
		latest: verdict.latest,
		current: verdict.current,
	};
}
