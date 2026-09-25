#!/usr/bin/env node
// End-to-end state verification: launch the real app once per connection
// scenario, drive it, and assert on what each panel actually rendered.
//
// WHY THIS AND NOT UNIT TESTS: scripts/test-client.js proves the pure models
// pick the right state. It cannot prove the app RENDERS that state instead of
// crashing, blanking, or showing "No results" for a rejected token. Those are
// exactly the failures the release work was about, so each one gets a scenario
// here.
//
// Scenarios (each in a throwaway user-data profile, so the first-run flag and
// saved settings never leak between runs):
//
//   connected     stub server, valid settings          → cards, tiles, results
//   empty         stub with STUB_EMPTY=1               → empty states, no errors
//   unauthorized  the LIVE Engraphy server with no token → "token rejected", NOT
//                                                        "start a server"
//   unreachable   a dead port                          → "cannot reach", retry
//   unconfigured  no serverUrl at all                  → connect-your-server
//   onboarding    fresh profile, first run             → the overlay opens
//
// The unauthorized scenario needs a live server on ENGRAPHY_LIVE_URL (default
// http://127.0.0.1:8000/healthz). It is SKIPPED, not failed, when nothing is
// listening, so this stays runnable on a machine with Docker down.
//
// Run: npm run smoke        (add --shots to also write docs screenshots)

'use strict';

const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const ELECTRON = require(path.join(ROOT, 'node_modules', 'electron'));
const WANT_SHOTS = process.argv.includes('--shots');
// --packaged drives the BUILT app (release/win-unpacked/Engraphy.exe) instead of
// electron + the source tree. Same scenarios, same assertions, but exercising
// the artifact that actually ships: asar packing, the icon, and the fact that
// the renderer tree really did get copied in.
const PACKAGED = process.argv.includes('--packaged');
const ONLY = (process.argv.find((a) => a.startsWith('--only=')) || '').replace('--only=', '');

const STUB_PORT = Number(process.env.SMOKE_STUB_PORT || 8010);
const EMPTY_STUB_PORT = Number(process.env.SMOKE_EMPTY_PORT || 8011);
const DEAD_PORT = Number(process.env.SMOKE_DEAD_PORT || 9911);
const LIVE_URL = process.env.ENGRAPHY_LIVE_URL || 'http://127.0.0.1:8000';

let failures = 0;
let checks = 0;

function ok(name, cond, detail) {
	checks++;
	if (cond) {
		console.log('  ok   - ' + name);
	} else {
		failures++;
		console.log('  FAIL - ' + name + (detail ? '\n         ' + detail : ''));
	}
}

function sleep(ms) {
	return new Promise((r) => setTimeout(r, ms));
}

// ---- helpers ---------------------------------------------------------------

async function isUp(url) {
	try {
		const res = await fetch(url, { method: 'GET' });
		return res.ok;
	} catch (e) {
		return false;
	}
}

function startStub(port, empty, delayMs) {
	const child = spawn(process.execPath, [path.join(ROOT, 'out', 'stub-server.js')], {
		env: {
			...process.env,
			STUB_PORT: String(port),
			STUB_EMPTY: empty ? '1' : '',
			STUB_DELAY_MS: delayMs ? String(delayMs) : '',
		},
		stdio: ['ignore', 'pipe', 'pipe'],
	});
	child.stdout.on('data', () => {});
	child.stderr.on('data', () => {});
	return child;
}

async function waitForStub(port, tries) {
	for (let i = 0; i < (tries || 40); i++) {
		if (await isUp('http://127.0.0.1:' + port + '/healthz')) {
			return true;
		}
		await sleep(250);
	}
	return false;
}

