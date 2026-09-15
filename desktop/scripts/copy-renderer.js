// Copy the renderer tree (HTML/CSS/JS/assets) verbatim into out/renderer so the
// packaged app and dev run load from a single out/ directory. The renderer is
// plain HTML/CSS/JS (no bundling) exactly like the VS Code extension's media/,
// so a recursive copy is all that is needed.
//
// Plus one guard, explained below.

const fs = require('fs');
const path = require('path');

const src = path.join(__dirname, '..', 'src', 'renderer');
const dest = path.join(__dirname, '..', 'out', 'renderer');

/**
 * Fail the build if a shipped SVG is not well-formed.
 *
 * This exists because of a real bug that shipped silently. The brand loop mark
 * is painted as a CSS mask so it can follow --brand-mark per theme, and
 * assets/loop-mark.svg carried a comment that mentioned the CSS custom property
 * by name, leading hyphens included. A double hyphen is ILLEGAL inside an XML
 * comment, so the file was not well-formed XML and Chromium refused to parse it
 * as an image. A CSS mask whose image fails to load paints nothing at all, and
 * emits no console error, so the sidebar logo and every large mark in the empty
 * and recovery states rendered completely invisible, in dev and in the packaged
 * app, with no symptom other than "the logo is missing".
 *
 * The check is deliberately narrow: it catches the trap that actually bit, at
 * build time, rather than pulling in an XML parser.
 */
function assertSvgWellFormed(file) {
	const svg = fs.readFileSync(file, 'utf8');
	const comments = svg.match(/<!--[\s\S]*?-->/g) || [];
	for (const c of comments) {
		const inner = c.slice(4, -3);
		if (inner.includes('--')) {
			throw new Error(
				'Malformed XML comment in ' +
					path.relative(process.cwd(), file) +
					': an XML comment may not contain "--", so this file will not parse as an image ' +
					'and any CSS mask using it will paint invisible. Offending comment:\n' +
					c.trim()
			);
		}
	}
	if (!/<svg[\s>]/.test(svg)) {
		throw new Error('Not an SVG: ' + path.relative(process.cwd(), file));
	}
}

/**
 * Fail the build if a renderer script will not parse.
 *
 * Same class of trap as the SVG check above, and it bit for real: a bad escape
 * in views/graph.js made the file a syntax error, and because index.html loads
 * the views as plain <script> tags in sequence, the parse failure took out
 * app.js with it. The window still opened, the title bar still drew, and three
 * panels that had nothing to do with the change silently rendered empty — with
 * the only evidence in a devtools console nobody had open. `new Function` parses
 * without executing, which is exactly the check wanted here: no DOM, no
 * electron, no side effects.
 */
function assertRendererScriptParses(file) {
	const code = fs.readFileSync(file, 'utf8');
	try {
		new Function(code);
	} catch (e) {
		throw new Error(
			'Syntax error in ' +
				path.relative(process.cwd(), file) +
				': the renderer loads views as plain scripts, so this would take out ' +
				'every script after it and blank the panels with no visible error.\n  ' +
				String((e && e.message) || e)
		);
	}
}

for (const dir of [src, path.join(src, 'views')]) {
	for (const name of fs.readdirSync(dir)) {
		if (name.toLowerCase().endsWith('.js')) {
			assertRendererScriptParses(path.join(dir, name));
		}
	}
}

const assetsDir = path.join(src, 'assets');
for (const name of fs.readdirSync(assetsDir)) {
	if (name.toLowerCase().endsWith('.svg')) {
		assertSvgWellFormed(path.join(assetsDir, name));
	}
}

fs.rmSync(dest, { recursive: true, force: true });
fs.cpSync(src, dest, { recursive: true });

/**
 * Vendor the graph libraries out of node_modules into out/renderer/vendor.
 *
 * They are copied at BUILD time rather than committed under src/renderer for the
 * same reason the MCP SDK is a devDependency (see package.json's "comment:deps"):
 * the version lives in package.json, and the packaged app ships only what is in
 * out/. They MUST be local files — index.html's CSP is `script-src 'self'` with
 * `connect-src 'none'`, so a CDN <script> would be blocked outright.
 *
 * Order matters at load time and is encoded in index.html: each of these is a
 * UMD bundle that, in a browser, reads its dependency off a global that the
 * previous file defined (layoutBase → coseBase → cytoscapeFcose).
 */
const VENDOR = [
	['cytoscape', 'dist/cytoscape.min.js'],
	['layout-base', 'layout-base.js'],
	['cose-base', 'cose-base.js'],
	['cytoscape-fcose', 'cytoscape-fcose.js'],
];
const vendorDir = path.join(dest, 'vendor');
fs.mkdirSync(vendorDir, { recursive: true });
for (const [pkg, rel] of VENDOR) {
	const from = path.join(__dirname, '..', 'node_modules', pkg, rel);
	if (!fs.existsSync(from)) {
		throw new Error(
			`Missing graph dependency ${pkg} (${rel}). Run \`npm install\` — the Graph ` +
				'panel cannot load from a CDN because the renderer CSP is script-src \'self\'.'
		);
	}
	fs.copyFileSync(from, path.join(vendorDir, path.basename(rel)));
}

console.log(
	`copied renderer → ${path.relative(process.cwd(), dest)} (+${VENDOR.length} vendored libs)`
);
