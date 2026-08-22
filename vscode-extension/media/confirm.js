// Engraphy confirm-write queue — webview client script.
//
// Runs in the sandboxed webview (no Node, no DOM-injection of server strings:
// every server value goes through textContent, never innerHTML). Talks to the
// extension host purely by postMessage using the typed protocol in
// src/webviewMessages.ts. The host owns all MCP calls; this script only renders
// the state it is handed and reports button clicks.
(function () {
	'use strict';

	const vscode = acquireVsCodeApi();

	const els = {
		root: document.getElementById('root'),
		refresh: document.getElementById('refresh'),
		mark: document.getElementById('mark'),
	};

	// The loop mark ships as an SVG, passed in via asWebviewUri on a data-attr,
	// and painted as a CSS mask so its color follows the theme accent.
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

	// Any action optimistically disables the UI; the host replies with fresh
	// state (which re-renders and re-enables) whether it succeeded or failed.
	function action(msg) {
		if (busy) {
			return;
		}
		setBusy(true);
		post(msg);
	}

	els.refresh.addEventListener('click', () => action({ type: 'refresh' }));

	// ---- small DOM helpers (textContent only — no HTML from server data) ----

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

	// ---- pending card -------------------------------------------------------

	function pendingCard(p) {
		const card = el('div', 'card' + (p.expired ? ' expired' : ''));

		card.appendChild(el('div', 'card-preview', p.preview));

		const badges = el('div', 'badges');
		badges.appendChild(el('span', 'badge', 'pending duplicate'));
		if (p.expired) {
			badges.appendChild(el('span', 'badge badge-expired', 'EXPIRED'));
		}
		card.appendChild(badges);

		if (p.candidates.length > 0) {
			card.appendChild(el('div', 'cand-label', 'Deny — merge into one of these:'));
			for (const c of p.candidates) {
				const row = el('div', 'cand');
				const main = el('div', 'cand-main');
				main.appendChild(el('div', 'cand-title', c.title));
				const sim = el('div', 'cand-sim');
				sim.appendChild(el('span', 'cand-pct brand-mono', c.similarityPct + '%'));
				const meter = el('div', 'meter');
				const fill = el('span');
				fill.style.width = Math.max(0, Math.min(100, c.similarityPct)) + '%';
				meter.appendChild(fill);
				sim.appendChild(meter);
				main.appendChild(sim);
				row.appendChild(main);
				row.appendChild(
					button(
						'btn btn-secondary btn-merge',
						'Merge into ↩',
						() => action({ type: 'merge', pendingId: p.id, mergeInto: c.id }),
						p.expired ? 'This row is expired — the server will refuse the merge.' : 'Merge into ' + c.title
					)
				);
				card.appendChild(row);
			}
		} else {
			card.appendChild(note('No candidate nodes recorded on this row.'));
		}

		const actions = el('div', 'actions');
		actions.appendChild(
			button(
				'btn btn-approve',
				'✓ Approve (keep distinct)',
				() => action({ type: 'approve', pendingId: p.id }),
				p.expired ? 'This row is expired — the server will refuse the approve.' : 'Keep as a new, distinct node'
			)
		);
		card.appendChild(actions);

		const metaBits = [];
		if (p.createdAt) {
			metaBits.push('created ' + p.createdAt);
		}
		if (p.expiresAt) {
			metaBits.push((p.expired ? 'expired ' : 'expires ') + p.expiresAt);
		}
		if (metaBits.length) {
			card.appendChild(el('div', 'meta', metaBits.join(' · ')));
		}
		return card;
	}

	// ---- inbox card ---------------------------------------------------------

	function inboxCard(it) {
		const card = el('div', 'card');
		card.appendChild(el('div', 'card-preview', it.preview));

		const badges = el('div', 'badges');
		badges.appendChild(el('span', 'badge', it.kind));
		badges.appendChild(el('span', 'badge', 'scope: ' + it.scope));
		card.appendChild(badges);

		const details = el('details');
		details.appendChild(el('summary', 'cand-label', 'Captured payload (reference only)'));
		const pre = el('pre', 'payload');
		pre.textContent = it.payloadJson;
		details.appendChild(pre);
		card.appendChild(details);

		const actions = el('div', 'actions');
		actions.appendChild(
			button('btn btn-approve', '✓ Promote…', () => action({ type: 'promote', inboxId: it.id }), 'Author a node from this item')
		);
		actions.appendChild(button('btn btn-ghost', 'Discard', () => action({ type: 'discard', inboxId: it.id }), 'Drop this inbox item'));
		card.appendChild(actions);

		if (it.createdAt) {
			card.appendChild(el('div', 'meta', 'captured ' + it.createdAt));
		}
		return card;
	}

	// ---- section renderers --------------------------------------------------

	function emptyState(text) {
		const wrap = el('div', 'empty');
		wrap.appendChild(el('div', 'rings'));
		wrap.appendChild(el('div', null, text));
		return wrap;
	}

	function section(titleText, count, error, items, buildCard, emptyText, truncated) {
		const sec = el('div', 'section');
		const head = el('div', 'section-head');
		head.appendChild(el('span', 'section-title', titleText));
		head.appendChild(el('span', 'count brand-mono', error ? '!' : String(count)));
		sec.appendChild(head);

		if (error) {
			sec.appendChild(note(error, true));
			return sec;
		}
		if (items.length === 0) {
			sec.appendChild(emptyState(emptyText));
			return sec;
		}
		for (const it of items) {
			sec.appendChild(buildCard(it));
		}
		if (truncated) {
			sec.appendChild(note('More items — increase the limit to see the rest.'));
		}
		return sec;
	}

	// Branded onboarding shown when there's no reachable server — replaces the
	// two bands so a cold install sees guidance, not raw HTTP errors.
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
					? 'Engraphy isn’t pointed at a memory server yet.'
					: 'Can’t reach the Engraphy server at your configured URL. If you just installed, you probably need to start one first.'
			)
		);
		const actions = el('div', 'actions onboarding-actions');
		actions.appendChild(
			button('btn btn-approve', 'Set up Engraphy', () => action({ type: 'openWalkthrough' }))
		);
		actions.appendChild(
			button('btn btn-secondary', 'Paste URL + token', () => action({ type: 'configureServer' }))
		);
		wrap.appendChild(actions);
		wrap.appendChild(
			el(
				'p',
				'onboarding-note',
				'Hosted Engraphy is coming soon — run locally with Docker or connect your own server.'
			)
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

	function render(state) {
		setBusy(false);
		const root = els.root;
		root.textContent = '';

		if (state.loading) {
			const n = note('Loading…');
			n.prepend(el('span', 'spinner'));
			root.appendChild(n);
			return;
		}

		if (state.connection && state.connection.kind === 'no-server') {
			root.appendChild(noServerBlock(state.connection));
			return;
		}

		root.appendChild(
			section(
				'Pending duplicates',
				state.pending.length,
				state.pendingError,
				state.pending,
				pendingCard,
				'No pending duplicates. Writes that collide will land here for you to confirm.',
				false
			)
		);
		root.appendChild(
			section(
				'Inbox',
				state.inbox.length,
				state.inboxError,
				state.inbox,
				inboxCard,
				'Inbox is empty. Captured items awaiting triage will appear here.',
				state.inboxTruncated
			)
		);
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
			// CSS already keys off the vscode-* body class; expose the kind for
			// any JS that needs it and to confirm live theme reactions.
			document.body.dataset.themeKind = msg.kind;
		}
	});

	// Tell the host we're ready for the first state push.
	post({ type: 'ready' });
})();