/** Launch the app under a scenario and return the parsed SMOKE_* lines. */
function runApp(scenario) {
	return new Promise((resolve) => {
		const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'engraphy-smoke-'));
		const env = {
			...process.env,
			ENGRAPHY_SMOKE: '1',
			ENGRAPHY_SMOKE_SEED: '1',
			ENGRAPHY_USER_DATA: profile,
			ENGRAPHY_SMOKE_URL: scenario.url || '',
			ENGRAPHY_SMOKE_TOKEN: scenario.token || '',
			ENGRAPHY_SMOKE_SPACE: scenario.space || '',
		};
		if (scenario.onboarding) {
			env.ENGRAPHY_SMOKE_ONBOARDING = '1';
		}
		if (scenario.loading) {
			env.ENGRAPHY_SMOKE_LOADING = '1';
		}
		if (scenario.graphError) {
			env.ENGRAPHY_SMOKE_GRAPH_ERROR = '1';
		}
		if (scenario.graph) {
			env.ENGRAPHY_SMOKE_GRAPH = '1';
			env.ENGRAPHY_SMOKE_GRAPH_MS = String(scenario.graphBudgetMs || 420000);
			if (scenario.loadingShot) {
				env.ENGRAPHY_SMOKE_GRAPH_LOADING_SHOT = scenario.loadingShot;
			}
			if (scenario.doneShot) {
				env.ENGRAPHY_SMOKE_GRAPH_DONE_SHOT = scenario.doneShot;
			}
		}
		if (scenario.lightForce) {
			env.ENGRAPHY_SMOKE_LIGHT_FORCE = '1';
		}
		if (scenario.shot) {
			env.ENGRAPHY_SMOKE_SHOT = scenario.shot;
			env.ENGRAPHY_SMOKE_LIGHT = scenario.bothThemes ? '1' : '';
			if (scenario.panels) {
				env.ENGRAPHY_SMOKE_PANELS = scenario.panels;
			}
		}

		const exe = PACKAGED ? packagedExe() : ELECTRON;
		const args = PACKAGED ? [] : [ROOT];
		const child = spawn(exe, args, { env, stdio: ['ignore', 'pipe', 'pipe'] });
		let out = '';
		child.stdout.on('data', (d) => {
			out += String(d);
		});
		child.stderr.on('data', (d) => {
			out += String(d);
		});
		const kill = setTimeout(() => child.kill(), scenario.timeoutMs || 60000);
		child.on('close', () => {
			clearTimeout(kill);
			try {
				fs.rmSync(profile, { recursive: true, force: true });
			} catch (e) {
				// a locked cache file on Windows is not worth failing the run over
			}
			resolve(parseReport(out));
		});
	});
}

function packagedExe() {
	const candidates = [
		path.join(ROOT, 'release', 'win-unpacked', 'Engraphy.exe'),
		path.join(ROOT, 'release', 'mac-arm64', 'Engraphy.app', 'Contents', 'MacOS', 'Engraphy'),
		path.join(ROOT, 'release', 'mac', 'Engraphy.app', 'Contents', 'MacOS', 'Engraphy'),
		path.join(ROOT, 'release', 'linux-unpacked', 'engraphy'),
	];
	for (const c of candidates) {
		if (fs.existsSync(c)) {
			return c;
		}
	}
	console.error('No packaged app found. Run `npm run dist:win` (or dist:mac) first.');
	process.exit(1);
}

function parseReport(out) {
	const parsed = { raw: out };
	for (const line of out.split(/\r?\n/)) {
		// [A-Z0-9_]+, not [A-Z]+: a tag with a digit in it (SMOKE_PROMOTE2) was
		// silently dropped, which made its assertion read as a failure with no
		// detail rather than as a missing report line.
		const m = /^ENGRAPHY_(SMOKE_[A-Z0-9_]+) (.*)$/.exec(line.trim());
		if (m) {
			try {
				parsed[m[1].toLowerCase()] = JSON.parse(m[2]);
			} catch (e) {
				parsed[m[1].toLowerCase()] = m[2];
			}
		}
	}
	return parsed;
}

/** Assertions every scenario must satisfy, whatever the connection state. */
function assertNeverBroken(label, r) {
	ok(label + ': the app started and reported', !!r.smoke_report, 'no ENGRAPHY_SMOKE_REPORT line');
	if (!r.smoke_report) {
		console.log(r.raw.split('\n').slice(-25).join('\n'));
		return false;
	}
	ok(label + ': window is not blank', r.smoke_report.blank === false);
	ok(label + ': no crash line', !/ENGRAPHY_SMOKE_ERROR/.test(r.raw), (r.raw.match(/ENGRAPHY_SMOKE_ERROR.*/) || [''])[0]);
	ok(label + ': health badge has a phase', !!r.smoke_report.healthPhase);
	ok(label + ': health badge has text', !!r.smoke_report.health);
	return true;
}

// ---- scenarios -------------------------------------------------------------

