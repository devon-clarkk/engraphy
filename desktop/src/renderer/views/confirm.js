// Engraphy confirm-write queue — view script.
//
// PORTED from the VS Code extension's media/confirm.js (v0.4.0). The rendering
// logic below (pendingCard / inboxCard / section / noServerBlock / render) is
// verbatim; only the shell wiring changed for the desktop app:
//   • the top-level IIFE became window.mountConfirm(ctx), mounted by app.js
//   • acquireVsCodeApi() → ctx.host (the preload IPC bridge)
//   • busy state is scoped to this panel, not document.body / every button
//   • the loop-mark URI comes from window.ENGRAPHY, not an asWebviewUri data-attr
//   • the Promote button opens an in-app modal (main has no native input boxes)
//   • window 'message' listener → ctx.host.onMessage (channel-scoped, no crosstalk)
//
// DESKTOP DIVERGENCE (beyond the shell wiring, see DECISIONS.md §12): the
// panel's not-connected handling is no longer the extension's single
// "No server connected" block. The host now sends a three-way connection state
// and CLASSIFIED band errors ({class, summary, detail}) instead of raw strings,
// so this renders distinct recovery copy for unconfigured / unreachable /
// unauthorized, an inline retryable error per band, and a skeleton on first
// load. All of it comes from the shared window.ENGRAPHY_STATES helpers.
//
// Still runs with no Node and injects no server strings as HTML: every server
// value goes through textContent. The host owns all MCP calls.
window.mountConfirm = function (ctx) {
	'use strict';

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
			// Recovery actions (Retry / Open Settings) stay live while the panel is
			// busy: they are how the user gets OUT of a stuck state, so disabling
			// them is exactly backwards.
			if (b.dataset.keepEnabled) {
				continue;
			}
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

	function button(cls, label, onClick, title, action) {
		const b = el('button', cls, label);
		if (title) {
			b.title = title;
		}
		// A stable hook for the smoke harness. Selecting these by CSS class alone
		// is ambiguous (Approve and Promote share btn-approve) and selecting by
		// label text breaks the moment the copy changes.
		if (action) {
			b.dataset.action = action;
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
						p.expired ? 'This row is expired — the server will refuse the merge.' : 'Merge into ' + c.title,
						'merge'
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
				p.expired ? 'This row is expired — the server will refuse the approve.' : 'Keep as a new, distinct node',
				'approve'
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
			button('btn btn-approve', '✓ Promote…', () => openPromote(it.id), 'Author a node from this item', 'promote')
		);
		actions.appendChild(
			button('btn btn-ghost', 'Discard', () => action({ type: 'discard', inboxId: it.id }), 'Drop this inbox item', 'discard')
		);
		card.appendChild(actions);

		if (it.createdAt) {
			card.appendChild(el('div', 'meta', 'captured ' + it.createdAt));
		}
		return card;
	}

	// ---- section renderers --------------------------------------------------

	function section(titleText, count, error, items, buildCard, empty, truncated) {
		const sec = el('div', 'section');
		const head = el('div', 'section-head');
		head.appendChild(el('span', 'section-title', titleText));
		head.appendChild(el('span', 'count brand-mono', error ? '!' : String(count)));
		sec.appendChild(head);

		if (error) {
			// A classified, retryable failure for THIS band only. The panel-wide
			// connection block already handled the case where everything is down.
			sec.appendChild(S.errorBlock(error, () => action({ type: 'refresh' })));
			return sec;
		}
		if (items.length === 0) {
			sec.appendChild(S.emptyState(empty.text, empty.sub));
			return sec;
		}
		for (const it of items) {
			sec.appendChild(buildCard(it));
		}
		if (truncated) {
			sec.appendChild(note('More items than fit in one page. Only the first 50 are shown.'));
		}
		return sec;
	}

	// Recovery block for a panel that cannot talk to a server. The host decides
	// WHICH of the three problems it is; the shared helper owns the copy and the
	// remedy buttons per kind.
	function recovery(conn) {
		return S.recoveryBlock(
			conn,
			{
				onSetup: () => post({ type: 'openWalkthrough' }),
				onSettings: () => post({ type: 'configureServer' }),
				onRetry: () => post({ type: 'refresh' }),
				onReconnect: () => post({ type: 'reconnect' }),
			},
			{ what: 'the confirm-write queue' }
		);
	}

	function render(state) {
		const root = els.root;
		busy = false;
		els.panel.classList.toggle('busy', !!state.loading);
		root.textContent = '';

		if (state.connection && state.connection.kind !== 'ok') {
			root.appendChild(recovery(state.connection));
			// Deliberately NOT setBusy: the recovery buttons must stay clickable.
			for (const b of els.panel.querySelectorAll('button')) {
				b.disabled = false;
			}
			return;
		}

		// First paint while the initial reads are in flight: show the shape of what
		// is coming rather than an empty panel that fills a beat later.
		if (state.loading && !state.loaded) {
			root.appendChild(S.skeleton(2));
			els.refresh.disabled = true;
			return;
		}

		root.appendChild(
			section(
				'Pending duplicates',
				state.pending.length,
				state.pendingError,
				state.pending,
				pendingCard,
				{
					text: 'No pending duplicates.',
					sub: 'When an agent writes something that looks like an existing memory, it lands here for you to approve or merge.',
				},
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
				{
					text: 'Inbox is empty.',
					sub: 'Captured items awaiting triage appear here. Promote one to author a memory from it, or discard it.',
				},
				state.inboxTruncated
			)
		);

		// A refresh over already-rendered content dims and locks the actions
		// rather than throwing the content away.
		setBusy(!!state.loading);
	}

	// ---- promote modal (desktop replacement for the native input flow) ------

	// The extension authored a promote via a chain of native input boxes. Here
	// main returns the node-type list + prefilled title/body via a promotePrepare
	// invoke, we collect them in an in-app modal, then post promoteSubmit. All
	// fields stay editable (promotion is authoring, not a verbatim replay).
	async function openPromote(inboxId) {
		if (busy) {
			return;
		}
		let prep;
		try {
			prep = await ctx.host.invoke({ type: 'promotePrepare', inboxId });
		} catch (e) {
			prep = { ok: false, error: { summary: String((e && e.message) || e) } };
		}
		// The host RESOLVES failures rather than rejecting, so a silent `return`
		// here used to make the Promote button look dead. Say what went wrong.
		if (!prep || !prep.ok) {
			const msg = (prep && prep.error && prep.error.summary) || 'Could not prepare this item.';
			if (window.ENGRAPHY && window.ENGRAPHY.toast) {
				window.ENGRAPHY.toast(msg, 'error');
			}
			return;
		}
		showPromoteModal(inboxId, prep);
	}

	function showPromoteModal(inboxId, prep) {
		const host = document.getElementById('modal-host');
		host.textContent = '';

		const backdrop = el('div', 'modal-backdrop');
		const modal = el('div', 'modal');
		modal.appendChild(el('h2', null, 'Promote to a memory'));
		modal.appendChild(
			el('p', 'modal-sub', 'Author a node from this captured item. Everything is editable before it is written.')
		);

		// node type (dropdown + Other…)
		const typeField = field('Node type');
		const typeSelect = document.createElement('select');
		typeSelect.dataset.field = 'type';
		for (const nt of prep.nodeTypes || []) {
			const o = document.createElement('option');
			o.value = nt.type;
			o.textContent = nt.type;
			o.title = nt.description || '';
			typeSelect.appendChild(o);
		}
		const otherOpt = document.createElement('option');
		otherOpt.value = '__other__';
		otherOpt.textContent = 'Other…';
		typeSelect.appendChild(otherOpt);
		const typeOther = document.createElement('input');
		typeOther.dataset.field = 'typeOther';
		typeOther.placeholder = 'Custom node type';
		typeOther.style.display = 'none';
		typeOther.style.marginTop = '6px';
		typeSelect.addEventListener('change', () => {
			typeOther.style.display = typeSelect.value === '__other__' ? 'block' : 'none';
		});
		typeField.appendChild(typeSelect);
		typeField.appendChild(typeOther);
		modal.appendChild(typeField);

		// scope (fixed when the item carries one; otherwise a chooser)
		let scopeGetter;
		if (prep.needsScope) {
			const scopeField = field('Scope');
			const scopeSelect = document.createElement('select');
			scopeSelect.dataset.field = 'scope';
			for (const s of prep.scopes || []) {
				const o = document.createElement('option');
				o.value = s;
				o.textContent = s;
				scopeSelect.appendChild(o);
			}
			const so = document.createElement('option');
			so.value = '__other__';
			so.textContent = 'Other…';
			scopeSelect.appendChild(so);
			const scopeOther = document.createElement('input');
			scopeOther.dataset.field = 'scopeOther';
			scopeOther.placeholder = 'Scope id';
			scopeOther.style.display = (prep.scopes || []).length ? 'none' : 'block';
			scopeOther.style.marginTop = '6px';
			if (!(prep.scopes || []).length) {
				scopeSelect.value = '__other__';
			}
			scopeSelect.addEventListener('change', () => {
				scopeOther.style.display = scopeSelect.value === '__other__' ? 'block' : 'none';
			});
			scopeField.appendChild(scopeSelect);
			scopeField.appendChild(scopeOther);
			modal.appendChild(scopeField);
			scopeGetter = () => (scopeSelect.value === '__other__' ? scopeOther.value.trim() : scopeSelect.value);
		} else {
			const scopeField = field('Scope');
			const fixed = el('div', 'hint', prep.scope + '  (from the captured item)');
			scopeField.appendChild(fixed);
			modal.appendChild(scopeField);
			scopeGetter = () => prep.scope;
		}

		// title + body (prefilled, editable)
		const titleField = field('Title');
		const titleInput = document.createElement('input');
		titleInput.dataset.field = 'title';
		titleInput.value = (prep.defaults && prep.defaults.title) || '';
		titleField.appendChild(titleInput);
		modal.appendChild(titleField);

		const bodyField = field('Body');
		const bodyInput = document.createElement('textarea');
		bodyInput.dataset.field = 'body';
		bodyInput.rows = 5;
		bodyInput.value = (prep.defaults && prep.defaults.body) || '';
		bodyField.appendChild(bodyInput);
		modal.appendChild(bodyField);

		// actions
		const acts = el('div', 'form-actions');
		const problem = el('span', 'form-status bad');
		const submit = button('btn btn-approve', 'Promote', () => {
			const type = typeSelect.value === '__other__' ? typeOther.value.trim() : typeSelect.value;
			const scope = scopeGetter();
			const title = titleInput.value.trim();
			const body = bodyInput.value;
			// Say WHICH field is missing. Silently doing nothing reads as a broken
			// button.
			if (!type) {
				problem.textContent = 'Pick a node type.';
				return;
			}
			if (!scope) {
				problem.textContent = 'Pick or type a scope.';
				return;
			}
			if (!title) {
				problem.textContent = 'Give it a title.';
				titleInput.focus();
				return;
			}
			closeModal();
			setBusy(true);
			// nodeType, NOT type: a shorthand `type` here would overwrite the
			// message's own `type: 'promoteSubmit'` discriminator (object literals
			// take the last value), the host's switch would never match, and
			// Promote would silently do nothing.
			post({ type: 'promoteSubmit', inboxId, nodeType: type, scope, title, body });
		});
		const cancel = button('btn btn-ghost', 'Cancel', () => closeModal(), null, 'promote-cancel');
		submit.dataset.action = 'promote-submit';
		acts.appendChild(submit);
		acts.appendChild(cancel);
		acts.appendChild(problem);
		modal.appendChild(acts);

		function closeModal() {
			document.removeEventListener('keydown', onKey);
			host.textContent = '';
		}
		function onKey(e) {
			if (e.key === 'Escape') {
				closeModal();
			}
		}
		document.addEventListener('keydown', onKey);
		backdrop.addEventListener('click', (e) => {
			if (e.target === backdrop) closeModal();
		});
		modal.setAttribute('role', 'dialog');
		modal.setAttribute('aria-modal', 'true');
		backdrop.appendChild(modal);
		host.appendChild(backdrop);
		titleInput.focus();
	}

	function field(labelText) {
		const f = el('div', 'form-field');
		f.appendChild(el('label', null, labelText));
		return f;
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

	// Tell the host we're ready for the first state push.
	post({ type: 'ready' });
};
