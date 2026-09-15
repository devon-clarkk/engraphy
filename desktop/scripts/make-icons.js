// Generate the app icons from the Engraphy loop mark.
//
// Outputs:
//   build/icon.png              1024x1024, the master. electron-builder derives
//                               the macOS .icns from it during a Mac build.
//   build/icon.ico              multi-resolution Windows icon (256 down to 16)
//   src/renderer/assets/icon.png 512x512, the in-app window/taskbar icon
//
// Run: npm run make-icons
//
// Two things this does that a naive "draw the path in the middle" does not:
//
// 1. OPTICAL CENTRING. The brand path's ink is not centred in its own viewBox.
//    Measured from the rendered alpha channel, the ink occupies
//    x 14.75..84.5, y 15.5..73.75, so its centre is (49.5, 44.5), 5.5 units
//    ABOVE the geometric centre. Centring the viewBox (what the first version
//    did) therefore parked the mark visibly high in the tile. The measurement is
//    redone at build time rather than hardcoded, so it stays correct if the path
//    is ever revised.
//
// 2. STROKE COMPENSATION AT SMALL SIZES. The mark is a monoline. At a 16px
//    favicon the brand stroke-width of 8 lands near one physical pixel and the
//    loop turns into a faint smudge, so the small rasters are drawn with a
//    heavier stroke. This is standard practice for monoline marks and keeps the
//    silhouette readable in a taskbar.
//
// Brand: Verdant #4C7A59 loop on a Cream #F3F1E6 rounded square, per
// Engraphy-design/brand/brand-guidelines.md §1 (the loop is the ONLY primary
// mark; recolour only within the green ramp; no glow, gradient, or shadow).

const fs = require('fs');
const path = require('path');
const { Resvg } = require('@resvg/resvg-js');
const pngToIco = require('png-to-ico');

// The primary mark path, verbatim from brand-guidelines.md §1.
const MARK_PATH = 'M35 66 C6 46 19 16 50 20 C82 24 93 55 62 68 C44 75 34 59 50 50';
const VERDANT = '#4C7A59';
const CREAM = '#F3F1E6';

/**
 * Fraction of the tile the mark's LONGEST side spans.
 *
 * The guidelines ask for clear space around the loop equal to the height of its
 * inner opening. 0.9 crowded the rounded corners; 0.72 leaves roughly that much
 * breathing room on the long axis while still filling far more of the tile than
 * the first pass (which sat at about half the tile and read as a small sticker).
 */
const FILL = 0.72;
/** Corner radius as a fraction of the tile. */
const RADIUS = 22;

function markOnly(strokeWidth) {
	return (
		'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">' +
		'<path d="' + MARK_PATH + '" fill="none" stroke="' + VERDANT + '" stroke-width="' + strokeWidth + '"' +
		' stroke-linecap="round" stroke-linejoin="round"/></svg>'
	);
}

/**
 * Measure where the mark's ink actually lands, by rendering it large and
 * scanning the alpha channel. Returns viewBox-space bounds.
 */
function measureInk(strokeWidth) {
	const SAMPLE = 400;
	const img = new Resvg(markOnly(strokeWidth), { fitTo: { mode: 'width', value: SAMPLE } }).render();
	const px = img.pixels;
	let minX = Infinity;
	let minY = Infinity;
	let maxX = -1;
	let maxY = -1;
	for (let y = 0; y < img.height; y++) {
		for (let x = 0; x < img.width; x++) {
			if (px[(y * img.width + x) * 4 + 3] > 10) {
				if (x < minX) minX = x;
				if (x > maxX) maxX = x;
				if (y < minY) minY = y;
				if (y > maxY) maxY = y;
			}
		}
	}
	const s = 100 / SAMPLE;
	return {
		width: (maxX - minX + 1) * s,
		height: (maxY - minY + 1) * s,
		cx: ((minX + maxX) / 2) * s,
		cy: ((minY + maxY) / 2) * s,
	};
}

/** The full icon tile at a given raster size. */
function iconSvg(size) {
	// Below ~48px a stroke of 8 renders near one physical pixel and the loop
	// stops reading. Thicken it for the small rasters only.
	const strokeWidth = size <= 32 ? 11 : size <= 64 ? 9.5 : 8;
	const ink = measureInk(strokeWidth);
	const scale = (FILL * 100) / Math.max(ink.width, ink.height);
	// Scale about the INK centre, then place that centre at the tile centre.
	const transform =
		'translate(50 50) scale(' + scale.toFixed(4) + ') translate(' + (-ink.cx).toFixed(3) + ' ' + (-ink.cy).toFixed(3) + ')';
	return (
		'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="' + size + '" height="' + size + '">' +
		'<rect x="0" y="0" width="100" height="100" rx="' + RADIUS + '" fill="' + CREAM + '"/>' +
		'<g transform="' + transform + '">' +
		'<path d="' + MARK_PATH + '" fill="none" stroke="' + VERDANT + '" stroke-width="' + strokeWidth + '"' +
		' stroke-linecap="round" stroke-linejoin="round"/>' +
		'</g></svg>'
	);
}

function render(size) {
	return Buffer.from(
		new Resvg(iconSvg(size), { fitTo: { mode: 'width', value: size } }).render().asPng()
	);
}

const buildDir = path.join(__dirname, '..', 'build');
fs.mkdirSync(buildDir, { recursive: true });

// Master PNG. 1024 so a macOS build has a real 512@2x layer to derive.
fs.writeFileSync(path.join(buildDir, 'icon.png'), render(1024));
console.log('wrote build/icon.png (1024x1024)');

// In-app window / taskbar icon.
fs.writeFileSync(path.join(__dirname, '..', 'src', 'renderer', 'assets', 'icon.png'), render(512));
console.log('wrote src/renderer/assets/icon.png (512x512)');

async function makeIco() {
	const sizes = [256, 128, 64, 48, 32, 16];
	const buffers = sizes.map((s) => render(s));
	fs.writeFileSync(path.join(buildDir, 'icon.ico'), await pngToIco(buffers));
	console.log('wrote build/icon.ico (' + sizes.join(', ') + ')');
}

makeIco().catch((e) => {
	console.error(e);
	process.exit(1);
});