async function scenarioConnected() {
	console.log('\n# connected (stub server, valid settings)');
	const stub = startStub(STUB_PORT, false);
	try {
		if (!(await waitForStub(STUB_PORT))) {
			ok('connected: stub came up', false);
			return;
		}
		const r = await runApp({
			url: 'http://127.0.0.1:' + STUB_PORT + '/mcp/',
			token: 'stub-token',
			space: 'demo',
			shot: WANT_SHOTS ? path.join(ROOT, 'docs', 'panels-PANEL.png') : undefined,
			bothThemes: true,
		});
		if (!assertNeverBroken('connected', r)) {
			return;
		}
		ok('connected: badge reads connected', r.smoke_report.healthPhase === 'connected', 'phase=' + r.smoke_report.healthPhase);
		ok('connected: no disconnected banner', !r.smoke_report.banner, 'banner=' + r.smoke_report.banner);
		ok('connected: confirm queue rendered cards', r.smoke_report.confirmCards > 0);
		ok('connected: no recovery block on confirm', r.smoke_report.confirmRecovery === 0);
		ok('connected: stats rendered tiles', r.smoke_report.statsTiles > 0);
		ok('connected: search returned rows', r.smoke_search && r.smoke_search.rows > 0, JSON.stringify(r.smoke_search));
		ok(
			'connected: approve removed a pending card',
			r.smoke_approve && r.smoke_approve.after < r.smoke_approve.before,
			JSON.stringify(r.smoke_approve)
		);
		// Menu.buildFromTemplate throws on a malformed template, so a built menu
		// with the expected accelerators is the check. win.removeMenu() used to
		// leave macOS with no Cmd+Q / Cmd+C / Cmd+V at all.
		ok(
			'connected: Memories auto-lists on open, with no query typed',
			!!(r.smoke_search && r.smoke_search.autoRows > 0),
			'autoRows=' + (r.smoke_search && r.smoke_search.autoRows) +
				' stateTitle=' + (r.smoke_search && r.smoke_search.autoStateTitle)
		);
		ok(
			'connected: opening a result renders a structured record, not raw JSON',
			!!(r.smoke_search && r.smoke_search.recordRendered && r.smoke_search.recordTitle),
			JSON.stringify(r.smoke_search)
		);
		ok(
			'connected: the record shows body text and its links',
			!!(r.smoke_search && r.smoke_search.recordBody > 0 && r.smoke_search.recordLinks > 0),
			JSON.stringify(r.smoke_search)
		);
		ok(
			'connected: the get envelope is not dumped at the user',
			!!(r.smoke_search && r.smoke_search.rawEnvelopeLeak === false),
			JSON.stringify(r.smoke_search && r.smoke_search.rawEnvelopeLeak)
		);
		ok(
			'connected: the links chevron traverses to neighbours',
			!!(r.smoke_search && r.smoke_search.linkedRows > 0),
			JSON.stringify(r.smoke_search)
		);
		ok(
			'connected: merge removed a pending card',
			!!(r.smoke_merge && r.smoke_merge.after < r.smoke_merge.before),
			JSON.stringify(r.smoke_merge)
		);
		ok(
			'connected: the promote modal opens with node types and a prefilled title',
			!!(r.smoke_promote && r.smoke_promote.modalOpened && r.smoke_promote.nodeTypes > 1),
			JSON.stringify(r.smoke_promote)
		);
		ok(
			'connected: promote actually writes (the inbox card goes away)',
			!!(r.smoke_promote && r.smoke_promote.modalClosed && r.smoke_promote.after < r.smoke_promote.before),
			JSON.stringify(r.smoke_promote)
		);
		ok(
			'connected: promote also works on an item with no scope (the chooser branch)',
			!!(
				r.smoke_promote2 &&
				r.smoke_promote2.modalOpened &&
				r.smoke_promote2.hasScopeChooser &&
				r.smoke_promote2.after < r.smoke_promote2.before
			),
			JSON.stringify(r.smoke_promote2)
		);
		// NOT covered: Discard. It confirms through a native dialog.showMessageBox,
		// which blocks the main process and cannot be driven from the renderer.
		// Verify it by hand.
		ok('connected: the application menu built', !!(r.smoke_menu && r.smoke_menu.built), JSON.stringify(r.smoke_menu));
		ok(
			'connected: nav, refresh and reconnect accelerators are registered',
			!!(
				r.smoke_menu &&
				r.smoke_menu.accelerators.includes('CmdOrCtrl+1') &&
				r.smoke_menu.accelerators.includes('CmdOrCtrl+R') &&
				r.smoke_menu.accelerators.includes('CmdOrCtrl+Shift+R')
			),
			JSON.stringify(r.smoke_menu && r.smoke_menu.accelerators)
		);
	} finally {
		stub.kill();
	}
}

