// Shared renderer state components: loading skeletons, empty states, per-band
// error blocks, and the connection recovery block.
//
// WHY THIS EXISTS: every panel needs the same four shapes (loading / empty /
// error / disconnected) and the app must never show a blank screen or a raw
// HTTP string. Before this, the confirm and stats panels each carried their own
// copy of a "No server connected" block, the explorer had neither, and none of
// them could tell "the server is not running" from "the server rejected your
// token" because the host collapsed both into one state.
//
// Every string that came from the server goes through textContent. Nothing here
// touches innerHTML.
(function () {
	'use strict';

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

	function markEl(cls) {
		const mark = el('div', cls || 'mark mark-lg');
		const uri = (window.ENGRAPHY && window.ENGRAPHY.loopMarkUri) || '';
		if (uri) {
			mark.style.webkitMaskImage = 'url("' + uri + '")';
			mark.style.maskImage = 'url("' + uri + '")';
		}
		return mark;
	}

	/** An inline "working…" line with a spinner. */
	function spinnerNote(text) {
		const n = el('div', 'note');
		n.appendChild(el('span', 'spinner'));
		n.appendChild(el('span', null, ' ' + (text || 'Loading…')));
		return n;
	}

	/**
	 * Shimmer placeholders shaped like the cards that are coming. Used instead of
	 * a bare "Loading…" line on the first paint, so the panel never flashes empty
	 * and then fills.
	 */
	function skeleton(count, variant) {
		const wrap = el('div', 'skeleton-wrap');
		for (let i = 0; i < (count || 3); i++) {
			const card = el('div', 'skeleton-card' + (variant ? ' skeleton-' + variant : ''));
			card.appendChild(el('div', 'skeleton-line skeleton-line-lg'));
			card.appendChild(el('div', 'skeleton-line'));
			card.appendChild(el('div', 'skeleton-line skeleton-line-sm'));
			wrap.appendChild(card);
		}
		wrap.setAttribute('aria-label', 'Loading');
		wrap.setAttribute('role', 'status');
		return wrap;
	}

	/**
	 * The one visual used for empty / error / disconnected. `opts`:
	 *   { tone, mark, title, message, actions:[{label,kind,onClick,title}],
	 *     detail, note }
	 * `tone` is 'neutral' | 'error' | 'warn'. `detail` renders behind a closed
	 * disclosure so the raw transport/tool text is available but never shouted.
	 */
	function stateBlock(opts) {
		const o = opts || {};
		const wrap = el('div', 'state-block' + (o.tone && o.tone !== 'neutral' ? ' tone-' + o.tone : ''));
		// Tagged so the smoke harness can assert on WHICH state rendered, rather
		// than guessing from class names that empty states also use.
		wrap.dataset.stateBlock = o.kind || 'generic';

		if (o.mark !== false) {
			wrap.appendChild(markEl('mark mark-lg'));
		}
		if (o.title) {
			wrap.appendChild(el('h2', 'state-title brand-mono', o.title));
		}
		if (o.message) {
			wrap.appendChild(el('p', 'state-msg', o.message));
		}

		const actions = (o.actions || []).filter(Boolean);
		if (actions.length) {
			const row = el('div', 'actions state-actions');
			for (const a of actions) {
				const cls =
					a.kind === 'primary'
						? 'btn btn-approve'
						: a.kind === 'ghost'
							? 'btn btn-ghost'
							: 'btn btn-secondary';
				const b = button(cls, a.label, a.onClick, a.title);
				// Recovery actions are how the user escapes a stuck panel, so the
				// panel's busy lock must not disable them.
				b.dataset.keepEnabled = '1';
				row.appendChild(b);
			}
			wrap.appendChild(row);
		}

		if (o.note) {
			wrap.appendChild(el('p', 'state-note', o.note));
		}
		if (o.detail) {
			const det = document.createElement('details');
			det.appendChild(el('summary', 'cand-label', 'Technical detail'));
			const pre = el('pre', 'payload');
			pre.textContent = o.detail;
			det.appendChild(pre);
			wrap.appendChild(det);
		}
		return wrap;
	}

	/**
	 * Map a connection state onto recovery copy + the actions that actually fix
	 * it. The three kinds need three different remedies, which is the entire
	 * point of splitting `unauthorized` out of `unreachable`: telling someone
	 * whose server is up and healthy to "start a server" sends them down the
	 * wrong path.
	 *
	 * `handlers`: { onSetup, onSettings, onRetry, onReconnect }
	 */
	function recoveryBlock(conn, handlers, opts) {
		const h = handlers || {};
		const o = opts || {};
		const kind = (conn && conn.kind) || 'unconfigured';
		const what = o.what || 'your memories';

		if (kind === 'unauthorized') {
			// Two different situations wear the same 401. Someone who never entered
			// a token should be told to add one, not that theirs was rejected.
			const noToken = conn.hasToken === false;
			return tag(stateBlock({
				tone: 'warn',
				title: noToken ? 'This server needs a token' : 'Your token was rejected',
				message:
					(conn.summary || 'The server did not accept this token.') +
					(noToken
						? ' Add the token you were given, and it will start reading.'
						: ' The server is running, so this is a credentials problem, not a connection one.'),
				actions: [
					{ label: noToken ? 'Add a token' : 'Open Settings', kind: 'primary', onClick: h.onSettings },
					{ label: 'Retry', kind: 'ghost', onClick: h.onRetry },
				],
				note:
					'Mint a token with "engraphy-admin token create --space <space> --principal <you> --role readwrite". ' +
					'A token is bound to one space, so a token for another space fails the same way.',
				detail: conn.detail,
			}), kind);
		}

		if (kind === 'unreachable') {
			return tag(stateBlock({
				tone: 'error',
				title: 'Cannot reach your server',
				message: conn.summary || 'The Engraphy server at your configured URL did not answer.',
				actions: [
					{ label: 'Retry', kind: 'primary', onClick: h.onRetry },
					{ label: 'Reconnect', kind: 'secondary', onClick: h.onReconnect },
					{ label: 'Open Settings', kind: 'ghost', onClick: h.onSettings },
				],
				note:
					'Running Engraphy locally with Docker? Check "docker compose ps" shows engraphy as healthy. ' +
					'First boot downloads the embedding model before it serves anything.',
				detail: conn.detail,
			}), kind);
		}

		// unconfigured
		return tag(stateBlock({
			tone: 'neutral',
			title: 'Connect your memory server',
			message:
				'Engraphy is not pointed at an Engraphy server yet. Connect one to see ' + what + '.',
			actions: [
				{ label: 'Set up Engraphy', kind: 'primary', onClick: h.onSetup },
				{ label: 'Paste URL + token', kind: 'secondary', onClick: h.onSettings },
			],
			note: 'Hosted Engraphy is not available yet. Run Engraphy locally with Docker, or connect a server you already run.',
		}), kind);
	}

	/** Mark a block as the panel-level connection recovery UI. */
	function tag(node, kind) {
		node.dataset.recovery = kind;
		return node;
	}

	/**
	 * A per-band failure that is NOT a whole-panel connection problem (a tool
	 * error, or one band failing while the other works). Rendered inline so the
	 * rest of the panel keeps working.
	 */
	function errorBlock(err, onRetry) {
		const wrap = el('div', 'band-error');
		const head = el('div', 'band-error-head');
		head.appendChild(el('span', 'band-error-icon', '!'));
		head.appendChild(el('span', 'band-error-text', (err && err.summary) || 'Something went wrong.'));
		wrap.appendChild(head);
		if (onRetry) {
			const row = el('div', 'actions');
			const b = button('btn btn-ghost', 'Retry', onRetry);
			b.dataset.keepEnabled = '1';
			row.appendChild(b);
			wrap.appendChild(row);
		}
		if (err && err.detail && err.detail !== err.summary) {
			const det = document.createElement('details');
			det.appendChild(el('summary', 'cand-label', 'Technical detail'));
			const pre = el('pre', 'payload');
			pre.textContent = err.detail;
			det.appendChild(pre);
			wrap.appendChild(det);
		}
		return wrap;
	}

	/** A quiet empty state for a band or list that legitimately has nothing in it. */
	function emptyState(text, sub) {
		const wrap = el('div', 'empty');
		wrap.appendChild(el('div', 'rings'));
		wrap.appendChild(el('div', null, text));
		if (sub) {
			wrap.appendChild(el('div', 'empty-sub', sub));
		}
		return wrap;
	}

	window.ENGRAPHY_STATES = {
		el: el,
		button: button,
		markEl: markEl,
		spinnerNote: spinnerNote,
		skeleton: skeleton,
		stateBlock: stateBlock,
		recoveryBlock: recoveryBlock,
		errorBlock: errorBlock,
		emptyState: emptyState,
	};
})();
