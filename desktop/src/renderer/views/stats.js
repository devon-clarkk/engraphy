// Engraphy Stats panel — view script.
//
// PORTED from the VS Code extension's media/stats.js (v0.4.0). The sparkline /
// tiles / controls rendering is verbatim; only the shell wiring changed for the
// desktop app (same edits as confirm.js): IIFE → window.mountStats(ctx),
// acquireVsCodeApi() → ctx.host, panel-scoped busy state, and ctx.host.onMessage
// instead of the window 'message' listener.
//
// DESKTOP DIVERGENCE (see DECISIONS.md §12): the not-connected block became the
// shared three-way recovery block, the error path renders a classified,
// retryable error instead of a raw string, first load shows tile skeletons, and
// an all-zero result is treated as a real empty state ("nothing recorded yet")
// rather than a grid of zeroes.
//
// No Node; server values go through textContent. The host owns the `stats` call.
window.mountStats = function (ctx) {
	'use strict';
	const SVG_NS = 'http://www.w3.org/2000/svg';
	const vscode = ctx.host;
	const S = window.ENGRAPHY_STATES;

	const els = { root: ctx.root, refresh: ctx.refresh, panel: ctx.panel };

	let busy = false;
	function post(msg) {
		vscode.postMessage(msg);
	}
	function setBusy(on) {
		busy = on;
		els.panel.classList.toggle('busy', on);
		for (const b of els.panel.querySelectorAll('button')) {
			if (b.dataset.keepEnabled) {
				continue;
			}
			b.disabled = on;
		}
	}
	function action(msg) {
		if (busy) {
			return;
		}
		setBusy(true);
		post(msg);
	}
	els.refresh.addEventListener('click', () => action({ type: 'refresh' }));

	// ---- DOM helpers (textContent only) ------------------------------------

	function el(tag, cls, text) {
		const n = document.createElement(tag);
		if (cls) {
			n.className = cls;
		}
		if (text != null) {
			n.textContent = text;
		}
		return n;
	}
	function button(cls, label, onClick, title) {
		const b = el('button', cls, label);
		if (title) {
			b.title = title;
		}
		b.addEventListener('click', onClick);
		return b;
	}
	function note(text, isError) {
		return el('div', 'note' + (isError ? ' error' : ''), text);
	}
	function svgEl(tag, attrs) {
		const n = document.createElementNS(SVG_NS, tag);
		for (const k in attrs) {
			n.setAttribute(k, String(attrs[k]));
		}
		return n;
	}

	// ---- sparkline (mini bar chart) ----------------------------------------

	function sparkline(norm, hero) {
		const n = norm.length || 1;
		const step = 3;
		const barW = 2;
		const H = hero ? 30 : 20;
		const W = n * step;
		const svg = svgEl('svg', {
			class: 'spark' + (hero ? ' spark-hero' : ''),
			viewBox: '0 0 ' + W + ' ' + H,
			preserveAspectRatio: 'none',
			'aria-hidden': 'true',
		});
		// baseline — makes an all-zero series read as a flat line, not emptiness.
		svg.appendChild(svgEl('rect', { class: 'spark-base', x: 0, y: H - 0.75, width: W, height: 0.75 }));
		norm.forEach((f, i) => {
			const clamped = f < 0 ? 0 : f > 1 ? 1 : f;
			let h = clamped * (H - 2);
			if (clamped > 0 && h < 1) {
				h = 1; // keep tiny non-zero days visible
			}
			if (h <= 0) {
				return;
			}
			svg.appendChild(
				svgEl('rect', { class: 'spark-bar', x: i * step, y: H - h, width: barW, height: h })
			);
		});
		return svg;
	}

	// ---- tiles --------------------------------------------------------------

	function infoMark(title) {
		const i = el('span', 'info', 'ⓘ');
		i.title = title;
		i.setAttribute('aria-label', title);
		return i;
	}

	function tileEl(t) {
		const card = el('div', 'tile' + (t.hero ? ' tile-hero' : ''));
		const head = el('div', 'tile-head');
		head.appendChild(el('span', 'tile-label', t.label));
		if (t.note) {
			head.appendChild(infoMark(t.note));
		}
		card.appendChild(head);
		card.appendChild(el('div', 'tile-value brand-mono', formatNum(t.value)));
		if (t.sub) {
			card.appendChild(el('div', 'tile-sub', t.sub));
		}
		card.appendChild(sparkline(t.spark || [], t.hero));
		return card;
	}

	function formatNum(v) {
		try {
			return Number(v).toLocaleString();
		} catch (e) {
			return String(v);
		}
	}

	// ---- controls (scope toggle + range) -----------------------------------

	function controls(state) {
		const wrap = el('div', 'controls');

		const seg = el('div', 'segmented');
		seg.setAttribute('role', 'group');
		seg.appendChild(segButton('Whole space', state.groupBy === 'space', () => action({ type: 'setGroup', groupBy: 'space' })));
		seg.appendChild(segButton('You', state.groupBy === 'user', () => action({ type: 'setGroup', groupBy: 'user' })));
		wrap.appendChild(seg);

		const range = el('div', 'range');
		[7, 14, 30].forEach((d) => {
			range.appendChild(
				segButton(d + 'd', state.rangeDays === d, () => action({ type: 'setRange', rangeDays: d }), 'range')
			);
		});
		wrap.appendChild(range);
		return wrap;
	}
	function segButton(label, active, onClick, extra) {
		const b = button('seg-btn' + (extra ? ' ' + extra : '') + (active ? ' active' : ''), label, onClick);
		if (active) {
			b.setAttribute('aria-pressed', 'true');
		}
		return b;
	}

	// ---- recovery block (shared look with the confirm queue) ---------------

	function recovery(conn) {
		return S.recoveryBlock(
			conn,
			{
				onSetup: () => post({ type: 'openWalkthrough' }),
				onSettings: () => post({ type: 'configureServer' }),
				onRetry: () => post({ type: 'refresh' }),
				onReconnect: () => post({ type: 'reconnect' }),
			},
			{ what: 'what Engraphy is doing for you' }
		);
	}

	/** True when the server answered but has no activity recorded yet. */
	function isEmptyView(v) {
		if (!v || !v.tiles) {
			return false;
		}
		return v.tiles.every((t) => !t.value);
	}

	// ---- render -------------------------------------------------------------

	function render(state) {
		const root = els.root;
		busy = false;
		els.panel.classList.toggle('busy', !!state.loading);
		root.textContent = '';

		if (state.connection && state.connection.kind !== 'ok') {
			root.appendChild(recovery(state.connection));
			for (const b of els.panel.querySelectorAll('button')) {
				b.disabled = false;
			}
			return;
		}

		// Controls are always shown so the toggle/range reflect the selection even
		// while loading or after an error.
		root.appendChild(controls(state));

		if (state.loading && !state.loaded) {
			root.appendChild(S.skeleton(4, 'tile'));
			setBusy(true);
			return;
		}
		if (state.error) {
			// A stats-tool failure that is not a connection problem: keep the
			// controls usable and offer a retry rather than emptying the panel.
			root.appendChild(S.errorBlock(state.error, () => action({ type: 'refresh' })));
			return;
		}
		if (!state.view) {
			root.appendChild(
				S.stateBlock({
					title: 'No stats yet',
					message: 'The server did not return any usage data for this range.',
					actions: [{ label: 'Retry', kind: 'primary', onClick: () => action({ type: 'refresh' }) }],
				})
			);
			return;
		}

		const v = state.view;

		// A brand-new space answers successfully with all-zero counters. That is a
		// legitimate empty state, not an error, and it deserves an explanation
		// rather than a wall of zeroes.
		if (isEmptyView(v)) {
			root.appendChild(
				S.stateBlock({
					title: 'Nothing recorded yet',
					message:
						'Connected, but no activity in the last ' +
						state.rangeDays +
						' days. These counters fill in as your agents search and write memories.',
					actions: [
						{ label: 'Refresh', kind: 'primary', onClick: () => action({ type: 'refresh' }) },
						{
							label: 'Try 30 days',
							kind: 'ghost',
							onClick: () => action({ type: 'setRange', rangeDays: 30 }),
						},
					],
					note: 'Counters are recorded server side per space, so activity from any client shows up here.',
				})
			);
			setBusy(!!state.loading);
			return;
		}

		// Scope line: what "Whole space" / "You" means for this data.
		const scope = el('div', 'scope-line');
		scope.appendChild(el('span', 'scope-label brand-mono', v.scopeLabel));
		scope.appendChild(el('span', 'scope-help', v.scopeHelp));
		root.appendChild(scope);

		// Hero tiles first, then supporting.
		const heroGrid = el('div', 'kpi-grid hero-grid');
		v.tiles.filter((t) => t.hero).forEach((t) => heroGrid.appendChild(tileEl(t)));
		root.appendChild(heroGrid);

		const grid = el('div', 'kpi-grid');
		v.tiles.filter((t) => !t.hero).forEach((t) => grid.appendChild(tileEl(t)));
		root.appendChild(grid);

		// Honest footnotes — this is a sell tool; don't overclaim.
		const foot = el('div', 'footnotes');
		foot.appendChild(
			el('p', null, 'Memory reused is a proxy: facts returned in searches, not distinct-node reuse.')
		);
		foot.appendChild(el('p', null, 'Answer rate = answered ÷ questions asked.'));
		if (v.generatedAt) {
			foot.appendChild(el('p', 'muted', 'Last ' + v.rangeDays + ' days · generated ' + v.generatedAt));
		}
		root.appendChild(foot);
	}

	// ---- host → view --------------------------------------------------------

	ctx.host.onMessage((msg) => {
		if (!msg || typeof msg !== 'object') {
			return;
		}
		if (msg.type === 'state') {
			render(msg.state);
		}
	});

	post({ type: 'ready' });
};