async function scenarioEmpty() {
	console.log('\n# empty (stub with zero fixtures: reachable, authorized, nothing stored)');
	const stub = startStub(EMPTY_STUB_PORT, true);
	try {
		if (!(await waitForStub(EMPTY_STUB_PORT))) {
			ok('empty: stub came up', false);
			return;
		}
		const r = await runApp({
			url: 'http://127.0.0.1:' + EMPTY_STUB_PORT + '/mcp/',
			token: 'stub-token',
			space: 'fresh',
			shot: WANT_SHOTS ? path.join(ROOT, 'docs', 'state-empty-PANEL.png') : undefined,
			panels: 'confirm,stats',
		});
		if (!assertNeverBroken('empty', r)) {
			return;
		}
		ok('empty: still reads as connected', r.smoke_report.healthPhase === 'connected', 'phase=' + r.smoke_report.healthPhase);
		ok('empty: confirm shows empty states, not errors', r.smoke_report.confirmEmpty > 0, JSON.stringify(r.smoke_report));
		ok('empty: confirm shows no cards', r.smoke_report.confirmCards === 0);
		ok('empty: confirm shows no recovery block', r.smoke_report.confirmRecovery === 0);
		ok(
			'empty: Memories says "No memories yet" rather than looking broken',
			!!(r.smoke_search && /no memories yet/i.test(r.smoke_search.autoStateTitle || '')),
			'stateTitle=' + (r.smoke_search && r.smoke_search.autoStateTitle)
		);
		ok('empty: stats rendered no tiles', r.smoke_report.statsTiles === 0, 'tiles=' + r.smoke_report.statsTiles);
		ok(
			'empty: stats shows an empty state, not an error or a recovery block',
			r.smoke_report.statsRecovery === 0 && !r.smoke_report.errorText,
			JSON.stringify({ recovery: r.smoke_report.statsRecovery, error: r.smoke_report.errorText })
		);
		ok('empty: search reports no matches rather than failing', !!(r.smoke_search && r.smoke_search.state), JSON.stringify(r.smoke_search));
		ok('empty: explorer did not go blank', r.smoke_search && r.smoke_search.blank === false);
	} finally {
		stub.kill();
	}
}

async function scenarioUnauthorized() {
	console.log('\n# unauthorized (LIVE Engraphy server, no token)');
	if (!(await isUp(LIVE_URL + '/healthz'))) {
		console.log('  skip - no live server at ' + LIVE_URL + ' (start Docker to cover this state)');
		return;
	}
	const r = await runApp({
		url: LIVE_URL + '/mcp/',
		token: '',
		space: '',
		shot: WANT_SHOTS ? path.join(ROOT, 'docs', 'state-unauthorized-PANEL.png') : undefined,
		panels: 'confirm',
		graphError: true,
	});
	if (!assertNeverBroken('unauthorized', r)) {
		return;
	}
	// The whole point: /healthz answers 200 unauthenticated, so the badge must
	// NOT go green off it, and the copy must not say "start a server".
	ok(
		'unauthorized: badge is unauthorized, not connected',
		r.smoke_report.healthPhase === 'unauthorized',
		'phase=' + r.smoke_report.healthPhase + ' text=' + r.smoke_report.health
	);
	ok('unauthorized: badge names the token', /token/i.test(r.smoke_report.health || ''), 'text=' + r.smoke_report.health);
	ok('unauthorized: banner is shown', !!r.smoke_report.banner);
	ok(
		'unauthorized: banner blames the token, not the server being down',
		/token/i.test(r.smoke_report.banner || '') && !/cannot reach/i.test(r.smoke_report.banner || ''),
		'banner=' + r.smoke_report.banner
	);
	ok('unauthorized: confirm shows the recovery block', r.smoke_report.confirmRecovery > 0);

	// A graph build that cannot even start must END, visibly, on something the
	// user can act on. The failure mode being ruled out is a spinner that spins
	// forever because the error never reached the panel.
	const ge = r.smoke_graph_error || {};
	ok('unauthorized: the graph panel still offers a build', ge.buildOffered === true);
	ok('unauthorized: a failed build stops spinning', ge.stillSpinning === false, JSON.stringify(ge));
	ok('unauthorized: a failed build shows an error', ge.errorShown === true, 'title=' + ge.errorTitle);
	ok('unauthorized: a failed build offers a retry', ge.retryOffered === true);
	ok(
		'unauthorized: explorer does NOT claim "no results"',
		!!(r.smoke_search && !/no match/i.test(r.smoke_search.state || '') && r.smoke_search.rows === 0),
		JSON.stringify(r.smoke_search)
	);
	ok(
		'unauthorized: explorer names the token problem',
		!!(r.smoke_search && /token/i.test(r.smoke_search.state || '')),
		'state=' + (r.smoke_search && r.smoke_search.state)
	);
}

