// Engraphy memory explorer — desktop view (built fresh).
//
// The extension's explorer was a native VS Code TreeView (explorerView.ts):
// search → result rows → expand a row to traverse depth-1 to its neighbours,
// click to open the node's JSON. This rebuilds that as an in-app HTML view over
// the same MCP calls, which main exposes as request/response invokes:
//   search {scope, query} → {ok, nodes}   traverse {id} → {ok, nodes}
//   get {id} → {ok, node, missing}        scopes → {ok, scopes}
//
// EVERY invoke result is checked for `ok` before its payload is read. The host
// RESOLVES failures ({ok:false, error}) rather than rejecting, so the previous
// `try/catch` here never fired: a dead server and a rejected token both fell
// through to `res.nodes || []` and rendered "No results for ...", which is a
// lie. Same trap applied to the detail pane (it pretty-printed the failure
// object) and to the scope dropdown (it silently stayed empty).
//
// All server strings go through textContent, never innerHTML.
window.initExplorer = function (ctx) {
	'use strict';
	const host = ctx.host;
	const root = ctx.root;
	const S = window.ENGRAPHY_STATES;
	const el = S.el;

	// ---- search bar --------------------------------------------------------
	const bar = el('div', 'explorer-search');
	const input = document.createElement('input');
	input.type = 'search';
	input.placeholder = 'Search memory…';
	input.setAttribute('aria-label', 'Search memory');
	const scopeSel = document.createElement('select');
	scopeSel.setAttribute('aria-label', 'Scope');
	addScopeOption('all');
	const searchBtn = el('button', 'btn btn-approve', 'Search');
	const clearBtn = el('button', 'btn btn-ghost', 'Clear');
	clearBtn.title = 'Clear the search and start over';
	bar.appendChild(input);
	bar.appendChild(scopeSel);
	bar.appendChild(searchBtn);
	bar.appendChild(clearBtn);
	root.appendChild(bar);

	const list = el('div', 'explorer-list');
	root.appendChild(list);

	/** Remember the last query so Retry re-runs the real thing. */
	let lastQuery = null;

	function addScopeOption(v) {
		const o = document.createElement('option');
		o.value = v;
		o.textContent = v;
		scopeSel.appendChild(o);
	}

	function show(node) {
		list.textContent = '';
		list.appendChild(node);
	}

	/** Route a failed invoke to the right block: connection states get remedies. */
	function showFailure(error, retry) {
		const cls = (error && error.class) || 'tool';
		if (cls === 'config') {
			show(
				S.recoveryBlock({ kind: 'unconfigured' }, handlers(retry), { what: 'your memories' })
			);
			return;
		}
		if (cls === 'auth') {
			show(
				S.recoveryBlock(
					{ kind: 'unauthorized', summary: error.summary, detail: error.detail },
					handlers(retry),
					{ what: 'your memories' }
				)
			);
			return;
		}
		if (cls === 'transport') {
			show(
				S.recoveryBlock(
					{ kind: 'unreachable', summary: error.summary, detail: error.detail },
					handlers(retry),
					{ what: 'your memories' }
				)
			);
			return;
		}
		// A genuine tool error (bad scope, bad argument): not a connection problem.
		show(
			S.stateBlock({
				tone: 'error',
				title: 'That search failed',
				message: (error && error.summary) || 'The server rejected the request.',
				actions: retry ? [{ label: 'Retry', kind: 'primary', onClick: retry }] : [],
				detail: error && error.detail,
			})
		);
	}

	function handlers(retry) {
		return {
			onSetup: () => window.ENGRAPHY.showOnboarding(),
			onSettings: () => window.ENGRAPHY.navigate('settings'),
			onRetry: retry || (() => doSearch()),
			onReconnect: () => window.ENGRAPHY.reconnect(),
		};
	}

	// The resting state, only ever shown after an explicit Clear.
	function showIdle() {
		show(
			S.stateBlock({
				title: 'Search your memory graph',
				message:
					'Look up people, preferences, commitments and notes your agents have stored. ' +
					'Open a result to see its full record, or expand it to walk its links.',
				actions: [
					{
						label: 'Show everything',
						kind: 'primary',
						onClick: () => {
							input.value = '';
							doSearch();
						},
						title: 'Run an empty search, which lists what is readable by your token',
					},
				],
				note: 'Tip: press Enter in the search box, or use the scope dropdown to narrow to one space.',
			})
		);
	}

	// Populate scopes. A failure here is NOT fatal (search still works with
	// "all"), but it must not be invisible either, so it degrades to a titled
	// dropdown rather than a silently empty one.
	host
		.invoke({ type: 'scopes' })
		.then((res) => {
			if (!res || !res.ok) {
				scopeSel.title =
					'Could not load the scope list: ' +
					((res && res.error && res.error.summary) || 'unknown error') +
					'. Searching "all" still works.';
				return;
			}
			for (const s of res.scopes || []) {
				if (s && s !== 'all') {
					addScopeOption(s);
				}
			}
			scopeSel.title = 'Limit the search to one scope';
		})
		.catch(() => {
			scopeSel.title = 'Could not load the scope list. Searching "all" still works.';
		});

	let busy = false;
	/** True when the last attempt ended in a failure block, so a retry is useful. */
	let lastFailed = false;
	/** True once the user has run a search of their own. */
	let userSearched = false;

	/** List everything the token can read, with no query. */
	function browse() {
		input.value = '';
		return doSearch();
	}

	async function doSearch() {
		if (busy) {
			return;
		}
		const query = input.value.trim();
		const scope = scopeSel.value;
		lastQuery = { query: query, scope: scope };
		busy = true;
		searchBtn.disabled = true;
		show(S.skeleton(3, 'row'));
		try {
			const res = await host.invoke({ type: 'search', scope: scope, query: query });
			if (!res || !res.ok) {
				lastFailed = true;
				showFailure(res && res.error, () => doSearch());
				return;
			}
			lastFailed = false;
			renderResults(res.nodes || [], query);
		} catch (e) {
			lastFailed = true;
			showFailure({ class: 'tool', summary: String((e && e.message) || e) }, () => doSearch());
		} finally {
			busy = false;
			searchBtn.disabled = false;
		}
	}
	searchBtn.addEventListener('click', () => {
		userSearched = true;
		doSearch();
	});
	clearBtn.addEventListener('click', () => {
		input.value = '';
		lastQuery = null;
		showIdle();
		input.focus();
	});
	input.addEventListener('keydown', (e) => {
		if (e.key === 'Enter') {
			userSearched = true;
			doSearch();
		}
	});

	function renderResults(nodes, query) {
		list.textContent = '';
		if (!nodes.length) {
			// A genuine zero-result search: the server answered, there is just
			// nothing matching. Distinct from every failure path above.
			show(
				S.stateBlock({
					title: query ? 'No matches' : 'No memories yet',
					message: query
						? 'Nothing in the readable graph matches "' + query + '".'
						: 'You are connected, and this space is empty. Memories appear here as soon as ' +
							'something writes one.',
					actions: [
						{
							label: query ? 'Show everything' : 'Check again',
							kind: 'primary',
							onClick: () => {
								scopeSel.value = 'all';
								browse();
							},
						},
					],
					note: query
						? 'A token is bound to one space, so results are limited to what it may read.'
						: 'To add the first one, have an agent write through the Engraphy MCP tools, or use ' +
							'"engraphy-admin". The Confirm-write queue is where writes that look like an ' +
							'existing memory land for you to approve.',
				})
			);
			return;
		}
		const count = el('div', 'result-count', nodes.length + (nodes.length === 1 ? ' result' : ' results'));
		list.appendChild(count);
		for (const n of nodes) {
			list.appendChild(nodeRow(n));
		}
	}

	// A result row: click the row body to open the detail card (get); click the
	// chevron to expand depth-1 neighbours (traverse).
	function nodeRow(node) {
		const wrap = el('div');
		const row = el('div', 'node-row');
		row.appendChild(el('span', 'node-dot'));
		const main = el('div', 'node-main');
		main.appendChild(el('div', 'node-title', node.title));
		main.appendChild(el('div', 'node-meta', node.type + ' · ' + node.scope + ' · ' + node.id));
		row.appendChild(main);
		const chev = el('span', 'node-chev', '⌄ links');
		chev.setAttribute('role', 'button');
		chev.setAttribute('tabindex', '0');
		row.appendChild(chev);
		wrap.appendChild(row);

		const detailHost = el('div');
		const childHost = el('div', 'node-children');
		wrap.appendChild(detailHost);
		wrap.appendChild(childHost);

		let detailOpen = false;
		main.setAttribute('role', 'button');
		main.setAttribute('tabindex', '0');
		async function toggleDetail() {
			detailOpen = !detailOpen;
			detailHost.textContent = '';
			row.classList.toggle('open', detailOpen);
			if (!detailOpen) {
				return;
			}
			const box = el('div', 'node-detail');
			box.appendChild(S.spinnerNote('Loading record…'));
			detailHost.appendChild(box);
			try {
				const res = await host.invoke({ type: 'get', id: node.id });
				box.textContent = '';
				if (!res || !res.ok) {
					box.appendChild(S.errorBlock(res && res.error, toggleAgain));
					return;
				}
				if (!res.node) {
					// Engraphy collapses "unknown id" and "not readable by your token"
					// on purpose, so say both.
					box.appendChild(
						el(
							'div',
							'note',
							'That record is not available. It was either deleted, or your token cannot read it.'
						)
					);
					return;
				}
				box.appendChild(detailCard(res.node));
			} catch (e) {
				box.textContent = '';
				box.appendChild(
					S.errorBlock({ summary: 'Could not load the record.', detail: String((e && e.message) || e) }, toggleAgain)
				);
			}
		}
		function toggleAgain() {
			detailOpen = false;
			toggleDetail();
		}
		main.addEventListener('click', toggleDetail);
		main.addEventListener('keydown', (e) => {
			if (e.key === 'Enter' || e.key === ' ') {
				e.preventDefault();
				toggleDetail();
			}
		});

		let childOpen = false;
		async function toggleChildren(e) {
			if (e) {
				e.stopPropagation();
			}
			childOpen = !childOpen;
			childHost.textContent = '';
			chev.textContent = childOpen ? '⌃ links' : '⌄ links';
			if (!childOpen) {
				return;
			}
			childHost.appendChild(S.spinnerNote('Following links…'));
			try {
				const res = await host.invoke({ type: 'traverse', id: node.id });
				childHost.textContent = '';
				if (!res || !res.ok) {
					childHost.appendChild(S.errorBlock(res && res.error, () => {
						childOpen = false;
						toggleChildren();
					}));
					return;
				}
				const neighbors = res.nodes || [];
				if (!neighbors.length) {
					childHost.appendChild(el('div', 'note', 'No linked memories.'));
					return;
				}
				for (const nb of neighbors) {
					childHost.appendChild(nodeRow(nb));
				}
			} catch (err) {
				childHost.textContent = '';
				childHost.appendChild(
					S.errorBlock({ summary: 'Could not follow links.', detail: String((err && err.message) || err) })
				);
			}
		}
		chev.addEventListener('click', toggleChildren);
		chev.addEventListener('keydown', (e) => {
			if (e.key === 'Enter' || e.key === ' ') {
				e.preventDefault();
				toggleChildren(e);
			}
		});

		return wrap;
	}

	/**
	 * The detail card. The shipped version dumped the whole `get` envelope
	 * ({"v":1,"nodes":[...],"missing":[]}) into a <pre>. Engraphy's envelope has
	 * body, attrs, addenda, status, author and both edge directions, so it is
	 * rendered as a record, with the raw JSON kept behind a disclosure for anyone
	 * who wants it.
	 */
	function detailCard(n) {
		const card = el('div', 'record');

		card.appendChild(el('div', 'record-title', n.title));

		const meta = el('div', 'record-badges');
		meta.appendChild(el('span', 'badge', n.type));
		meta.appendChild(el('span', 'badge', 'scope: ' + n.scope));
		if (n.status) {
			meta.appendChild(el('span', 'badge', n.status));
		}
		card.appendChild(meta);

		if (n.body) {
			card.appendChild(el('div', 'record-body', n.body));
		} else {
			card.appendChild(el('div', 'note', 'No body text on this memory.'));
		}

		if (n.attrs && n.attrs.length) {
			const dl = el('div', 'record-attrs');
			for (const a of n.attrs) {
				const rowEl = el('div', 'record-attr');
				rowEl.appendChild(el('span', 'record-attr-key brand-mono', a.key));
				rowEl.appendChild(el('span', 'record-attr-val', a.value));
				dl.appendChild(rowEl);
			}
			card.appendChild(el('div', 'record-label', 'Attributes'));
			card.appendChild(dl);
		}

		if (n.edges && n.edges.length) {
			card.appendChild(el('div', 'record-label', 'Links'));
			const links = el('div', 'record-links');
			for (const e of n.edges) {
				const chip = el('span', 'record-link');
				chip.appendChild(el('span', 'record-link-dir', e.direction === 'out' ? '→' : '←'));
				chip.appendChild(el('span', 'record-link-type brand-mono', e.type));
				chip.appendChild(el('span', 'record-link-id', e.otherId));
				links.appendChild(chip);
			}
			card.appendChild(links);
		}

		if (n.addenda && n.addenda.length) {
			card.appendChild(el('div', 'record-label', 'History'));
			for (const a of n.addenda) {
				card.appendChild(el('div', 'record-addendum', a));
			}
		}

		const foot = [];
		if (n.author) {
			foot.push('by ' + n.author);
		}
		if (n.createdAt) {
			foot.push('created ' + n.createdAt);
		}
		foot.push(n.id);
		card.appendChild(el('div', 'meta', foot.join(' · ')));

		const det = document.createElement('details');
		det.appendChild(el('summary', 'cand-label', 'Raw record'));
		const pre = el('pre', 'payload');
		pre.textContent = n.raw;
		det.appendChild(pre);
		card.appendChild(det);

		return card;
	}

	/**
	 * Re-run only when the panel is STUCK on a failure block. Switching tabs
	 * must not silently re-query a server, and it must not throw away results
	 * the user searched for. The case this exists for is "I fixed my token in
	 * Settings, now show me my memories": the shell calls this after a
	 * successful save or reconnect.
	 */
	ctx.onRefresh = function () {
		if (lastFailed) {
			doSearch();
		}
	};

	// AUTO-BROWSE ON OPEN. This is the fix for "I connected and Memories showed
	// nothing". The panel used to mount into an idle prompt and run no query at
	// all, so someone who had just connected a working server saw an empty page,
	// which is indistinguishable from a broken one. Verified against a live
	// Engraphy server: an empty query returns everything the token can read.
	//
	// Failures route through the same showFailure path as a manual search, so an
	// unconfigured / unreachable / rejected-token server still lands on its
	// recovery block instead of on a failed query.
	//
	// This call MUST stay below the `let busy / lastFailed / userSearched`
	// declarations: function declarations hoist but those bindings do not, so
	// calling it earlier throws on the temporal dead zone.
	browse();

	/** Force a fresh listing, used after connection settings change. */
	ctx.reload = function () {
		if (!userSearched) {
			browse();
		} else if (lastFailed) {
			doSearch();
		}
	};
};
