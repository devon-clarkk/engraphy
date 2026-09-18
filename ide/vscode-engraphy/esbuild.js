// Bundle the extension (and its bundled deps, incl. the MCP SDK) into a single
// CJS file the VS Code extension host loads. `vscode` is provided by the host,
// so it stays external. Bundling means no runtime node_modules ships in the .vsix.

const esbuild = require('esbuild');

const production = process.argv.includes('--production');
const watch = process.argv.includes('--watch');

async function main() {
	const ctx = await esbuild.context({
		entryPoints: ['src/extension.ts'],
		bundle: true,
		format: 'cjs',
		platform: 'node',
		target: 'node20',
		outfile: 'out/extension.js',
		external: ['vscode'],
		sourcemap: !production,
		minify: production,
		logLevel: 'info',
	});
	if (watch) {
		await ctx.watch();
	} else {
		await ctx.rebuild();
		await ctx.dispose();
	}
}

main().catch((e) => {
	console.error(e);
	process.exit(1);
});