async function scenarioUnreachable() {
	console.log('\n# unreachable (dead port)');
	const r = await runApp({
		url: 'http://127.0.0.1:' + DEAD_PORT + '/mcp/',
		token: 'whatever',
		space: '',
		shot: WANT_SHOTS ? path.join(ROOT, 'docs', 'state-unreachable-PANEL.png') : undefined,
		panels: 'confirm',
	});
	if (!assertNeverBroken('unreachable', r)) {
		return;
	}
	ok('unreachable: badge is unreachable', r.smoke_report.healthPhase === 'unreachable', 'phase=' + r.smoke_report.healthPhase);
	ok('unreachable: banner is shown', !!r.smoke_report.banner);
	ok('unreachable: confirm shows the recovery block', r.smoke_report.confirmRecovery > 0);
	ok('unreachable: confirm rendered no half-state cards', r.smoke_report.confirmCards === 0);
	ok(
		'unreachable: explorer explains the connection, not "no results"',
		!!(r.smoke_search && /reach|listening|server/i.test(r.smoke_search.state || '')),
		'state=' + (r.smoke_search && r.smoke_search.state)
	);
}

async function scenarioUnconfigured() {
	console.log('\n# unconfigured (no server URL at all)');
	const r = await runApp({
		url: '',
		token: '',
		space: '',
		shot: WANT_SHOTS ? path.join(ROOT, 'docs', 'state-unconfigured-PANEL.png') : undefined,
		panels: 'confirm',
	});
	if (!assertNeverBroken('unconfigured', r)) {
		return;
	}
	ok('unconfigured: badge says no server set', r.smoke_report.healthPhase === 'unconfigured', 'phase=' + r.smoke_report.healthPhase);
	ok('unconfigured: banner is shown', !!r.smoke_report.banner);
	ok('unconfigured: confirm shows the connect block', r.smoke_report.confirmRecovery > 0);
	ok('unconfigured: stats shows the connect block', r.smoke_report.statsRecovery > 0);
	ok(
		'unconfigured: explorer offers setup rather than failing',
		!!(r.smoke_search && /connect/i.test(r.smoke_search.state || '')),
		'state=' + (r.smoke_search && r.smoke_search.state)
	);
}

async function scenarioOnboarding() {
	console.log('\n# onboarding (fresh profile, first run)');
	const stub = startStub(STUB_PORT, false);
	try {
		if (!(await waitForStub(STUB_PORT))) {
			ok('onboarding: stub came up', false);
			return;
		}
		const r = await runApp({
			url: 'http://127.0.0.1:' + STUB_PORT + '/mcp/',
			token: 'stub-token',
			space: 'demo',
			onboarding: true,
			shot: WANT_SHOTS ? path.join(ROOT, 'docs', 'onboarding-PANEL.png') : undefined,
			panels: 'onboarding',
		});
		if (!assertNeverBroken('onboarding', r)) {
			return;
		}
		ok('onboarding: the overlay opened on first run', r.smoke_report.onboardingOpen === true, JSON.stringify(r.smoke_report));
		ok(
			'onboarding: it opens on the welcome step',
			/welcome/i.test(r.smoke_report.onboardingTitle || ''),
			'title=' + r.smoke_report.onboardingTitle
		);
		ok('onboarding: three steps are offered', r.smoke_report.onboardingSteps === 3, 'dots=' + r.smoke_report.onboardingSteps);
		// It must not reappear on every launch: a returning user gets the app.
		const again = await runApp({
			url: 'http://127.0.0.1:' + STUB_PORT + '/mcp/',
			token: 'stub-token',
			space: 'demo',
		});
		ok(
			'onboarding: does NOT reopen once completed',
			!!(again.smoke_report && again.smoke_report.onboardingOpen === false),
			JSON.stringify(again.smoke_report && again.smoke_report.onboardingOpen)
		);
	} finally {
		stub.kill();
	}
}

