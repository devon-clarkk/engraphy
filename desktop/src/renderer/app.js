// Engraphy desktop — renderer shell.
//
// Owns the window chrome the VS Code extension got from the IDE: the custom
// title bar, sidebar nav, brand lockup, theme, the health badge, the global
// disconnected banner, toasts, the promote modal host, and the first-run
// onboarding overlay. It builds a per-view `host` object on top of the preload
// IPC bridge (window.engraphyIPC) shaped like the webview's acquireVsCodeApi(),
// then mounts each view. The views own their rendering; main owns every MCP call.
(function () {
	'use strict';

	// The brand mark is painted as a CSS mask so it can follow --brand-mark per
	// theme. A mask whose image fails to load paints NOTHING, with no console
	// error, which is how this shipped invisible for so long: the SVG carried an
	// XML comment containing "--brand-mark", and a double hyphen is illegal
	// inside an XML comment, so the file was not well-formed and never parsed.
	// scripts/copy-renderer.js now fails the build if that recurs.
	const LOOP_MARK_URI = 'assets/loop-mark.svg';
	const IPC = window.engraphyIPC;

	window.ENGRAPHY = { loopMarkUri: LOOP_MARK_URI };

	// ---- platform class ----------------------------------------------------
	// Windows draws the OS window controls as an overlay in the top-right and
	// macOS puts traffic lights top-left, so the layout has to reserve different
	// corners. Linux keeps a native frame and needs neither.
	const platform = (IPC && IPC.platform) || 'win32';
	document.body.classList.add('platform-' + platform);

	// Brand mark in the sidebar (CSS mask so it follows --brand-mark).
	function applyMask(node) {
		if (!node) {
			return;
		}
		node.style.webkitMaskImage = 'url("' + LOOP_MARK_URI + '")';
		node.style.maskImage = 'url("' + LOOP_MARK_URI + '")';
	}
	applyMask(document.getElementById('brand-mark'));

	// ---- theme: follow the OS colour scheme --------------------------------
	const darkMq = window.matchMedia('(prefers-color-scheme: dark)');
	function applyTheme(isDark) {
		const dark = isDark === undefined ? darkMq.matches : !!isDark;
		document.body.classList.toggle('vscode-dark', dark);
		document.body.classList.toggle('vscode-light', !dark);
	}
	applyTheme();
	darkMq.addEventListener('change', () => applyTheme());

	// ---- host factory (per channel) ----------------------------------------
	function makeHost(channel) {
		return {
			postMessage(msg) {
				IPC.send(channel, msg);
			},
			onMessage(cb) {
				return IPC.subscribe(channel, cb);
			},
			invoke(msg) {
				return IPC.invoke(channel, msg);
			},
			openExternal(url) {
				IPC.openExternal(url);
			},
		};
	}

	// ---- navigation --------------------------------------------------------
	const navButtons = Array.from(document.querySelectorAll('.nav-item'));
	const panels = Array.from(document.querySelectorAll('.panel'));
	const contexts = {};

	function navigate(to) {
		let matched = false;
		for (const b of navButtons) {
			const on = b.dataset.nav === to;
			b.classList.toggle('active', on);
			b.setAttribute('aria-selected', on ? 'true' : 'false');
			matched = matched || on;
		}
		if (!matched) {
			return;
		}
		for (const p of panels) {
			p.classList.toggle('active', p.dataset.panel === to);
		}
		// Views can re-run whatever they were showing when the panel comes back.
		const ctx = contexts[to];
		if (ctx && typeof ctx.onRefresh === 'function') {
			ctx.onRefresh();
		}
	}
	for (const b of navButtons) {
		b.addEventListener('click', () => navigate(b.dataset.nav));
	}
	window.ENGRAPHY.navigate = navigate;

	// ---- mount views -------------------------------------------------------
	function ctxFor(panelName, channel) {
		const panel = document.querySelector('.panel[data-panel="' + panelName + '"]');
		const ctx = {
			panel: panel,
			root: panel.querySelector('[data-root]'),
			refresh: panel.querySelector('[data-refresh]'),
			host: makeHost(channel),
		};
		contexts[panelName] = ctx;
		return ctx;
	}

	window.mountConfirm(ctxFor('confirm', 'confirm'));
	window.mountStats(ctxFor('stats', 'stats'));
	window.mountGraph(ctxFor('graph', 'graph'));
	window.initExplorer(ctxFor('explorer', 'explorer'));
	window.initSettings(ctxFor('settings', 'settings'), {
		onSaved: () => {
			checkHealth();
			reloadExplorer();
		},
	});

	/**
	 * Re-list memories after the connection changes. Without this, fixing a bad
	 * token in Settings left the Memories panel still showing the old recovery
	 * block until the user manually searched.
	 */
	function reloadExplorer() {
		const ex = contexts.explorer;
		if (ex && typeof ex.reload === 'function') {
			ex.reload();
		}
	}

	// ---- onboarding --------------------------------------------------------
	const onboarding = window.mountOnboarding({
		host: makeHost('onboarding'),
		settingsHost: makeHost('settings'),
		mount: document.getElementById('onboarding-host'),
		onSaved: () => {
			checkHealth();
			const s = contexts.settings;
			if (s && s.onRefresh) {
				s.onRefresh();
			}
		},
	});
	window.ENGRAPHY.showOnboarding = () => onboarding.show(1);
	// First run only. Never blocks startup if the check fails.
	onboarding.showIfFirstRun();

	// ---- app channel: toasts, navigation, theme, onboarding ---------------
	IPC.subscribe('app', (msg) => {
		if (!msg || typeof msg !== 'object') {
			return;
		}
		if (msg.type === 'toast') {
			toast(msg.text, msg.level);
		} else if (msg.type === 'navigate' && msg.to) {
			navigate(msg.to);
		} else if (msg.type === 'onboarding') {
			onboarding.show(msg.step || 1);
		} else if (msg.type === 'theme') {
			applyTheme(msg.dark);
		}
	});

	// ---- health badge + disconnected banner --------------------------------
	//
	// The badge phase comes from main, which probes /healthz AND an authenticated
	// MCP call. That second probe is the whole point: /healthz is unauthenticated
	// on Engraphy, so trusting it alone showed a green "Connected" badge over four
	// panels that were all failing with 401s.
	const healthEl = document.getElementById('health');
	const healthText = healthEl.querySelector('.health-text');
	const banner = document.getElementById('conn-banner');
	const bannerText = banner.querySelector('.banner-text');
	const bannerFix = banner.querySelector('.banner-fix');
	const bannerRetry = banner.querySelector('.banner-retry');

	const BANNER_COPY = {
		unauthorized: 'Your token was rejected. The server is running, but it will not let this app read anything.',
		unreachable: 'Cannot reach your Engraphy server.',
		unconfigured: 'No server connected yet.',
		degraded: 'The server answered, but the connection check failed.',
	};

	function renderHealth(h) {
		if (!h || typeof h !== 'object') {
			return;
		}
		const phase = h.phase || 'unconfigured';
		healthEl.className = 'health phase-' + phase;
		healthEl.setAttribute('data-phase', phase);
		healthText.textContent = h.label || '';
		healthEl.title = h.title || '';

		// The banner only appears for states the user must act on, and it always
		// carries the action that resolves it.
		// Prefer the line main built: it knows whether a token is set, so the
		// banner cannot contradict the badge or the panel.
		const copy = h.banner || BANNER_COPY[phase];
		if (h.usable || !copy) {
			banner.classList.add('hidden');
			return;
		}
		banner.classList.remove('hidden');
		banner.setAttribute('data-phase', phase);
		bannerText.textContent = copy;
		bannerFix.textContent = phase === 'unconfigured' ? 'Set up Engraphy' : 'Open Settings';
		bannerRetry.classList.toggle('hidden', phase === 'unconfigured');
	}

	bannerFix.addEventListener('click', () => {
		if (banner.getAttribute('data-phase') === 'unconfigured') {
			onboarding.show(1);
		} else {
			navigate('settings');
		}
	});
	bannerRetry.addEventListener('click', () => reconnect());

	// ---- update banner -----------------------------------------------------
	//
	// The window makes no network requests of its own: its CSP is
	// `connect-src 'none'`, so main runs the check and this only paints the
	// view-model it sends. Everything the banner shows, including whether there
	// is a download button at all, was decided in updateModel.ts, because that
	// depends on how this copy was installed.
	const updateBanner = document.getElementById('update-banner');
	const updateText = updateBanner.querySelector('.banner-text');
	const updateDetail = updateBanner.querySelector('.banner-detail');
	const updateAction = updateBanner.querySelector('.update-action');
	const updateNotes = updateBanner.querySelector('.update-notes');
	const updateDismiss = updateBanner.querySelector('.update-dismiss');
	let updateVM = null;

	function renderUpdate(msg) {
		const vm = msg && msg.vm;
		updateVM = vm || null;
		if (!vm || !vm.visible) {
			updateBanner.classList.add('hidden');
			return;
		}
		updateBanner.classList.remove('hidden');
		updateBanner.setAttribute('data-tone', vm.tone || 'info');
		updateText.textContent = vm.text || '';
		updateDetail.textContent = vm.detail || '';
		// A Microsoft Store install carries no download button: the Store
		// delivers the new version, and an installer would put a second copy
		// beside it.
		updateAction.classList.toggle('hidden', !vm.action);
		if (vm.action) {
			updateAction.textContent = vm.action.label;
		}
		updateNotes.classList.toggle('hidden', !vm.notes);
		if (vm.notes) {
			updateNotes.textContent = vm.notes.label;
		}
	}

	updateAction.addEventListener('click', () => {
		if (updateVM && updateVM.action) {
			IPC.openExternal(updateVM.action.url);
		}
	});
	updateNotes.addEventListener('click', () => {
		if (updateVM && updateVM.notes) {
			IPC.openExternal(updateVM.notes.url);
		}
	});
	updateDismiss.addEventListener('click', () => {
		IPC.send('update', { type: 'dismiss' });
	});

	IPC.subscribe('update', renderUpdate);
	IPC.send('update', { type: 'ready' });

	IPC.subscribe('health', renderHealth);

	function checkHealth() {
		IPC.invoke('health', { type: 'check' });
	}
	function reconnect() {
		healthEl.className = 'health phase-checking';
		healthText.textContent = 'Reconnecting…';
		IPC.invoke('health', { type: 'reconnect' }).then(reloadExplorer, () => {});
	}
	window.ENGRAPHY.reconnect = reconnect;

	healthEl.addEventListener('click', checkHealth);
	checkHealth();

	// Poll, but back off hard when the window is hidden so a minimised app is not
	// hammering a server (or a dead port) every 20 seconds forever.
	let pollTimer;
	function schedulePoll() {
		clearTimeout(pollTimer);
		pollTimer = setTimeout(() => {
			if (!document.hidden) {
				checkHealth();
			}
			schedulePoll();
		}, 20000);
	}
	schedulePoll();
	document.addEventListener('visibilitychange', () => {
		if (!document.hidden) {
			checkHealth();
		}
	});

	// ---- toasts ------------------------------------------------------------
	const toastHost = document.getElementById('toast-host');
	function toast(text, level) {
		const t = document.createElement('div');
		t.className = 'toast' + (level === 'warn' ? ' warn' : level === 'error' ? ' error' : '');
		t.setAttribute('role', 'status');
		t.textContent = text;
		toastHost.appendChild(t);
		setTimeout(() => {
			t.style.transition = 'opacity .4s';
			t.style.opacity = '0';
			setTimeout(() => t.remove(), 400);
		}, 4600);
	}
	window.ENGRAPHY.toast = toast;
})();
