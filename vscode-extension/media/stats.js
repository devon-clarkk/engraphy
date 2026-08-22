// Engraphy Stats panel — webview client script.
//
// Runs in the sandboxed webview (no Node; server values go through textContent,
// never innerHTML). Talks to the host by postMessage using the typed protocol in
// src/statsModel.ts. The host owns the `stats` MCP call; this renders the
// view-model it is handed (tiles + normalized sparkline series) and reports the
// toggle/range/refresh clicks. Sparklines are built with createElementNS — no
// external chart library, nonce/CSP respected.
(function () {
	'use strict';
	const SVG_NS = 'http://www.w3.org/2000/svg';
	const vscode = acquireVsCodeApi();

	const els = {
		root: document.getElementById('root'),
		refresh: document.getElementById('refresh'),
		mark: document.getElementById('mark'),
	};
	const markUri = els.mark && els.mark.getAttribute('data-mark-uri');
	if (markUri) {
		els.mark.style.webkitMaskImage = 'url("' + markUri + '")';
		els.mark.style.maskImage = 'url("' + markUri + '")';
	}

	let busy = false;
	function post(msg) {
		vscode.postMessage(msg);
	}
	function setBusy(on) {
		busy = on;
		document.body.classList.toggle('busy', on);
		for (const b of document.querySelectorAll('button')) {
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

	// ---- no-server onboarding (shared look with confirm queue) -------------

	function noServerBlock(conn) {
		const wrap = el('div', 'onboarding');
		const mark = el('div', 'mark mark-lg');
		if (markUri) {
			mark.style.webkitMaskImage = 'url("' + markUri + '")';
			mark.style.maskImage = 'url("' + markUri + '")';
		}
		wrap.appendChild(mark);
		wrap.appendChild(el('h2', 'onboarding-title brand-mono', 'No server connected'));
		wrap.appendChild(
			el(
				'p',
				'onboarding-msg',
				conn.reason === 'unconfigured'
					? 'Engraphy isn’t pointed at a memory server yet — connect one to see your impact stats.'
					: 'Can’t reach the Engraphy server at your configured URL. Start one, then refresh.'
			)
		);
		const actions = el('div', 'actions onboarding-actions');
		actions.appendChild(button('btn btn-approve', 'Set up Engraphy', () => action({ type: 'openWalkthrough' })));
		actions.appendChild(button('btn btn-secondary', 'Paste URL + token', () => action({ type: 'configureServer' })));
		actions.appendChild(button('btn btn-ghost', 'Refresh', () => action({ type: 'refresh' })));
		wrap.appendChild(actions);
		wrap.appendChild(
			el('p', 'onboarding-note', 'Hosted Engraphy is coming soon — run locally with Docker or connect your own server.')
		);
		if (conn.detail) {
			const det = el('details');
			det.appendChild(el('summary', 'cand-label', 'Connection error detail'));
			const pre = el('pre', 'payload');
			pre.textContent = conn.detail;
			det.appendChild(pre);
			wrap.appendChild(det);
		}
		return wrap;
	}

	// ---- render -------------------------------------------------------------

	function render(state) {
		setBusy(false);
		const root = els.root;
		root.textContent = '';

		if (state.connection && state.connection.kind === 'no-server') {
			root.appendChild(noServerBlock(state.connection));
			return;
		}

		// Controls are always shown so the toggle/range reflect the selection even
		// while loading or after an error.
		root.appendChild(controls(state));

		if (state.loading) {
			const n = note('Loading stats…');
			n.prepend(el('span', 'spinner'));
			root.appendChild(n);
			return;
		}
		if (state.error || !state.view) {
			root.appendChild(note(state.error || 'No stats available.', true));
			return;
		}

		const v = state.view;

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

	// ---- host → webview -----------------------------------------------------

	window.addEventListener('message', (event) => {
		const msg = event.data;
		if (!msg || typeof msg !== 'object') {
			return;
		}
		if (msg.type === 'state') {
			render(msg.state);
		} else if (msg.type === 'theme') {
			document.body.dataset.themeKind = msg.kind;
		}
	});

	post({ type: 'ready' });
})();