async function scenarioLoading() {
	console.log('\n# loading (slow server: the skeletons must actually appear)');
	const LOADING_PORT = Number(process.env.SMOKE_LOADING_PORT || 8012);
	// Hold every tool call long enough that the harness can look mid-flight.
	const stub = startStub(LOADING_PORT, false, 6000);
	try {
		if (!(await waitForStub(LOADING_PORT))) {
			ok('loading: stub came up', false);
			return;
		}
		const r = await runApp({
			url: 'http://127.0.0.1:' + LOADING_PORT + '/mcp/',
			token: 'stub-token',
			space: 'demo',
			loading: true,
		});
		ok('loading: the app reported mid-flight', !!r.smoke_loading, JSON.stringify(r.smoke_loading));
		if (!r.smoke_loading) {
			return;
		}
		ok('loading: the window is not blank while waiting', r.smoke_loading.blank === false);
		ok(
			'loading: the confirm queue shows skeletons, not an empty panel',
			r.smoke_loading.confirmSkeletons > 0,
			JSON.stringify(r.smoke_loading)
		);
		ok(
			'loading: no cards are rendered yet',
			r.smoke_loading.confirmCards === 0,
			JSON.stringify(r.smoke_loading)
		);
	} finally {
		stub.kill();
	}
}

/**
 * The real thing: a LIVE Engraphy server with a VALID readwrite token.
 *
 * This exists because the gap it closes caused a real bug report. Every other
 * scenario proves the app against the dev stub or against failure conditions;
 * none of them proved that a genuine authorized read renders. Skipped unless
 * ENGRAPHY_LIVE_TOKEN is set, so no secret ever lives in the repo:
 *
 *   ENGRAPHY_LIVE_TOKEN=<token> npm run smoke -- --only=live
 */
async function scenarioLive() {
	console.log('\n# live (a REAL Engraphy server with a valid token)');
	const token = process.env.ENGRAPHY_LIVE_TOKEN;
	if (!token) {
		console.log('  skip - set ENGRAPHY_LIVE_TOKEN to run this against a real server');
		return;
	}
	if (!(await isUp(LIVE_URL + '/healthz'))) {
		console.log('  skip - no live server at ' + LIVE_URL);
		return;
	}
	const r = await runApp({
		url: LIVE_URL + '/mcp/',
		token: token,
		space: process.env.ENGRAPHY_LIVE_SPACE || '',
		shot: WANT_SHOTS ? path.join(ROOT, 'docs', 'state-live-PANEL.png') : undefined,
		panels: 'explorer',
	});
	if (!assertNeverBroken('live', r)) {
		return;
	}
	ok('live: the badge is green off an AUTHENTICATED read', r.smoke_report.healthPhase === 'connected', 'phase=' + r.smoke_report.healthPhase);
	ok('live: no disconnected banner', !r.smoke_report.banner, 'banner=' + r.smoke_report.banner);
	ok(
		'live: Memories auto-lists real nodes on open',
		!!(r.smoke_search && r.smoke_search.autoRows > 0),
		'autoRows=' + (r.smoke_search && r.smoke_search.autoRows)
	);
	ok(
		'live: opening a real record renders title and body',
		!!(r.smoke_search && r.smoke_search.recordRendered && r.smoke_search.recordBody > 0),
		JSON.stringify(r.smoke_search)
	);
	ok(
		'live: the raw get envelope is not dumped at the user',
		!!(r.smoke_search && r.smoke_search.rawEnvelopeLeak === false)
	);
	ok('live: stats rendered against real data', r.smoke_report.statsTiles > 0, 'tiles=' + r.smoke_report.statsTiles);
	console.log('  info - live rows on open: ' + (r.smoke_search && r.smoke_search.autoRows) +
		', first: ' + JSON.stringify(r.smoke_search && r.smoke_search.first));
}

/**
 * The graph index against a REAL server, end to end.
 *
 * Kept separate from `live` and off by default because it is MINUTES long: the
 * server allows 60 reads a minute and the whole-graph index costs a few hundred
 * reads (graphHarvest.ts explains why it cannot be fewer). Assertions read
 * cytoscape's OWN element counts, not the DOM, so "the rail rendered" cannot
 * stand in for "the graph laid out".
 *
 *   ENGRAPHY_LIVE_TOKEN=<token> npm run smoke -- --only=graph
 */
