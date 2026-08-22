#!/usr/bin/env node
// Rasterize the Engraphy loop mark (media/loop.svg) to the 128x128 PNG the
// VS Code Marketplace requires (media/icon.png). Tries the rasterizers commonly
// present on a dev box, in order. No hard dependency on any single tool.
//
//   node scripts/make-icon.js
//
// If none are found, install one of: librsvg (rsvg-convert), ImageMagick
// (magick/convert), Inkscape, or python `cairosvg`.

'use strict';

const cp = require('child_process');
const fs = require('fs');
const path = require('path');

const mediaDir = path.join(__dirname, '..', 'media');
const svg = path.join(mediaDir, 'loop.svg');
const png = path.join(mediaDir, 'icon.png');
const SIZE = 128;

if (!fs.existsSync(svg)) {
	console.error(`Source SVG not found: ${svg}`);
	process.exit(1);
}

function have(bin) {
	const probe = process.platform === 'win32' ? 'where' : 'which';
	const r = cp.spawnSync(probe, [bin], { stdio: 'ignore' });
	return r.status === 0;
}

function run(cmd, args) {
	const r = cp.spawnSync(cmd, args, { stdio: 'inherit' });
	return r.status === 0;
}

const candidates = [
	{ bin: 'rsvg-convert', args: ['-w', `${SIZE}`, '-h', `${SIZE}`, svg, '-o', png] },
	{ bin: 'magick', args: ['-background', 'none', '-density', '384', svg, '-resize', `${SIZE}x${SIZE}`, png] },
	{ bin: 'convert', args: ['-background', 'none', '-density', '384', svg, '-resize', `${SIZE}x${SIZE}`, png] },
	{ bin: 'inkscape', args: [svg, '--export-type=png', `--export-filename=${png}`, `--export-width=${SIZE}`, `--export-height=${SIZE}`] },
	{ bin: 'cairosvg', args: [svg, '-o', png, '-W', `${SIZE}`, '-H', `${SIZE}`] },
	// python cairosvg module (common on Windows where cairosvg is not a PATH binary)
	{ bin: 'python', args: ['-c', `import cairosvg; cairosvg.svg2png(url=r'${svg}', write_to=r'${png}', output_width=${SIZE}, output_height=${SIZE})`] },
];

for (const c of candidates) {
	if (have(c.bin)) {
		console.log(`Rasterizing with ${c.bin} ...`);
		if (run(c.bin, c.args) && fs.existsSync(png)) {
			console.log(`Wrote ${png}`);
			process.exit(0);
		}
		console.warn(`${c.bin} failed; trying next.`);
	}
}

console.error(
	'No usable SVG rasterizer found. Install one of: librsvg (rsvg-convert), ' +
		'ImageMagick (magick/convert), Inkscape, or python cairosvg, then re-run ' +
		'`node scripts/make-icon.js`.'
);
process.exit(1);
