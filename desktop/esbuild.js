// Bundle the Electron main process, the preload script, and the dev stub server
// each into a single CommonJS file under out/.
//
// Why bundle instead of `tsc`: @modelcontextprotocol/sdk@1.30.0 is ESM-only
// ("type":"module" with subpath exports). A plain tsc->CJS main process would
// throw ERR_REQUIRE_ESM at require() time. esbuild inlines the SDK into CJS the
// same way the VS Code extension does, so we ship no runtime node_modules and
// electron-builder's `files` stays trivial (out/**/* + package.json).
//
// `electron` is provided by the runtime, so it stays external. The stub server
// is bundled too (it uses the SDK server side) but is excluded from packaged
// builds via the electron-builder `files` glob.

const esbuild = require('esbuild');

const production = process.argv.includes('--production');
const watch = process.argv.includes('--watch');
const stubOnly = process.argv.includes('--stub');

/** @type {import('esbuild').BuildOptions} */
const common = {
	bundle: true,
	format: 'cjs',
	platform: 'node',
	target: 'node20',
	external: ['electron'],
	sourcemap: !production,
	minify: production,
	logLevel: 'info',
};

const targets = stubOnly
	? [{ entryPoints: ['stub/stub-server.ts'], outfile: 'out/stub-server.js' }]
	: [
			{ entryPoints: ['src/main/main.ts'], outfile: 'out/main.js' },
			{ entryPoints: ['src/preload/preload.ts'], outfile: 'out/preload.js' },
			{ entryPoints: ['stub/stub-server.ts'], outfile: 'out/stub-server.js' },
		];

async function main() {
	const contexts = await Promise.all(
		targets.map((t) => esbuild.context({ ...common, ...t }))
	);
	if (watch) {
		await Promise.all(contexts.map((c) => c.watch()));
		// Keep the process alive in watch mode.
	} else {
		await Promise.all(
			contexts.map(async (c) => {
				await c.rebuild();
				await c.dispose();
			})
		);
	}
}

main().catch((e) => {
	console.error(e);
	process.exit(1);
});