async function scenarioGraph() {
	console.log('\n# graph (index a REAL server and render it)');
	const token = process.env.ENGRAPHY_LIVE_TOKEN;
	if (!token) {
		console.log('  skip - set ENGRAPHY_LIVE_TOKEN to run this against a real server');
		return;
	}
	if (!(await isUp(LIVE_URL + '/healthz'))) {
		console.log('  skip - no live server at ' + LIVE_URL);
		return;
	}
	const budgetMs = Number(process.env.ENGRAPHY_SMOKE_GRAPH_MS || 420000);
	const r = await runApp({
		url: LIVE_URL + '/mcp/',
		token: token,
		space: process.env.ENGRAPHY_LIVE_SPACE || '',
		graph: true,
		loadingShot: WANT_SHOTS ? path.join(ROOT, 'docs', 'graph-building-STAGE.png') : undefined,
		graphBudgetMs: budgetMs,
		timeoutMs: budgetMs + 120000,
		// No finished-graph shot here: docs/graph-{dark,light}.png are curated at a
		// chosen zoom, and a second auto-written copy of the same panel only went
		// stale next to them. What this scenario uniquely captures is the LOADING
		// state, which is what loadingShot writes.
	});
	if (!assertNeverBroken('graph', r)) {
		return;
	}
	const g = r.smoke_graph;
	if (!g) {
		ok('graph: the panel reported', false, 'no ENGRAPHY_SMOKE_GRAPH line');
		return;
	}
	ok('graph: cytoscape + fcose loaded under the CSP', g.libs === true);
	ok('graph: the idle state offers a build', g.idleShown === true);

	// The regression these guard: the progress card was painted into a subtree
	// that render() had just hidden, so the whole 3.5-minute first index showed
	// an empty panel. `cardPresent` alone would have passed; `cardVisible` is the
	// assertion that would have failed.
	const L = g.loading || {};
	ok('graph: a progress card is VISIBLE during the first index', L.cardVisible === true, JSON.stringify(L));
	ok('graph: the spinner is visible while indexing', L.spinnerVisible === true);
	ok('graph: the progress card says what it is doing', !!L.label && L.label !== 'Starting…', 'label=' + L.label);
	ok('graph: progress reports discovered counts', /memories/.test(L.sub || '') && /elapsed/.test(L.sub || ''), 'sub=' + L.sub);
	ok('graph: the index can be cancelled from the card', L.cancelPresent === true);
	ok('graph: the progress bar never goes backwards', g.barWentBackwards === false);
	ok('graph: the bar actually advanced', (g.barReached || 0) > 20, 'reached ' + g.barReached + '%');
	ok('graph: a rate-limit pause is explained, not silent', g.sawRateLimitNotice === true);
	ok('graph: the progress card is gone once the graph lands', g.cardGoneWhenDone === true);
	const cy = g.cy || {};
	ok('graph: memories are on the canvas', cy.memories > 0, 'memories=' + cy.memories);
	ok('graph: links are on the canvas', cy.links > 0, 'links=' + cy.links);
	ok(
		'graph: every memory sits inside a scope cluster',
		cy.memories > 0 && cy.parented === cy.memories,
		'parented=' + cy.parented + ' of ' + cy.memories
	);
	ok('graph: scopes render as separate clusters', cy.scopeClusters > 1, 'clusters=' + cy.scopeClusters);
	ok(
		'graph: every node carries a readable label',
		cy.memories > 0 && cy.labelled === cy.memories,
		'labelled=' + cy.labelled + ' of ' + cy.memories
	);
	ok(
		'graph: the layout actually placed the nodes',
		cy.memories > 0 && cy.laidOut === cy.memories,
		'laidOut=' + cy.laidOut + ' of ' + cy.memories
	);
	ok('graph: the scope filter lists every cluster', g.scopeRows === cy.scopeClusters,
		'rows=' + g.scopeRows + ' clusters=' + cy.scopeClusters);
	ok('graph: the kind legend is populated', g.typeRows > 0, 'typeRows=' + g.typeRows);
	ok('graph: the relationship legend is populated', g.edgeRows > 0, 'edgeRows=' + g.edgeRows);
	const gc = r.smoke_graph_controls || {};
	// The regression this guards: the deep sweep was reachable only from the
	// first-run block, so it vanished the moment a graph existed.
	ok('graph: the deep sweep is still reachable once a graph exists', gc.sweepReachableWithGraph === true);
	ok('graph: the deep sweep defaults off', gc.sweepDefaultsOff === true);
	ok('graph: the deep sweep toggles', gc.sweepTogglesOn === true && gc.sweepTogglesOff === true);
	ok('graph: clearing the index returns to the build screen', gc.idleAfterClear === true, JSON.stringify(gc));
	ok('graph: the sweep survives clearing too', gc.sweepStillReachable === true);
	console.log('  info - ' + g.statusText);
}

/**
 * Regenerate docs/graph-{dark,light}.png against the dev stub, not a real
 * space. graph-{dark,light}.png are otherwise curated by hand at a chosen
 * zoom (see scenarioGraph's comment): this exists so that curation can start
 * from synthetic fixture data instead of a live server, whenever the images
 * need to be redone. Not part of the default run and not a state check;
 * `--shots` is required or it does nothing.
 *
 *   npm run smoke -- --shots --only=graphDocShots
 */
