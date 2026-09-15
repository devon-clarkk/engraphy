// Engraphy first-run onboarding.
//
// The extension shipped a VS Code walkthrough (media/walkthrough/*.md) that the
// desktop app cannot host. This is its replacement: a three-step overlay that
// opens once, on first run, and can be reopened any time from Help or Settings.
//
// It is written for someone who is NOT a server operator. The premise is that a
// person who lives in chat windows just installed this and has no idea what an
// "MCP endpoint" is, so step 2 lays out the three ways to get a server and is
// honest that hosted Engraphy does not exist yet, and step 3 does the actual
// connecting with a test button before it commits anything.
//
// Reuses the settings channel for validate / test / save, so there is exactly
// one implementation of those rules. textContent only.
window.mountOnboarding = function (ctx) {
	'use strict';
	const S = window.ENGRAPHY_STATES;
	const el = S.el;
	const host = ctx.host; // onboarding channel
	const settingsHost = ctx.settingsHost; // settings channel
	const mount = ctx.mount;

	const TOTAL = 3;
	let step = 1;
	let state = null;
	let open = false;

	function button(cls, label, onClick, title) {
		const b = el('button', cls, label);
		b.type = 'button';
		if (title) {
			b.title = title;
		}
		b.addEventListener('click', onClick);
		return b;
	}

	function close(markComplete) {
		open = false;
		document.removeEventListener('keydown', onKey);
		mount.textContent = '';
		if (markComplete) {
			host.invoke({ type: 'complete' }).catch(() => {});
		}
		if (ctx.onClose) {
			ctx.onClose();
		}
	}

	function onKey(e) {
		if (e.key === 'Escape') {
			close(true);
		}
	}

	async function show(startStep) {
		try {
			const res = await host.invoke({ type: 'state' });
			state = res && res.ok ? res : null;
		} catch (e) {
			state = null;
		}
		open = true;
		step = startStep || 1;
		document.addEventListener('keydown', onKey);
		render();
	}

	/** Open only if the user has never finished or skipped it. */
	async function showIfFirstRun() {
		try {
			const res = await host.invoke({ type: 'state' });
			if (res && res.ok && !res.completed) {
				state = res;
				open = true;
				step = 1;
				document.addEventListener('keydown', onKey);
				render();
			}
		} catch (e) {
			// Never block startup on onboarding.
		}
	}

	// ---- steps -------------------------------------------------------------

	function stepWelcome(body) {
		body.appendChild(S.markEl('mark mark-xl'));
		body.appendChild(el('h2', 'ob-title', 'Welcome to Engraphy'));
		body.appendChild(
			el(
				'p',
				'ob-lead',
				'Engraphy is a memory for your AI agents: a graph of the notes, people, preferences and commitments they have learned, that they can search and add to across conversations.'
			)
		);

		const points = el('div', 'ob-points');
		const point = (title, text) => {
			const p = el('div', 'ob-point');
			p.appendChild(el('div', 'ob-point-title', title));
			p.appendChild(el('div', 'ob-point-text', text));
			return p;
		};
		points.appendChild(
			point('Browse what it remembers', 'Search the graph and open any memory to see the full record.')
		);
		points.appendChild(
			point(
				'Approve what it writes',
				'When an agent writes something that looks like an existing memory, you decide: keep it separate, or merge it in.'
			)
		);
		points.appendChild(
			point('See what it is worth', 'Duplicates prevented, memory reused, answer rate, over time.')
		);
		body.appendChild(points);

		body.appendChild(
			el(
				'p',
				'ob-note',
				'This app is a window onto a server that stores your memories. The next step is getting one to point it at.'
			)
		);
	}

	function stepServer(body) {
		body.appendChild(el('h2', 'ob-title', 'Get a memory server'));
		body.appendChild(
			el(
				'p',
				'ob-lead',
				'Engraphy reads from an Engraphy server. There are three ways to have one, and only two of them work today.'
			)
		);

		const options = el('div', 'ob-options');

		const opt = (badge, badgeCls, title, text, extra) => {
			const o = el('div', 'ob-option');
			const head = el('div', 'ob-option-head');
			head.appendChild(el('span', 'ob-badge ' + badgeCls, badge));
			head.appendChild(el('span', 'ob-option-title', title));
			o.appendChild(head);
			o.appendChild(el('div', 'ob-option-text', text));
			if (extra) {
				o.appendChild(extra);
			}
			return o;
		};

		// 1. Someone already runs one. By far the easiest path for a normal user.
		options.appendChild(
			opt(
				'Easiest',
				'ob-badge-good',
				'Someone already runs one',
				'If your team already runs Engraphy, ask them for the MCP URL and a token for your space. That is all you need. Continue to the next step and paste them in.'
			)
		);

		// 2. Docker, condensed from the extension walkthrough's docker.md.
		const dockerSteps = el('div', 'ob-steps');
		const cmd = (text, note) => {
			const c = el('div', 'ob-cmd');
			const pre = el('pre', 'ob-code');
			pre.textContent = text;
			c.appendChild(pre);
			if (note) {
				c.appendChild(el('div', 'ob-cmd-note', note));
			}
			return c;
		};
		dockerSteps.appendChild(
			el('div', 'ob-steps-lead', 'From a checkout of the Engraphy repo, with Docker running:')
		);
		dockerSteps.appendChild(
			cmd(
				'docker compose up -d',
				'Brings up the whole stack: Postgres, the schema, and the server. First boot downloads the embedding model (about 523 MB) before it answers anything, so give it a minute.'
			)
		);
		dockerSteps.appendChild(
			cmd(
				'docker compose --profile admin run --rm admin \\\n  engraphy-admin space create --id personal --display-name "Personal" --principal me',
				'Create a space.'
			)
		);
		dockerSteps.appendChild(
			cmd(
				'docker compose --profile admin run --rm admin \\\n  engraphy-admin pack apply packs/starter/pack.yaml --space personal',
				'Apply the starter pack.'
			)
		);
		dockerSteps.appendChild(
			cmd(
				'docker compose --profile admin run --rm admin \\\n  engraphy-admin token create --space personal \\\n  --principal me --client-name desktop --role readwrite',
				'Mint your token. It prints once, so copy it now.'
			)
		);
		dockerSteps.appendChild(
			el('div', 'ob-cmd-note', 'Then use http://127.0.0.1:8000/mcp/ as the server URL on the next step.')
		);
		options.appendChild(
			opt(
				'Works today',
				'ob-badge-good',
				'Run one yourself with Docker',
				'You need Docker and a checkout of the Engraphy repo.',
				dockerSteps
			)
		);

		// 3. Cloud. Not available; say so plainly rather than implying a signup.
		options.appendChild(
			opt(
				'Not yet',
				'ob-badge-muted',
				'Engraphy Cloud',
				'There is no hosted Engraphy service today: no signup, no hosted endpoint. It is planned, not available. Use one of the two options above for now.'
			)
		);

		body.appendChild(options);

		const links = el('div', 'ob-links');
		links.appendChild(
			button('link-btn', 'Open the Engraphy repo', () => {
				if (state && state.repoUrl) {
					window.engraphyIPC.openExternal(state.repoUrl);
				}
			})
		);
		body.appendChild(links);
	}

	function stepConnect(body) {
		body.appendChild(el('h2', 'ob-title', 'Connect it'));
		body.appendChild(
			el('p', 'ob-lead', 'Paste your server URL and token. Test it before saving, so you know it works.')
		);

		const f = el('div', 'form ob-form');

		const urlField = el('div', 'form-field');
		urlField.appendChild(el('label', null, 'Server URL'));
		const urlInput = document.createElement('input');
		urlInput.type = 'text';
		urlInput.spellcheck = false;
		urlInput.placeholder = 'http://127.0.0.1:8000/mcp/';
		urlInput.value =
			(state && state.settings && state.settings.serverUrl) ||
			(state && state.defaultServerUrl) ||
			'http://127.0.0.1:8000/mcp/';
		urlField.appendChild(urlInput);
		urlField.appendChild(
			el('div', 'hint', 'The MCP endpoint. Keep the trailing slash. A local Docker bring-up uses the value above.')
		);
		const urlIssues = el('div', 'field-issues');
		urlField.appendChild(urlIssues);
		f.appendChild(urlField);

		const tokenField = el('div', 'form-field');
		tokenField.appendChild(el('label', null, 'Token'));
		const tokenInput = document.createElement('input');
		tokenInput.type = 'password';
		tokenInput.autocomplete = 'off';
		tokenInput.spellcheck = false;
		tokenInput.placeholder =
			state && state.settings && state.settings.hasToken
				? 'A token is already stored. Leave blank to keep it.'
				: 'Paste the token from "engraphy-admin token create"';
		tokenField.appendChild(tokenInput);
		tokenField.appendChild(
			el('div', 'hint', 'Your token is your identity on the server. It is stored in your OS keychain, never in plaintext.')
		);
		const tokenIssues = el('div', 'field-issues');
		tokenField.appendChild(tokenIssues);
		f.appendChild(tokenField);

		const acts = el('div', 'form-actions');
		const testBtn = button('btn btn-secondary', 'Test connection', doTest);
		const saveBtn = button('btn btn-approve', 'Save & finish', doSave);
		acts.appendChild(testBtn);
		acts.appendChild(saveBtn);
		f.appendChild(acts);

		const status = el('div', 'connection-status');
		f.appendChild(status);
		body.appendChild(f);

		function tokenArg() {
			return tokenInput.value.length > 0 ? tokenInput.value : undefined;
		}

		function setStatus(tone, text, extra, detail) {
			status.textContent = '';
			const box = el('div', 'status-box status-' + tone);
			const head = el('div', 'status-head');
			if (tone === 'busy') {
				head.appendChild(el('span', 'spinner'));
			} else {
				head.appendChild(el('span', 'status-icon', tone === 'ok' ? '✓' : '!'));
			}
			head.appendChild(el('span', 'status-text', text));
			box.appendChild(head);
			if (extra) {
				box.appendChild(el('div', 'status-extra', extra));
			}
			if (detail) {
				const det = document.createElement('details');
				det.appendChild(el('summary', 'cand-label', 'Technical detail'));
				const pre = el('pre', 'payload');
				pre.textContent = detail;
				det.appendChild(pre);
				box.appendChild(det);
			}
			status.appendChild(box);
		}

		function showIssues(v) {
			if (!v) {
				return;
			}
			const paint = (container, issues) => {
				container.textContent = '';
				for (const i of issues || []) {
					const row = el('div', 'field-issue ' + (i.level === 'error' ? 'is-error' : 'is-warn'));
					row.appendChild(el('span', 'field-issue-icon', i.level === 'error' ? '✕' : '!'));
					row.appendChild(el('span', null, i.message));
					container.appendChild(row);
				}
			};
			paint(urlIssues, v.serverUrl.issues);
			paint(tokenIssues, v.token.issues);
		}

		function remedy(error) {
			const cls = (error && error.class) || 'tool';
			if (cls === 'auth') {
				return 'The server is running, but it did not accept this token. Check you copied the whole token, and that it was minted for the space you want.';
			}
			if (cls === 'transport') {
				return 'Nothing answered at that address. If you are running it with Docker, check "docker compose ps" shows engraphy as healthy.';
			}
			return 'Check the URL and try again.';
		}

		async function doTest() {
			testBtn.disabled = true;
			setStatus('busy', 'Testing…');
			try {
				const res = await settingsHost.invoke({
					type: 'test',
					serverUrl: urlInput.value,
					token: tokenArg(),
					space: (state && state.settings && state.settings.space) || '',
				});
				if (!res || !res.ok) {
					setStatus('bad', 'Could not run the test.');
					return;
				}
				showIssues(res.validation);
				if (res.blocked) {
					setStatus('bad', res.blocked);
					return;
				}
				const r = res.result;
				if (r && r.ok) {
					const scopes = r.scopes && r.scopes.length ? 'You can read: ' + r.scopes.join(', ') : '';
					setStatus('ok', 'It works. You are connected.', scopes);
				} else {
					setStatus('bad', (r && r.error && r.error.summary) || 'The test failed.', remedy(r && r.error), r && r.error && r.error.detail);
				}
			} catch (e) {
				setStatus('bad', 'Could not run the test: ' + String((e && e.message) || e));
			} finally {
				testBtn.disabled = false;
			}
		}

		async function doSave() {
			saveBtn.disabled = true;
			setStatus('busy', 'Saving…');
			try {
				const res = await settingsHost.invoke({
					type: 'save',
					serverUrl: urlInput.value,
					token: tokenArg(),
					space: (state && state.settings && state.settings.space) || '',
				});
				if (!res || !res.ok || !res.saved) {
					showIssues(res && res.validation);
					setStatus('bad', (res && res.blocked) || 'Nothing was saved.');
					return;
				}
				showIssues(res.validation);
				if (res.authOk) {
					setStatus('ok', 'Connected. Setting up your panels…');
					setTimeout(() => close(true), 700);
				} else {
					// Saved but not working: let them stay and fix it rather than
					// dropping them into four erroring panels.
					setStatus(
						'warn',
						'Saved, but could not read from the server yet.',
						remedy(res.probeError),
						res.probeError && res.probeError.detail
					);
				}
				if (ctx.onSaved) {
					ctx.onSaved();
				}
			} catch (e) {
				setStatus('bad', 'Save failed: ' + String((e && e.message) || e));
			} finally {
				saveBtn.disabled = false;
			}
		}

		setTimeout(() => urlInput.focus(), 30);
	}

	// ---- frame -------------------------------------------------------------

	function render() {
		if (!open) {
			return;
		}
		mount.textContent = '';

		const backdrop = el('div', 'ob-backdrop');
		const panel = el('div', 'ob-panel');
		panel.setAttribute('role', 'dialog');
		panel.setAttribute('aria-modal', 'true');
		panel.setAttribute('aria-label', 'Set up Engraphy');

		const body = el('div', 'ob-body');
		if (step === 1) {
			stepWelcome(body);
		} else if (step === 2) {
			stepServer(body);
		} else {
			stepConnect(body);
		}
		panel.appendChild(body);

		// footer: dots + navigation
		const foot = el('div', 'ob-foot');
		const dots = el('div', 'ob-dots');
		for (let i = 1; i <= TOTAL; i++) {
			const d = el('span', 'ob-dot' + (i === step ? ' active' : ''));
			d.title = 'Step ' + i + ' of ' + TOTAL;
			dots.appendChild(d);
		}
		foot.appendChild(dots);

		const nav = el('div', 'ob-nav');
		nav.appendChild(
			button('btn btn-ghost', 'Skip for now', () => close(true), 'You can reopen this from Settings or the Help menu')
		);
		if (step > 1) {
			nav.appendChild(
				button('btn btn-secondary', 'Back', () => {
					step -= 1;
					render();
				})
			);
		}
		if (step < TOTAL) {
			nav.appendChild(
				button('btn btn-approve', 'Next', () => {
					step += 1;
					render();
				})
			);
		}
		foot.appendChild(nav);
		panel.appendChild(foot);

		backdrop.appendChild(panel);
		mount.appendChild(backdrop);
	}

	return { show: show, showIfFirstRun: showIfFirstRun, close: close };
};
