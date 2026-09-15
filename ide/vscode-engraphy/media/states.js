// Shared webview state components: loading skeletons, empty states, per-band
// error blocks, and the connection recovery block.
//
// WHY THIS EXISTS: both panels need the same four shapes (loading / empty /
// error / disconnected) and neither should ever show a blank pane or a raw HTTP
// string. Before this, confirm.js and stats.js each carried their own copy of a
// "No server connected" block, neither had a skeleton, and neither could tell
// "the server is not running" from "the server rejected your token" because the
// host collapsed both into one state.
//
// Ported from the Engraphy desktop app's renderer/views/states.js, adapted to
// the VS Code webview (postMessage instead of an IPC bridge, no window.ENGRAPHY
// global, VS Code command names in the remedy copy).
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

	/**
	 * A masked brand mark. The mark is an SVG painted as a CSS mask so its color
	 * follows the theme accent rather than being baked into the file.
	 *
	 * The URI is passed in by the caller (both panels read it off a data-attr the
	 * host filled with asWebviewUri). If it is missing the element still lays out,
	 * it just paints nothing, which is the same failure the malformed-SVG bug
	 * produced silently. scripts/test-client.js guards the SVG itself.
	 */
	function markEl(markUri, cls) {
		const mark = el('div', cls || 'mark mark-lg');
		if (markUri) {
			mark.style.webkitMaskImage = 'url("' + markUri + '")';
			mark.style.maskImage = 'url("' + markUri + '")';
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
	 *   { kind, tone, markUri, mark, title, message,
	 *     actions:[{label,kind,onClick,title}], detail, note }
	 * `tone` is 'neutral' | 'error' | 'warn'. `detail` renders behind a closed
	 * disclosure so the raw transport text is available but never shouted.
	 */
	function stateBlock(opts) {
		const o = opts || {};
		const wrap = el('div', 'state-block' + (o.tone && o.tone !== 'neutral' ? ' tone-' + o.tone : ''));
		// Tagged so a test or a screenshot harness can assert WHICH state rendered,
		// rather than guessing from class names that empty states also use.
		wrap.dataset.stateBlock = o.kind || 'generic';

		if (o.mark !== false) {
			wrap.appendChild(markEl(o.markUri, 'mark mark-lg'));
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
	 * Map a connection state onto recovery copy plus the actions that actually fix
	 * it. The three reasons need three different remedies, which is the entire
	 * point of splitting `unauthorized` out of `unreachable`: telling someone
	 * whose server is up and healthy to go start a server sends them down the
	 * wrong path, and that is what 0.4.0 did.
	 *
	 * `handlers`: { onSetup, onConfigure, onRetry, onReconnect }
	 */
	function recoveryBlock(conn, handlers, opts) {
		const h = handlers || {};
		const o = opts || {};
		const reason = (conn && conn.reason) || 'unconfigured';
		const what = o.what || 'your memories';
		const markUri = o.markUri;

		if (reason === 'unauthorized') {
			// Two different situations wear the same 401. Someone who never entered
			// a token should be told to add one, not that theirs was rejected.
			const noToken = conn.hasToken === false;
			return tag(
				stateBlock({
					kind: 'unauthorized',
					tone: 'warn',
					markUri: markUri,
					title: noToken ? 'This server needs a token' : 'Your token was rejected',
					message:
						(conn.summary || 'The server did not accept this token.') +
						(noToken
							? ' Add the token you were given and it will start reading.'
							: ' The server is running, so this is a credentials problem, not a connection one.'),
					actions: [
						{
							label: noToken ? 'Add a token' : 'Update token',
							kind: 'primary',
							onClick: h.onConfigure,
						},
						{ label: 'Retry', kind: 'ghost', onClick: h.onRetry },
					],
					note:
						'Mint a token with "engraphy-admin token create --space <space> --principal <you> ' +
						'--role readwrite". A token is bound to one space, so a token for another space ' +
						'fails the same way.',
					detail: conn.detail,
				}),
				reason
			);
		}

		if (reason === 'unreachable') {
			return tag(
				stateBlock({
					kind: 'unreachable',
					tone: 'error',
					markUri: markUri,
					title: 'Cannot reach your server',
					message:
						conn.summary || 'The Engraphy server at your configured URL did not answer.',
					actions: [
						{ label: 'Retry', kind: 'primary', onClick: h.onRetry },
						{ label: 'Reconnect', kind: 'secondary', onClick: h.onReconnect },
						{ label: 'Change URL', kind: 'ghost', onClick: h.onConfigure },
					],
					note:
						'Running Engraphy locally with Docker? Check that "docker compose ps" shows it as ' +
						'healthy. First boot downloads the embedding model before it serves anything.',
					detail: conn.detail,
				}),
				reason
			);
		}

		// unconfigured
		return tag(
			stateBlock({
				kind: 'unconfigured',
				tone: 'neutral',
				markUri: markUri,
				title: 'No server connected',
				message: 'Engraphy is not pointed at a memory server yet. Connect one to see ' + what + '.',
				actions: [
					{ label: 'Set up Engraphy', kind: 'primary', onClick: h.onSetup },
					{ label: 'Paste URL + token', kind: 'secondary', onClick: h.onConfigure },
				],
				note:
					'Hosted Engraphy is not available yet. Run it locally with Docker, or connect a ' +
					'server you already run.',
			}),
			reason
		);
	}

	/** Mark a block as the panel-level connection recovery UI. */
	function tag(node, reason) {
		node.dataset.recovery = reason;
		return node;
	}

	/**
	 * A per-band failure that is NOT a whole-panel connection problem (a tool
	 * error, or one band failing while the other works). Rendered inline so the
	 * rest of the panel keeps working.
	 */
	function errorBlock(summary, onRetry, detail) {
		const wrap = el('div', 'band-error');
		const head = el('div', 'band-error-head');
		head.appendChild(el('span', 'band-error-icon', '!'));
		head.appendChild(el('span', 'band-error-text', summary || 'Something went wrong.'));
		wrap.appendChild(head);
		if (onRetry) {
			const row = el('div', 'actions');
			const b = button('btn btn-ghost', 'Retry', onRetry);
			b.dataset.keepEnabled = '1';
			row.appendChild(b);
			wrap.appendChild(row);
		}
		if (detail && detail !== summary) {
			const det = document.createElement('details');
			det.appendChild(el('summary', 'cand-label', 'Technical detail'));
			const pre = el('pre', 'payload');
			pre.textContent = detail;
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