async function scenarioGraphDocShots() {
	console.log('\n# graphDocShots (regenerate graph-{dark,light}.png against the stub)');
	if (!WANT_SHOTS) {
		console.log('  skip - pass --shots');
		return;
	}
	const stub = startStub(STUB_PORT, false);
	try {
		if (!(await waitForStub(STUB_PORT))) {
			console.log('  skip - stub did not start');
			return;
		}
		const base = {
			url: 'http://127.0.0.1:' + STUB_PORT + '/mcp/',
			token: 'stub-token',
			space: 'demo',
			graph: true,
			graphBudgetMs: 30000,
			timeoutMs: 60000,
		};
		await runApp({ ...base, doneShot: path.join(ROOT, 'docs', 'graph-dark.png') });
		await runApp({ ...base, doneShot: path.join(ROOT, 'docs', 'graph-light.png'), lightForce: true });
		console.log('  wrote docs/graph-dark.png and docs/graph-light.png from the stub');
	} finally {
		stub.kill();
	}
}

async function scenarioSettingsRoundTrip() {
	console.log('\n# settings round-trip (token through the OS keychain)');
	const stub = startStub(STUB_PORT, false);
	try {
		if (!(await waitForStub(STUB_PORT))) {
			ok('settings: stub came up', false);
			return;
		}
		process.env.ENGRAPHY_SMOKE_SETTINGS = '1';
		process.env.ENGRAPHY_SMOKE_SAVE_URL = 'http://127.0.0.1:' + STUB_PORT + '/mcp/';
		const r = await runApp({ url: 'http://127.0.0.1:' + STUB_PORT + '/mcp/', token: 'stub-token', space: 'demo' });
		delete process.env.ENGRAPHY_SMOKE_SETTINGS;
		ok('settings: save reported success', !!(r.smoke_settings && r.smoke_settings.savedOk), JSON.stringify(r.smoke_settings));
		ok('settings: a token is stored after save', !!(r.smoke_settings && r.smoke_settings.hasToken));
		ok(
			'settings: the renderer never receives the token value',
			!!(r.smoke_settings && r.smoke_settings.hasToken === true && r.smoke_settings.tokenInsecure === false),
			JSON.stringify(r.smoke_settings)
		);
		ok('settings: the raw token never appears in output', !/smoke-secret-token/.test(r.raw));
	} finally {
		stub.kill();
	}
}

// ---- run -------------------------------------------------------------------

const ALL = {
	connected: scenarioConnected,
	empty: scenarioEmpty,
	unauthorized: scenarioUnauthorized,
	unreachable: scenarioUnreachable,
	unconfigured: scenarioUnconfigured,
	onboarding: scenarioOnboarding,
	loading: scenarioLoading,
	live: scenarioLive,
	graph: scenarioGraph,
	graphDocShots: scenarioGraphDocShots,
	settings: scenarioSettingsRoundTrip,
};

/**
 * Scenarios the default run SKIPS, runnable only by name.
 *
 * `graph` indexes the whole graph, which costs a few hundred reads and takes
 * minutes. Worse for a suite, it drains the token's 60-reads-per-minute budget,
 * so whatever scenario runs next against the same live server gets
 * RATE_LIMITED, which is precisely how it broke `live`'s three read assertions
 * when it was in the default set. A minutes-long scenario that starves its
 * neighbours belongs behind an explicit `--only=graph`.
 *
 * `graphDocShots` writes doc screenshots and asserts nothing, so it has no
 * place in a state-check run either; it also does nothing at all unless
 * `--shots` is passed.
 */
const OPT_IN = new Set(['graph', 'graphDocShots']);

(async () => {
	if (!PACKAGED && !fs.existsSync(path.join(ROOT, 'out', 'main.js'))) {
		console.error('out/main.js is missing. Run `npm run build` first.');
		process.exit(1);
	}
	if (PACKAGED) {
		console.log('driving the PACKAGED app: ' + packagedExe());
	}
	const names = ONLY ? ONLY.split(',') : Object.keys(ALL).filter((n) => !OPT_IN.has(n));
	if (!ONLY) {
		console.log('(skipping: ' + [...OPT_IN].join(', ') + ' — run with --only=<name>)');
	}
	for (const name of names) {
		const fn = ALL[name];
		if (!fn) {
			console.log('unknown scenario: ' + name);
			continue;
		}
		await fn();
	}
	console.log('\n' + checks + ' state checks, ' + failures + ' failed.');
	process.exit(failures > 0 ? 1 : 0);
})();
