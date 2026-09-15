// Engraphy settings — desktop view (built fresh).
//
// The extension read serverUrl / token / space from VS Code settings and had a
// "configure server" command. There is no VS Code here, so this is the app's own
// connection screen. It never displays the stored token (main only reports
// whether one is set, and whether it had to be stored insecurely).
//
// Three things this does that the extension's input-box chain could not:
//   • live per-field validation as you type (main owns the pure validator, so
//     the renderer asks rather than duplicating the rules)
//   • a TEST CONNECTION button that probes without persisting anything, so
//     testing a typo cannot destroy the working connection you already had
//   • it reports what it normalized (trailing slash, stripped "Bearer ") rather
//     than silently fixing or scolding
//
// Authored from scratch, textContent only.
window.initSettings = function (ctx, opts) {
	'use strict';
	const host = ctx.host;
	const root = ctx.root;
	const S = window.ENGRAPHY_STATES;
	const el = S.el;
	const onSaved = (opts && opts.onSaved) || function () {};

	// Order matters: label, control, hint, then extras (checkbox / storage note),
	// then validation issues. Putting the hint after the extras read as though it
	// belonged to them.
	function field(labelText, hintText) {
		const f = el('div', 'form-field');
		f.appendChild(el('label', null, labelText));
		const slot = el('div', 'field-slot');
		f.appendChild(slot);
		if (hintText) {
			f.appendChild(el('div', 'hint', hintText));
		}
		const extras = el('div', 'field-extras');
		f.appendChild(extras);
		const issues = el('div', 'field-issues');
		f.appendChild(issues);
		return { field: f, slot: slot, extras: extras, issues: issues };
	}

	/** Render a validator's issue list under its field. */
	function renderIssues(container, issues) {
		container.textContent = '';
		for (const i of issues || []) {
			const row = el('div', 'field-issue ' + (i.level === 'error' ? 'is-error' : 'is-warn'));
			row.appendChild(el('span', 'field-issue-icon', i.level === 'error' ? '✕' : '!'));
			row.appendChild(el('span', null, i.message));
			container.appendChild(row);
		}
	}

	const form = el('div', 'form');
	form.appendChild(
		el(
			'p',
			'form-intro',
			'Engraphy talks to an Engraphy server over MCP. Point it at yours, then test the connection.'
		)
	);

	// ---- server URL --------------------------------------------------------
	const urlF = field(
		'Server URL',
		'The MCP endpoint (Streamable HTTP). Keep the trailing slash. Local bring-up: http://127.0.0.1:8000/mcp/'
	);
	const urlInput = document.createElement('input');
	urlInput.type = 'text';
	urlInput.spellcheck = false;
	urlInput.placeholder = 'http://127.0.0.1:8000/mcp/';
	urlInput.setAttribute('aria-label', 'Server URL');
	urlF.slot.appendChild(urlInput);
	const resetUrl = el('button', 'link-btn', 'Use the local default');
	resetUrl.type = 'button';
	urlF.extras.appendChild(resetUrl);
	form.appendChild(urlF.field);

	// ---- token -------------------------------------------------------------
	const tokenF = field(
		'Token',
		'A bearer token from "engraphy-admin token create". It IS your identity on the server, and it is bound to one space. Stored in your OS keychain.'
	);
	const tokenRow = el('div', 'input-row');
	const tokenInput = document.createElement('input');
	tokenInput.type = 'password';
	tokenInput.autocomplete = 'off';
	tokenInput.spellcheck = false;
	tokenInput.setAttribute('aria-label', 'Token');
	tokenRow.appendChild(tokenInput);
	const revealBtn = el('button', 'btn btn-ghost btn-inline', 'Show');
	revealBtn.type = 'button';
	revealBtn.title = 'Show the token you just typed. The stored token is never displayed.';
	tokenRow.appendChild(revealBtn);
	tokenF.slot.appendChild(tokenRow);

	const clearWrap = el('label', 'checkbox-row');
	const clearBox = document.createElement('input');
	clearBox.type = 'checkbox';
	clearWrap.appendChild(clearBox);
	clearWrap.appendChild(document.createTextNode(' Clear the stored token'));
	tokenF.extras.appendChild(clearWrap);

	const secureNote = el('div', 'secure-note');
	tokenF.extras.appendChild(secureNote);
	form.appendChild(tokenF.field);

	// ---- space -------------------------------------------------------------
	const spaceF = field(
		'Space label',
		'Optional label shown on the connection badge. Informational only: the real space is fixed by your token.'
	);
	const spaceInput = document.createElement('input');
	spaceInput.type = 'text';
	spaceInput.placeholder = 'e.g. team';
	spaceInput.setAttribute('aria-label', 'Space label');
	spaceF.slot.appendChild(spaceInput);
	form.appendChild(spaceF.field);

	// ---- updates -----------------------------------------------------------
	// Applied on change rather than on Save: it is a preference about this app,
	// not part of the connection the Save button tests and stores.
	const updateF = field(
		'Updates',
		'Engraphy asks engraphy.tech once a day whether a newer version is published. The request is for a public file and carries nothing about this machine or the version you are running.'
	);
	const updateWrap = el('label', 'checkbox-row');
	const updateBox = document.createElement('input');
	updateBox.type = 'checkbox';
	updateBox.checked = true;
	updateWrap.appendChild(updateBox);
	updateWrap.appendChild(document.createTextNode(' Check for new versions'));
	updateF.slot.appendChild(updateWrap);
	form.appendChild(updateF.field);

	updateBox.addEventListener('change', () => {
		host.invoke({ type: 'setUpdateCheck', enabled: updateBox.checked }).catch(() => {});
	});

	// ---- actions -----------------------------------------------------------
	const actions = el('div', 'form-actions');
	const saveBtn = el('button', 'btn btn-approve', 'Save & connect');
	const testBtn = el('button', 'btn btn-secondary', 'Test connection');
	testBtn.title = 'Probe this URL and token without saving anything';
	actions.appendChild(saveBtn);
	actions.appendChild(testBtn);
	form.appendChild(actions);

	const status = el('div', 'connection-status');
	form.appendChild(status);

	// ---- footer ------------------------------------------------------------
	const footer = el('div', 'form-footer');
	const openSetup = el('button', 'link-btn', 'Open the setup guide');
	openSetup.type = 'button';
	openSetup.addEventListener('click', () => window.ENGRAPHY.showOnboarding());
	footer.appendChild(openSetup);
	const revealFile = el('button', 'link-btn', 'Show settings file');
	revealFile.type = 'button';
	revealFile.addEventListener('click', () => host.invoke({ type: 'revealSettingsFile' }));
	footer.appendChild(revealFile);
	const pathNote = el('div', 'hint');
	footer.appendChild(pathNote);
	form.appendChild(footer);

	root.appendChild(form);

	// ---- state -------------------------------------------------------------
	let hasToken = false;
	let defaultUrl = 'http://127.0.0.1:8000/mcp/';

	function reflectTokenState() {
		tokenInput.placeholder = hasToken
			? 'A token is stored. Leave blank to keep it.'
			: 'Paste your bearer token';
		clearWrap.style.display = hasToken ? '' : 'none';
	}

	function applySettings(s) {
		// Absent means on, so an older settings file reads as enabled.
		updateBox.checked = !s || s.updateCheckEnabled !== false;
		if (!s) {
			return;
		}
		urlInput.value = s.serverUrl || '';
		spaceInput.value = s.space || '';
		hasToken = !!s.hasToken;
		reflectTokenState();
		pathNote.textContent = s.settingsPath ? 'Stored at ' + s.settingsPath : '';
		if (s.tokenInsecure) {
			secureNote.className = 'secure-note warn';
			secureNote.textContent =
				'Heads up: the OS keychain was unavailable, so the token is stored in plaintext in your app data folder.';
		} else {
			secureNote.className = 'secure-note';
			secureNote.textContent = hasToken ? 'Stored in your OS keychain.' : '';
		}
	}

	host
		.invoke({ type: 'load' })
		.then((res) => {
			if (!res || !res.ok) {
				setStatus('bad', 'Could not read your saved settings.', res && res.error);
				return;
			}
			defaultUrl = res.defaultServerUrl || defaultUrl;
			applySettings(res.settings);
		})
		.catch(() => {
			setStatus('bad', 'Could not read your saved settings.');
		});

	resetUrl.addEventListener('click', () => {
		urlInput.value = defaultUrl;
		scheduleValidate();
		urlInput.focus();
	});
	revealBtn.addEventListener('click', () => {
		const showing = tokenInput.type === 'text';
		tokenInput.type = showing ? 'password' : 'text';
		revealBtn.textContent = showing ? 'Show' : 'Hide';
	});
	clearBox.addEventListener('change', () => {
		tokenInput.disabled = clearBox.checked;
		if (clearBox.checked) {
			tokenInput.value = '';
		}
	});

	// ---- token semantics ---------------------------------------------------
	// undefined = keep the stored token; '' = clear it; string = set it.
	function tokenArg() {
		if (clearBox.checked) {
			return '';
		}
		if (tokenInput.value.length > 0) {
			return tokenInput.value;
		}
		return undefined;
	}

	// ---- live validation ---------------------------------------------------
	// The rules live in main (one pure, unit-tested module), so the renderer asks
	// instead of keeping a second, drifting copy.
	let validateTimer;
	function scheduleValidate() {
		clearTimeout(validateTimer);
		validateTimer = setTimeout(runValidate, 250);
	}
	async function runValidate() {
		try {
			const res = await host.invoke({
				type: 'validate',
				serverUrl: urlInput.value,
				token: tokenArg(),
				space: spaceInput.value,
			});
			if (!res || !res.ok) {
				return;
			}
			showValidation(res.validation);
		} catch (e) {
			// validation is advisory; never let it break typing
		}
	}
	function showValidation(v) {
		if (!v) {
			return;
		}
		renderIssues(urlF.issues, v.serverUrl.issues);
		renderIssues(tokenF.issues, v.token.issues);
		renderIssues(spaceF.issues, v.space.issues);
		saveBtn.disabled = !v.ok;
		testBtn.disabled = !v.ok;
		return v;
	}
	for (const inputEl of [urlInput, spaceInput, tokenInput]) {
		inputEl.addEventListener('input', scheduleValidate);
	}
	urlInput.addEventListener('blur', runValidate);

	// ---- status area -------------------------------------------------------
	function setStatus(tone, text, error, extra) {
		status.textContent = '';
		if (!text) {
			return;
		}
		const box = el('div', 'status-box status-' + tone);
		const head = el('div', 'status-head');
		head.appendChild(el('span', 'status-icon', tone === 'ok' ? '✓' : tone === 'busy' ? '' : '!'));
		if (tone === 'busy') {
			head.appendChild(el('span', 'spinner'));
		}
		head.appendChild(el('span', 'status-text', text));
		box.appendChild(head);
		if (extra) {
			box.appendChild(el('div', 'status-extra', extra));
		}
		if (error && error.detail) {
			const det = document.createElement('details');
			det.appendChild(el('summary', 'cand-label', 'Technical detail'));
			const pre = el('pre', 'payload');
			pre.textContent = error.detail;
			det.appendChild(pre);
			box.appendChild(det);
		}
		status.appendChild(box);
	}

	/** Turn a probe failure into a remedy sentence, not just an error string. */
	function remedyFor(error) {
		const cls = (error && error.class) || 'tool';
		if (cls === 'auth') {
			return 'The server is running but rejected this token. Mint a new one with "engraphy-admin token create" for the space you want to read.';
		}
		if (cls === 'transport') {
			return 'Nothing answered at that address. Check the server is running, and that the host and port are right.';
		}
		if (cls === 'config') {
			return 'Fill in the server URL first.';
		}
		return 'The server answered but rejected the request.';
	}

	function describeSuccess(result) {
		const info = result && result.info;
		const bits = [];
		if (info && info.version) {
			bits.push('server v' + info.version);
		}
		if (info && typeof info.spaces === 'number') {
			bits.push(info.spaces + ' space(s)');
		}
		if (result && result.scopes && result.scopes.length) {
			bits.push('scopes you can read: ' + result.scopes.join(', '));
		}
		return bits.join(' · ');
	}

	// ---- test connection (never persists) ----------------------------------
	testBtn.addEventListener('click', async () => {
		testBtn.disabled = true;
		setStatus('busy', 'Testing…');
		try {
			const res = await host.invoke({
				type: 'test',
				serverUrl: urlInput.value,
				token: tokenArg(),
				space: spaceInput.value,
			});
			if (!res || !res.ok) {
				setStatus('bad', 'Could not run the test.', res && res.error);
				return;
			}
			showValidation(res.validation);
			if (res.blocked) {
				setStatus('bad', res.blocked);
				return;
			}
			const r = res.result;
			if (r && r.ok) {
				setStatus(
					'ok',
					'Connected. This URL and token work.',
					null,
					describeSuccess(r) || 'Nothing else to report.'
				);
			} else {
				setStatus('bad', (r && r.error && r.error.summary) || 'The test failed.', r && r.error, remedyFor(r && r.error));
			}
		} catch (e) {
			setStatus('bad', 'Could not run the test: ' + String((e && e.message) || e));
		} finally {
			testBtn.disabled = false;
		}
	});

	// ---- save --------------------------------------------------------------
	saveBtn.addEventListener('click', async () => {
		saveBtn.disabled = true;
		setStatus('busy', 'Saving and connecting…');
		try {
			const res = await host.invoke({
				type: 'save',
				serverUrl: urlInput.value,
				space: spaceInput.value,
				token: tokenArg(),
			});
			if (!res || !res.ok) {
				setStatus('bad', 'Save failed.', res && res.error);
				return;
			}
			showValidation(res.validation);
			if (!res.saved) {
				setStatus('bad', res.blocked || 'Nothing was saved.');
				return;
			}

			applySettings(res.settings);
			tokenInput.value = '';
			tokenInput.disabled = false;
			clearBox.checked = false;

			// Report the truth after saving: saved is not the same as working.
			if (res.authOk) {
				const info = res.info || {};
				const bits = [];
				if (info.version) {
					bits.push('server v' + info.version);
				}
				if (typeof info.spaces === 'number') {
					bits.push(info.spaces + ' space(s)');
				}
				setStatus('ok', 'Saved and connected.', null, bits.join(' · '));
			} else {
				setStatus(
					'warn',
					'Saved, but could not read from the server yet.',
					res.probeError,
					remedyFor(res.probeError)
				);
			}
			onSaved();
		} catch (e) {
			setStatus('bad', 'Save failed: ' + String((e && e.message) || e));
		} finally {
			saveBtn.disabled = false;
		}
	});

	/** Let the shell push fresh settings in after onboarding writes them. */
	ctx.onRefresh = function () {
		host
			.invoke({ type: 'load' })
			.then((res) => {
				if (res && res.ok) {
					applySettings(res.settings);
				}
			})
			.catch(() => {});
	};
};
