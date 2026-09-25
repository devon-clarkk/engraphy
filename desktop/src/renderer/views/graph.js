// Engraphy graph viewer — the whole memory graph as a graph.
//
// The Memories panel answers "find me a memory". This answers the question a
// list cannot: what SHAPE is my memory, and how do its scopes relate? So the
// three things it must get right are the three a flat list has no way to show —
// labels you can read, edges you can inspect, and scopes that occupy visibly
// separate regions rather than blurring into one blob.
//
// Rendering is cytoscape.js with the fcose layout, both vendored locally by
// scripts/copy-renderer.js. They MUST be local: index.html's CSP is
// `script-src 'self'` / `connect-src 'none'`, so a CDN script would be blocked
// and the renderer cannot fetch anything itself. Every byte of data arrives over
// the existing IPC channel, same as every other panel.
//
// SCOPES ARE COMPOUND PARENTS, not a colour convention. Each scope becomes a
// cytoscape parent node holding its memories, so the layout engine itself is
// what pushes the 16 scopes into separate labelled regions and keeps them apart
// — rather than us hand-tuning per-scope forces and hoping. Parents auto-size to
// their children, so a scope's area is literally how much memory it holds.
//
// All server strings go through textContent or cytoscape's own data binding;
// nothing here touches innerHTML.
window.mountGraph = function (ctx) {
	'use strict';
	const host = ctx.host;
	const root = ctx.root;
	const S = window.ENGRAPHY_STATES;
	const el = S.el;

	// fcose ships as a cytoscape extension that has to be registered once.
	let layoutName = 'cose';
	try {
		if (window.cytoscape && window.cytoscapeFcose) {
			window.cytoscape.use(window.cytoscapeFcose);
			layoutName = 'fcose';
		}
	} catch (e) {
		// Registering twice throws; either way `cose` is a working fallback.
		layoutName = window.cytoscapeFcose ? 'fcose' : 'cose';
	}

	// ---- palette -----------------------------------------------------------
	//
	// Cytoscape needs literal colour strings, not CSS variables, so the theme is
	// mirrored here and re-applied when the shell flips `body.vscode-dark`.
	const PALETTES = {
		light: {
			nodeText: '#2a3626',
			mutedText: '#7c806a',
			edge: '#b9b7a2',
			edgeLabel: '#6b6f5c',
			scopeFill: 'rgba(76,122,89,0.05)',
			scopeLine: '#cfd3bd',
			scopeText: '#5a6b52',
			selected: '#2a3626',
			types: {
				note: '#4c7a59',
				project: '#2f6f8f',
				preference: '#b58a2e',
				strategy: '#7a4c9e',
				club: '#c2603a',
				unit: '#2f8f7a',
				person: '#a8476b',
				concept: '#5c7fa8',
			},
			typeFallback: '#7c806a',
			edges: { relates_to: '#b9b7a2', references: '#7fa8bf', supersedes: '#c2837a' },
		},
		dark: {
			nodeText: '#e8eae0',
			mutedText: '#9aa08c',
			edge: '#4a5245',
			edgeLabel: '#9aa08c',
			scopeFill: 'rgba(107,167,124,0.06)',
			scopeLine: '#3c4539',
			scopeText: '#9fbfa4',
			selected: '#e8eae0',
			types: {
				note: '#6ba77c',
				project: '#5aa3c9',
				preference: '#d3ab55',
				strategy: '#a781c9',
				club: '#e08a63',
				unit: '#54bfa6',
				person: '#d1799a',
				concept: '#8fb0d6',
			},
			typeFallback: '#9aa08c',
			edges: { relates_to: '#4a5245', references: '#4d7c93', supersedes: '#8f5c54' },
		},
	};
	const isDark = () => document.body.classList.contains('vscode-dark');
	const pal = () => (isDark() ? PALETTES.dark : PALETTES.light);
	const typeColor = (t) => pal().types[t] || pal().typeFallback;
	const edgeColor = (t) => pal().edges[t] || pal().edge;

	// Every scope gets its OWN hue, so the 16 regions read apart at a glance even
	// where two clusters end up adjacent. Hues are walked by the golden angle
	// (137.5°) rather than divided evenly, which keeps neighbouring INDEXES far
	// apart in colour — and the scope order is stable (biggest first, id as the
	// tie-break), so a scope keeps its colour between rebuilds.
	let scopeHues = new Map();
	function assignScopeHues(scopes, nodes) {
		scopeHues = new Map();
		scopeTallies(scopes, nodes).forEach((s, i) => {
			scopeHues.set(s.id, (i * 137.508) % 360);
		});
	}
	/**
	 * COMMA-separated hsl() on purpose. Cytoscape parses colours itself rather
	 * than handing them to the browser, and its parser predates the space-
	 * separated CSS Color 4 syntax: `hsl(120 45% 55%)` is not recognised and the
	 * property silently falls back to its default. That is what made the first
	 * build paint sixteen identically grey clusters despite every scope having
	 * been assigned its own hue — the HTML chips (real CSS) were coloured
	 * correctly the whole time, which is what gave the mismatch away.
	 */
	function hsl(h, s_, l) {
		return 'hsl(' + Math.round(h) + ', ' + s_ + '%, ' + l + '%)';
	}
	function scopeColor(id, kind) {
		const h = scopeHues.has(id) ? scopeHues.get(id) : 120;
		if (kind === 'line') {
			return isDark() ? hsl(h, 42, 48) : hsl(h, 45, 58);
		}
		if (kind === 'text') {
			return isDark() ? hsl(h, 45, 70) : hsl(h, 50, 30);
		}
		return isDark() ? hsl(h, 48, 55) : hsl(h, 58, 48);
	}

	// ---- view state --------------------------------------------------------
	let state = null;
	let cy = null;
	/** Scope ids the user has switched OFF. */
	const hiddenScopes = new Set();
	/** Node types the user has switched OFF. */
	const hiddenTypes = new Set();
	let labelMode = 'auto'; // auto | always | off
	let focusMode = true; // dim everything outside a selected node's neighbourhood
	let query = '';
	let selectedId = null;
	let deepSweep = false;
	/** Set once per snapshot so a re-render (filter, theme) does not re-layout. */
	let renderedBuiltAt = null;

	// ---- chrome ------------------------------------------------------------

	const shell = el('div', 'graph-shell');
	const side = el('aside', 'graph-side');
	const stage = el('div', 'graph-stage');
	const inspector = el('aside', 'graph-inspector hidden');
	shell.appendChild(side);
	shell.appendChild(stage);
	shell.appendChild(inspector);

	const toolbar = el('div', 'graph-toolbar');
	const search = document.createElement('input');
	search.type = 'search';
	search.placeholder = 'Highlight memories…';
	search.setAttribute('aria-label', 'Highlight memories by title');
	const labelSel = document.createElement('select');
	labelSel.setAttribute('aria-label', 'Label visibility');
	for (const [v, t] of [
		['auto', 'Labels: on zoom'],
		['always', 'Labels: always'],
		['off', 'Labels: off'],
	]) {
		const o = document.createElement('option');
		o.value = v;
		o.textContent = t;
		labelSel.appendChild(o);
	}
	const focusBtn = el('button', 'btn btn-ghost graph-toggle on', 'Focus links');
	focusBtn.title = 'When a memory is selected, dim everything it is not linked to';

	// The deep-sweep switch lives HERE, in the toolbar, and not only in the
	// first-run block. It used to be a checkbox inside the idle state, which
	// renders only while there is no snapshot — so the moment one existed (which
	// is from the first build onwards, since the cache loads on open) the control
	// was gone for good and Rebuild could only ever post deepSweep:false. The
	// sweep was documented as opt-in while being, in practice, unreachable.
	const sweepBox = el('label', 'graph-sweep-toggle');
	const sweepInput = document.createElement('input');
	sweepInput.type = 'checkbox';
	sweepBox.title =
		'Also sweep with search when rebuilding. Finds memories that no link ' +
		'reaches, but counts towards the numbers on Impact & usage.';
	sweepBox.appendChild(sweepInput);
	sweepBox.appendChild(el('span', null, 'Deep sweep'));

	toolbar.appendChild(search);
	toolbar.appendChild(labelSel);
	toolbar.appendChild(focusBtn);
	toolbar.appendChild(sweepBox);
	stage.appendChild(toolbar);

	/** One source of truth for the sweep, mirrored into both checkboxes. */
	let deepSweepBoxes = [sweepInput];
	function setDeepSweep(on) {
		deepSweep = !!on;
		for (const box of deepSweepBoxes) {
			box.checked = deepSweep;
		}
	}
	sweepInput.addEventListener('change', () => setDeepSweep(sweepInput.checked));

	const canvas = el('div', 'graph-canvas');
	stage.appendChild(canvas);

	// Scope names are drawn as HTML over the canvas, not as cytoscape labels.
	//
	// A cytoscape label is painted INSIDE the scene graph, so its size scales with
	// zoom: at the zoom that fits 229 memories on screen, a 13px scope name
	// renders around 4px and the clusters become anonymous grey boxes. These divs
	// sit in screen space at a fixed size, so every scope stays named at every
	// zoom — which is the whole point of clustering by scope.
	const scopeLayer = el('div', 'graph-scope-layer');
	canvas.appendChild(scopeLayer);

	const overlay = el('div', 'graph-overlay hidden');
	stage.appendChild(overlay);

	const status = el('div', 'graph-status');
	stage.appendChild(status);

	// ---- side rail ---------------------------------------------------------

	const scopeBox = el('div', 'graph-rail-section');
	const typeBox = el('div', 'graph-rail-section');
	const edgeBox = el('div', 'graph-rail-section');
	side.appendChild(scopeBox);
	side.appendChild(typeBox);
	side.appendChild(edgeBox);

	function railHeader(box, title, actionLabel, onAction) {
		const h = el('div', 'graph-rail-head');
		h.appendChild(el('h2', null, title));
		if (actionLabel) {
			const b = el('button', 'graph-rail-action', actionLabel);
			b.addEventListener('click', onAction);
			h.appendChild(b);
		}
		box.appendChild(h);
	}

	function renderRail(snap) {
		const nodes = snap ? snap.nodes : [];
		const edges = snap ? snap.edges : [];

		// --- scopes ---
		scopeBox.textContent = '';
		const allOn = hiddenScopes.size === 0;
		railHeader(scopeBox, 'Scopes', allOn ? 'Only…' : 'Show all', () => {
			hiddenScopes.clear();
			applyFilters();
			renderRail(snap);
		});
		const tallies = scopeTallies(snap ? snap.scopes : [], nodes);
		for (const s of tallies) {
			if (!s.count) {
				continue;
			}
			const row = el('label', 'graph-rail-row');
			const cb = document.createElement('input');
			cb.type = 'checkbox';
			cb.checked = !hiddenScopes.has(s.id);
			cb.addEventListener('change', () => {
				if (cb.checked) {
					hiddenScopes.delete(s.id);
				} else {
					hiddenScopes.add(s.id);
				}
				applyFilters();
			});
			const swatch = el('span', 'graph-scope-dot');
			swatch.style.borderColor = scopeColor(s.id, 'line');
			swatch.style.background = scopeColor(s.id, 'fill');
			row.appendChild(cb);
			row.appendChild(swatch);
			const name = el('span', 'graph-rail-name', s.displayName);
			name.title = s.id + (s.ambient ? ' (ambient)' : '');
			row.appendChild(name);
			row.appendChild(el('span', 'graph-rail-count', String(s.count)));
			scopeBox.appendChild(row);
		}

		// --- node types ---
		typeBox.textContent = '';
		railHeader(typeBox, 'Kinds', hiddenTypes.size ? 'Show all' : '', () => {
			hiddenTypes.clear();
			applyFilters();
			renderRail(snap);
		});
		for (const t of typeTallies(nodes)) {
			const row = el('label', 'graph-rail-row');
			const cb = document.createElement('input');
			cb.type = 'checkbox';
			cb.checked = !hiddenTypes.has(t.type);
			cb.addEventListener('change', () => {
				if (cb.checked) {
					hiddenTypes.delete(t.type);
				} else {
					hiddenTypes.add(t.type);
				}
				applyFilters();
			});
			const dot = el('span', 'graph-type-dot');
			dot.style.background = typeColor(t.type);
			row.appendChild(cb);
			row.appendChild(dot);
			row.appendChild(el('span', 'graph-rail-name', t.type));
			row.appendChild(el('span', 'graph-rail-count', String(t.count)));
			typeBox.appendChild(row);
		}

		// --- relationships (legend only: edges follow their endpoints) ---
		edgeBox.textContent = '';
		railHeader(edgeBox, 'Relationships', '', null);
		for (const t of edgeTypeTallies(edges)) {
			const row = el('div', 'graph-rail-row static');
			const line = el('span', 'graph-edge-swatch');
			line.style.background = edgeColor(t.type);
			if (t.type === 'supersedes') {
				line.classList.add('dashed');
			}
			row.appendChild(line);
			row.appendChild(el('span', 'graph-rail-name', t.type));
			row.appendChild(el('span', 'graph-rail-count', String(t.count)));
			edgeBox.appendChild(row);
		}
	}

	// ---- tallies (mirrors graphModel.ts; the renderer gets plain data) ------

	function scopeTallies(scopes, nodes) {
		const counts = new Map();
		for (const n of nodes) {
			counts.set(n.scope, (counts.get(n.scope) || 0) + 1);
		}
		const known = new Map((scopes || []).map((s) => [s.id, s]));
		const ids = new Set([].concat([...counts.keys()], [...known.keys()]));
		const out = [];
		for (const id of ids) {
			const s = known.get(id) || { id: id, displayName: id, ambient: false };
			out.push({
				id: id,
				displayName: s.displayName || id,
				ambient: !!s.ambient,
				count: counts.get(id) || 0,
			});
		}
		out.sort((a, b) => b.count - a.count || (a.id < b.id ? -1 : 1));
		return out;
	}
	function typeTallies(nodes) {
		const counts = new Map();
		for (const n of nodes) {
			counts.set(n.type, (counts.get(n.type) || 0) + 1);
		}
		return [...counts.entries()]
			.map(([type, count]) => ({ type: type, count: count }))
			.sort((a, b) => b.count - a.count || (a.type < b.type ? -1 : 1));
	}
	function edgeTypeTallies(edges) {
		const counts = new Map();
		for (const e of edges) {
			counts.set(e.type, (counts.get(e.type) || 0) + 1);
		}
		return [...counts.entries()]
			.map(([type, count]) => ({ type: type, count: count }))
			.sort((a, b) => b.count - a.count || (a.type < b.type ? -1 : 1));
	}

	// ---- cytoscape ---------------------------------------------------------

	function shortLabel(title) {
		const t = String(title || '(untitled)');
		return t.length > 46 ? t.slice(0, 44) + '…' : t;
	}

	function elementsFor(snap) {
		assignScopeHues(snap.scopes, snap.nodes);
		const out = [];
		const used = new Set(snap.nodes.map((n) => n.scope));
		const byId = new Map((snap.scopes || []).map((s) => [s.id, s]));
		for (const id of used) {
			const s = byId.get(id);
			out.push({
				group: 'nodes',
				data: {
					id: 'scope:' + id,
					kind: 'scope',
					scopeId: id,
					label: (s && s.displayName) || id,
				},
			});
		}
		const degree = new Map();
		for (const e of snap.edges) {
			degree.set(e.src, (degree.get(e.src) || 0) + 1);
			degree.set(e.dst, (degree.get(e.dst) || 0) + 1);
		}
		for (const n of snap.nodes) {
			out.push({
				group: 'nodes',
				data: {
					id: n.id,
					parent: 'scope:' + n.scope,
					kind: 'memory',
					label: shortLabel(n.title),
					title: n.title,
					type: n.type,
					scope: n.scope,
					author: n.author || '',
					createdAt: n.createdAt || '',
					deg: degree.get(n.id) || 0,
				},
			});
		}
		for (const e of snap.edges) {
			out.push({
				group: 'edges',
				data: {
					id: e.src + '|' + e.type + '|' + e.dst,
					source: e.src,
					target: e.dst,
					label: e.type,
					type: e.type,
				},
			});
		}
		return out;
	}

	function stylesheet() {
		const p = pal();
		return [
			{
				// Scope clusters, each in its own hue. The label sits ABOVE the box
				// rather than inside it so it never collides with the memories the box
				// contains.
				//
				// `background-opacity` is a SEPARATE property in cytoscape and the
				// alpha channel of an rgba() background-color is ignored — passing a
				// 6%-alpha green here with opacity 1 painted 16 solid slabs of colour
				// that swamped the nodes inside them. The tint has to come from the
				// opacity property.
				selector: 'node[kind = "scope"]',
				style: {
					'background-color': (n) => scopeColor(n.data('scopeId'), 'fill'),
					'background-opacity': isDark() ? 0.17 : 0.13,
					'border-width': 2,
					'border-style': 'solid',
					'border-color': (n) => scopeColor(n.data('scopeId'), 'line'),
					'border-opacity': 1,
					shape: 'round-rectangle',
					padding: 30,
					// No in-scene label: the name is an HTML overlay (see scopeLayer).
					label: '',
					'z-compound-depth': 'bottom',
				},
			},
			{
				selector: 'node[kind = "memory"]',
				style: {
					'background-color': (n) => typeColor(n.data('type')),
					// Degree-scaled, so the hubs a graph exists to reveal are the ones
					// that read as hubs.
					width: (n) => 22 + Math.min(30, Math.sqrt(n.data('deg') || 0) * 9),
					height: (n) => 22 + Math.min(30, Math.sqrt(n.data('deg') || 0) * 9),
					'border-width': 1.5,
					'border-color': (n) => scopeColor(n.data('scope'), 'line'),
					shape: (n) => (n.data('type') === 'note' ? 'ellipse' : 'round-diamond'),
					label: 'data(label)',
					color: p.nodeText,
					'font-size': 11,
					'text-valign': 'bottom',
					'text-halign': 'center',
					'text-margin-y': 4,
					'text-wrap': 'wrap',
					'text-max-width': 150,
					'text-background-color': isDark() ? '#191d18' : '#f3f1e6',
					'text-background-opacity': 0.8,
					'text-background-padding': 2,
					'text-background-shape': 'roundrectangle',
					// Cytoscape hides a label once its ON-SCREEN size falls below this,
					// which is what keeps 229 titles from becoming a grey smear at
					// overview zoom. Titles resolve as you zoom in; 'always' overrides.
					'min-zoomed-font-size': labelMode === 'always' ? 0 : 6,
				},
			},
			{
				selector: 'edge',
				style: {
					width: 1.4,
					'line-color': (e) => edgeColor(e.data('type')),
					'line-style': (e) => (e.data('type') === 'supersedes' ? 'dashed' : 'solid'),
					'curve-style': 'bezier',
					'target-arrow-shape': (e) => (e.data('type') === 'relates_to' ? 'none' : 'triangle'),
					'target-arrow-color': (e) => edgeColor(e.data('type')),
					'arrow-scale': 0.8,
					opacity: 0.8,
				},
			},
			{
				// The relationship name only appears close-up, so 443 edges do not
				// print 443 words over the canvas at overview zoom.
				selector: 'edge[label]',
				style: {
					label: 'data(label)',
					'font-size': 7,
					color: p.edgeLabel,
					'text-rotation': 'autorotate',
					'min-zoomed-font-size': 11,
					'text-background-color': isDark() ? '#191d18' : '#f3f1e6',
					'text-background-opacity': 0.8,
					'text-background-padding': 1,
				},
			},
			{ selector: 'node.hidden, edge.hidden', style: { display: 'none' } },
			{ selector: '.dim', style: { opacity: 0.08, 'text-opacity': 0 } },
			{
				selector: 'node.hit',
				style: {
					'border-width': 3,
					'border-color': p.selected,
					'text-opacity': 1,
					'min-zoomed-font-size': 0,
					'z-index': 20,
				},
			},
			{
				selector: 'node:selected',
				style: {
					'border-width': 3,
					'border-color': p.selected,
					'min-zoomed-font-size': 0,
					'font-size': 11,
					'z-index': 30,
				},
			},
			{
				selector: 'edge.hot',
				style: {
					width: 2.4,
					opacity: 1,
					'min-zoomed-font-size': 0,
					'z-index': 25,
				},
			},
			{ selector: 'node[kind = "memory"].labels-off', style: { label: '' } },
		];
	}

	function layoutOptions() {
		// The knobs that matter for "scopes read as separate areas": compound
		// gravity pulls each scope's children into its own parent, nodeSeparation
		// keeps the parents from touching, and packComponents stops the graph's
		// disconnected pieces from being flung far apart.
		if (layoutName === 'fcose') {
			return {
				name: 'fcose',
				quality: 'proof',
				animate: false,
				randomize: true,
				fit: true,
				padding: 40,
				// These four are what turn "one blob" into "sixteen readable regions".
				// gravityCompound pulls each scope's memories tightly into their own
				// parent; nodeRepulsion and nodeSeparation then push the parents apart
				// hard enough that cross-scope edges cannot drag two clusters into
				// overlap, which is exactly what a weaker repulsion produced.
				nodeSeparation: 300,
				idealEdgeLength: (e) =>
					e.source().parent().id() === e.target().parent().id() ? 55 : 260,
				nodeRepulsion: 40000,
				gravity: 0.05,
				gravityCompound: 6,
				gravityRangeCompound: 2.5,
				nestingFactor: 0.08,
				numIter: 4000,
				packComponents: true,
				tile: true,
				tilingPaddingVertical: 18,
				tilingPaddingHorizontal: 18,
			};
		}
		return { name: 'cose', animate: false, fit: true, padding: 40, nestingFactor: 0.6 };
	}

	function ensureCy(snap) {
		const fresh = !cy || renderedBuiltAt !== snap.builtAt;
		if (!fresh) {
			return false;
		}
		if (cy) {
			cy.destroy();
			cy = null;
		}
		cy = window.cytoscape({
			container: canvas,
			elements: elementsFor(snap),
			style: stylesheet(),
			wheelSensitivity: 0.2,
			minZoom: 0.06,
			maxZoom: 3,
			// Compound parents are layout regions, not things to drag around.
			autoungrabify: false,
			boxSelectionEnabled: false,
		});
		cy.nodes('[kind = "scope"]').ungrabify();
		// Exposed for scripts/smoke.js only: the harness asserts on cytoscape's own
		// element counts rather than on the DOM, so a canvas that laid out nothing
		// cannot pass by having the right rail rendered.
		window.__engraphyCy = cy;
		wireEvents();
		renderedBuiltAt = snap.builtAt;
		cy.layout(layoutOptions()).run();
		rebuildScopeChips();
		return true;
	}

	// ---- scope label overlay -----------------------------------------------

	/** One div per scope cluster, kept over its bounding box in screen space. */
	const scopeChips = new Map();
	let scopeFrame = 0;

	function rebuildScopeChips() {
		scopeLayer.textContent = '';
		scopeChips.clear();
		if (!cy) {
			return;
		}
		cy.nodes('[kind = "scope"]').forEach((p) => {
			const id = p.data('scopeId');
			const chip = el('button', 'graph-scope-chip');
			chip.style.borderColor = scopeColor(id, 'line');
			chip.style.color = scopeColor(id, 'text');
			chip.appendChild(el('span', 'graph-scope-chip-name', p.data('label')));
			chip.appendChild(el('span', 'graph-scope-chip-count', String(p.children().length)));
			chip.title = id + ' — click to frame this scope';
			chip.addEventListener('click', () => {
				cy.animate({ fit: { eles: p, padding: 60 }, duration: 240 });
			});
			scopeLayer.appendChild(chip);
			scopeChips.set(p.id(), chip);
		});
		positionScopeChips();
	}

	function positionScopeChips() {
		if (!cy) {
			return;
		}
		const w = canvas.clientWidth;
		const h = canvas.clientHeight;
		cy.nodes('[kind = "scope"]').forEach((p) => {
			const chip = scopeChips.get(p.id());
			if (!chip) {
				return;
			}
			if (p.hasClass('hidden')) {
				chip.classList.add('hidden');
				return;
			}
			const bb = p.renderedBoundingBox();
			// A cluster scrolled off-screen keeps its chip out of the way rather than
			// piling every off-screen name into the corner.
			if (bb.x2 < 0 || bb.y2 < 0 || bb.x1 > w || bb.y1 > h) {
				chip.classList.add('hidden');
				return;
			}
			chip.classList.remove('hidden');
			chip.style.left = Math.round(Math.max(2, Math.min(w - 40, bb.x1))) + 'px';
			chip.style.top = Math.round(Math.max(2, bb.y1 - 20)) + 'px';
			chip.style.maxWidth = Math.max(90, Math.round(bb.w)) + 'px';
		});
	}

	function scheduleScopeChips() {
		if (scopeFrame) {
			return;
		}
		scopeFrame = requestAnimationFrame(() => {
			scopeFrame = 0;
			positionScopeChips();
		});
	}

	function wireEvents() {
		cy.on('tap', 'node[kind = "memory"]', (ev) => selectNode(ev.target));
		cy.on('tap', 'node[kind = "scope"]', (ev) => {
			// Tapping a scope frames it — the fastest way to read one cluster.
			cy.animate({ fit: { eles: ev.target, padding: 60 }, duration: 220 });
		});
		cy.on('tap', 'edge', (ev) => showEdge(ev.target));
		cy.on('tap', (ev) => {
			if (ev.target === cy) {
				clearSelection();
			}
		});
		cy.on('mouseover', 'edge', (ev) => ev.target.addClass('hot'));
		cy.on('mouseout', 'edge', (ev) => {
			if (!ev.target.selected()) {
				ev.target.removeClass('hot');
			}
		});
		cy.on('mouseover', 'node[kind = "memory"]', (ev) => ev.target.addClass('hit'));
		cy.on('mouseout', 'node[kind = "memory"]', (ev) => {
			if (ev.target.id() !== selectedId) {
				ev.target.removeClass('hit');
			}
		});
		cy.on('pan zoom resize', scheduleScopeChips);
		cy.on('layoutstop', () => {
			positionScopeChips();
		});
	}

	// ---- filtering / highlighting -----------------------------------------

	function applyFilters() {
		if (!cy) {
			return;
		}
		cy.batch(() => {
			cy.nodes('[kind = "memory"]').forEach((n) => {
				const off = hiddenScopes.has(n.data('scope')) || hiddenTypes.has(n.data('type'));
				n.toggleClass('hidden', off);
			});
			// A scope box with nothing left in it is noise, so it goes too.
			cy.nodes('[kind = "scope"]').forEach((p) => {
				const kids = p.children().filter((c) => !c.hasClass('hidden'));
				p.toggleClass('hidden', kids.length === 0);
			});
			cy.edges().forEach((e) => {
				e.toggleClass(
					'hidden',
					e.source().hasClass('hidden') || e.target().hasClass('hidden')
				);
			});
		});
		applyHighlight();
		positionScopeChips();
		renderStatus();
	}

	function applyHighlight() {
		if (!cy) {
			return;
		}
		const q = query.trim().toLowerCase();
		cy.batch(() => {
			cy.elements().removeClass('dim');
			cy.nodes('[kind = "memory"]').removeClass('hit');
			if (q) {
				const hits = cy
					.nodes('[kind = "memory"]')
					.filter((n) => String(n.data('title') || '').toLowerCase().includes(q));
				if (hits.length) {
					hits.addClass('hit');
					cy.elements().not(hits).not(hits.connectedEdges()).addClass('dim');
					cy.nodes('[kind = "scope"]').removeClass('dim');
				}
			} else if (selectedId && focusMode) {
				const n = cy.getElementById(selectedId);
				if (n && n.length) {
					const keep = n.closedNeighborhood();
					cy.elements().not(keep).addClass('dim');
					cy.nodes('[kind = "scope"]').removeClass('dim');
					n.addClass('hit');
				}
			}
		});
	}

	function selectNode(n) {
		selectedId = n.id();
		cy.nodes().unselect();
		n.select();
		applyHighlight();
		showNode(n);
	}

	function clearSelection() {
		selectedId = null;
		if (cy) {
			cy.nodes().unselect();
		}
		applyHighlight();
		inspector.classList.add('hidden');
	}

	// ---- inspector ---------------------------------------------------------

	function inspectorShell(title, subtitle) {
		inspector.textContent = '';
		inspector.classList.remove('hidden');
		const head = el('div', 'graph-inspector-head');
		head.appendChild(el('h2', null, title));
		if (subtitle) {
			head.appendChild(el('p', 'graph-inspector-sub', subtitle));
		}
		const close = el('button', 'graph-inspector-close', '×');
		close.title = 'Close';
		close.setAttribute('aria-label', 'Close details');
		close.addEventListener('click', clearSelection);
		head.appendChild(close);
		inspector.appendChild(head);
		return inspector;
	}

	function metaRow(label, value) {
		const row = el('div', 'graph-meta-row');
		row.appendChild(el('span', 'graph-meta-key', label));
		row.appendChild(el('span', 'graph-meta-val', value));
		return row;
	}

	function showEdge(edge) {
		selectedId = null;
		const src = cy.getElementById(edge.data('source'));
		const dst = cy.getElementById(edge.data('target'));
		inspectorShell('Relationship', edge.data('type'));
		const body = el('div', 'graph-inspector-body');
		body.appendChild(metaRow('From', src.data('title') || edge.data('source')));
		body.appendChild(metaRow('Link', edge.data('type')));
		body.appendChild(metaRow('To', dst.data('title') || edge.data('target')));
		const jump = el('div', 'graph-inspector-actions');
		const a = el('button', 'btn btn-secondary', 'Open source');
		a.addEventListener('click', () => selectNode(src));
		const b = el('button', 'btn btn-secondary', 'Open target');
		b.addEventListener('click', () => selectNode(dst));
		jump.appendChild(a);
		jump.appendChild(b);
		body.appendChild(jump);
		inspector.appendChild(body);
		cy.edges().removeClass('hot');
		edge.addClass('hot');
	}

	function showNode(n) {
		inspectorShell(n.data('title'), n.data('type') + ' · ' + n.data('scope'));
		const body = el('div', 'graph-inspector-body');
		inspector.appendChild(body);

		const bodySlot = el('div', 'graph-node-body');
		bodySlot.appendChild(S.spinnerNote('Reading the full record…'));

		if (n.data('author')) {
			body.appendChild(metaRow('Author', n.data('author')));
		}
		if (n.data('createdAt')) {
			body.appendChild(metaRow('Created', String(n.data('createdAt')).slice(0, 10)));
		}
		body.appendChild(metaRow('Links', String(n.data('deg'))));
		body.appendChild(bodySlot);

		// Neighbours, so the inspector is a way to WALK the graph, not just read it.
		const nbrs = n.connectedEdges().filter((e) => !e.hasClass('hidden'));
		if (nbrs.length) {
			body.appendChild(el('h3', 'graph-inspector-h3', 'Linked memories'));
			const list = el('div', 'graph-neighbour-list');
			nbrs.forEach((e) => {
				const other = e.source().id() === n.id() ? e.target() : e.source();
				const row = el('button', 'graph-neighbour');
				const rel = el('span', 'graph-neighbour-rel', e.data('type'));
				rel.style.color = edgeColor(e.data('type'));
				row.appendChild(rel);
				row.appendChild(el('span', 'graph-neighbour-title', other.data('title') || other.id()));
				row.addEventListener('click', () => {
					selectNode(other);
					cy.animate({ center: { eles: other }, duration: 200 });
				});
				list.appendChild(row);
			});
			body.appendChild(list);
		}

		const wantId = n.id();
		host.invoke({ type: 'get', id: wantId }).then(
			(res) => {
				if (selectedId !== wantId) {
					return;
				}
				bodySlot.textContent = '';
				if (!res || !res.ok) {
					bodySlot.appendChild(
						el('p', 'graph-node-body-error', 'Could not read the full record.')
					);
					return;
				}
				const text = res.node && (res.node.body || res.node.text);
				bodySlot.appendChild(el('p', 'graph-node-text', text || '(no body)'));
			},
			() => {
				if (selectedId === wantId) {
					bodySlot.textContent = '';
				}
			}
		);
	}

	// ---- status line -------------------------------------------------------

	function renderStatus() {
		status.textContent = '';
		if (!state || !state.snapshot) {
			return;
		}
		const snap = state.snapshot;
		const shownNodes = cy ? cy.nodes('[kind = "memory"]').not('.hidden').length : snap.nodes.length;
		const shownEdges = cy ? cy.edges().not('.hidden').length : snap.edges.length;
		const scopes = new Set(snap.nodes.map((n) => n.scope)).size;
		const filtered = shownNodes !== snap.nodes.length;

		const counts = el(
			'span',
			'graph-status-counts',
			filtered
				? `${shownNodes} of ${snap.nodes.length} memories · ${shownEdges} of ${snap.edges.length} links`
				: `${snap.nodes.length} memories · ${snap.edges.length} links · ${scopes} scopes`
		);
		status.appendChild(counts);

		const s = snap.stats || {};
		const reads = (s.briefingCalls || 0) + (s.traverseCalls || 0) + (s.searchCalls || 0);
		const when = snap.builtAt ? new Date(snap.builtAt) : null;
		const age = when && !isNaN(when.getTime()) ? when.toLocaleString() : 'unknown';
		const gaps = s.unreadableHubs || 0;
		const meta = el(
			'span',
			'graph-status-meta',
			`Indexed ${age} · ${reads} reads` +
				(s.deepSweep ? ' · deep sweep' : '') +
				(gaps ? ` · ${gaps} link list${gaps === 1 ? '' : 's'} incomplete` : '') +
				(state.fromCache ? ' · cached' : '')
		);
		meta.title =
			'The graph is assembled from many small reads and cached on disk, because ' +
			'Engraphy has no whole-graph tool and caps every read. Rebuild to re-index.' +
			(gaps
				? '\n\nSome memories have more links than one read can return, and their ' +
					'relationships could not be split any finer, so a few links are not drawn.'
				: '');

		const right = el('span', 'graph-status-right');
		right.appendChild(meta);
		const clear = el('button', 'graph-status-action', 'Clear index');
		clear.title =
			'Forget the cached graph and go back to the build screen. The memories ' +
			'themselves are untouched — this only drops the local index file.';
		clear.addEventListener('click', () => host.postMessage({ type: 'clearCache' }));
		right.appendChild(clear);
		status.appendChild(right);
	}

	// ---- states ------------------------------------------------------------

	function handlers() {
		return {
			onSetup: () => window.ENGRAPHY.showOnboarding(),
			onSettings: () => window.ENGRAPHY.navigate('settings'),
			onRetry: () => build(),
			onReconnect: () => window.ENGRAPHY.reconnect(),
		};
	}

	function showBlock(node) {
		shell.classList.add('hidden');
		blockHost.textContent = '';
		blockHost.appendChild(node);
		blockHost.classList.remove('hidden');
	}
	function showGraph() {
		blockHost.classList.add('hidden');
		shell.classList.remove('hidden');
	}

	const blockHost = el('div', 'graph-block-host');
	root.appendChild(blockHost);
	root.appendChild(shell);

	function showIdle() {
		const wrap = el('div', 'graph-intro');
		wrap.appendChild(
			S.stateBlock({
				kind: 'graph-idle',
				title: 'Draw your memory graph',
				message:
					'Engraphy stores memories as a graph, but the server hands them out one small ' +
					'read at a time — so the whole picture has to be indexed once, then cached. ' +
					'Indexing reads scope briefings and walks every link it finds.',
				actions: [{ label: 'Build the graph', kind: 'primary', onClick: () => build() }],
			})
		);
		const opt = el('label', 'graph-sweep-opt');
		const cb = document.createElement('input');
		cb.type = 'checkbox';
		// Registered so the toolbar switch and this one can never disagree; the
		// idle block is rebuilt on every render, so the old node is dropped first.
		deepSweepBoxes = [sweepInput, cb];
		cb.checked = deepSweep;
		cb.addEventListener('change', () => setDeepSweep(cb.checked));
		opt.appendChild(cb);
		opt.appendChild(
			el(
				'span',
				null,
				'Also sweep with search (finds memories that have no links yet, but counts ' +
					'towards the numbers on Impact & usage)'
			)
		);
		wrap.appendChild(opt);
		showBlock(wrap);
	}

	// ---- build progress -----------------------------------------------------
	//
	// WHERE THIS HAS TO MOUNT, and the bug that taught it. The progress card used
	// to live only in `overlay`, which sits inside `stage` inside `shell` — and
	// the no-snapshot branch of render() calls showBlock(), which HIDES `shell`.
	// So on a FIRST build, the one that takes three and a half minutes, the card
	// was painted into a hidden subtree and the panel showed an empty div: no
	// spinner, no bar, no text, for the entire index. A rebuild looked fine
	// because a snapshot exists then and the shell stays visible, which is
	// exactly the path every cached-profile test run took.
	//
	// The card is now built once and RE-PARENTED into whichever container is
	// actually on screen.

	let progressUi = null;
	let progressTimer = 0;
	/** When the last progress event arrived, so the local clock stays truthful. */
	let progressAt = 0;
	let lastProgress = null;
	/**
	 * The bar must never retreat. `closed / known` is the only walk estimate
	 * available and `known` grows as memories are discovered, so the raw ratio
	 * dips whenever a walk finds more than it closes. A bar that goes backwards
	 * reads as a fault.
	 */
	let maxFraction = 0;

	function buildProgressUi() {
		const card = el('div', 'graph-progress');
		const head = el('div', 'graph-progress-head');
		// The spinner is the part that matters most: it keeps moving through the
		// minute-long rate-limit waits, when no number on the card changes.
		head.appendChild(el('span', 'spinner'));
		const label = el('div', 'graph-progress-label', 'Starting…');
		head.appendChild(label);
		card.appendChild(head);

		const track = el('div', 'graph-progress-track');
		const bar = el('div', 'graph-progress-bar indeterminate');
		track.appendChild(bar);
		card.appendChild(track);

		const sub = el('div', 'graph-progress-sub', '');
		card.appendChild(sub);
		const wait = el('div', 'graph-progress-wait hidden');
		card.appendChild(wait);
		card.appendChild(
			el(
				'div',
				'graph-progress-note',
				'The server allows 60 reads a minute, so a first index takes a few minutes. ' +
					'It is cached afterwards, and this panel opens straight from that cache.'
			)
		);
		const cancel = el('button', 'btn btn-secondary', 'Cancel');
		cancel.addEventListener('click', () => host.postMessage({ type: 'cancel' }));
		card.appendChild(cancel);
		return { card: card, label: label, bar: bar, sub: sub, wait: wait };
	}

	function fmtDuration(ms) {
		const total = Math.max(0, Math.round(ms / 1000));
		const m = Math.floor(total / 60);
		const sec = total % 60;
		return m ? m + 'm ' + String(sec).padStart(2, '0') + 's' : sec + 's';
	}

	/** Repaint the parts that move on their own between server events. */
	function tickProgress() {
		if (!progressUi || !lastProgress) {
			return;
		}
		const since = Date.now() - progressAt;
		const elapsed = (lastProgress.elapsedMs || 0) + since;
		const left = (lastProgress.waitingMs || 0) - since;
		if (lastProgress.waitingMs && left > 0) {
			progressUi.wait.classList.remove('hidden');
			progressUi.wait.textContent =
				'Paused for the server read limit, resuming in ' + fmtDuration(left) + '.';
		} else {
			progressUi.wait.classList.add('hidden');
		}
		const p = lastProgress;
		progressUi.sub.textContent =
			p.nodes +
			' memories · ' +
			p.edges +
			' links · ' +
			p.calls +
			' reads · ' +
			fmtDuration(elapsed) +
			' elapsed';
	}

	/**
	 * Show the card, mounted in the right container for the current state.
	 * `inGraph` is true when a graph is already on screen (a rebuild), in which
	 * case the card floats over it; otherwise it is the whole panel.
	 */
	function showProgress(p, inGraph) {
		if (!progressUi) {
			progressUi = buildProgressUi();
		}
		const home = inGraph ? overlay : blockHost;
		if (progressUi.card.parentNode !== home) {
			home.textContent = '';
			home.appendChild(progressUi.card);
		}
		if (inGraph) {
			overlay.classList.remove('hidden');
		} else {
			shell.classList.add('hidden');
			blockHost.classList.remove('hidden');
			overlay.classList.add('hidden');
		}

		if (p) {
			lastProgress = p;
			progressAt = Date.now();
			progressUi.label.textContent = p.label || 'Indexing…';
			if (typeof p.fraction === 'number') {
				maxFraction = Math.max(maxFraction, Math.min(1, p.fraction));
				progressUi.bar.classList.remove('indeterminate');
				progressUi.bar.style.width = Math.round(Math.max(0.02, maxFraction) * 100) + '%';
			}
		} else {
			// The build has been asked for but the first event has not landed yet.
			// An indeterminate bar plus the spinner says "working" without claiming
			// a number nobody has measured.
			progressUi.label.textContent = 'Starting the index…';
			progressUi.bar.classList.add('indeterminate');
			progressUi.bar.style.width = '';
		}
		tickProgress();
		if (!progressTimer) {
			progressTimer = setInterval(tickProgress, 1000);
		}
	}

	function hideProgress() {
		overlay.classList.add('hidden');
		if (progressTimer) {
			clearInterval(progressTimer);
			progressTimer = 0;
		}
		lastProgress = null;
		maxFraction = 0;
		if (progressUi && progressUi.card.parentNode) {
			progressUi.card.parentNode.removeChild(progressUi.card);
		}
	}

	function build() {
		const strip = stage.querySelector('.graph-error-strip');
		if (strip) {
			strip.remove();
		}
		maxFraction = 0;
		lastProgress = null;
		// Paint the spinner NOW rather than waiting for main to echo `building`
		// back. That round trip is short, but "I clicked and nothing happened" is
		// the exact impression this fix exists to remove, so it is not left to
		// chance — and if a snapshot is on screen the card floats over it.
		showProgress(null, !!(state && state.snapshot));
		host.postMessage({ type: 'build', deepSweep: deepSweep });
	}

	// ---- render ------------------------------------------------------------

	function render() {
		if (!state) {
			showBlock(S.skeleton(1, 'wide'));
			return;
		}
		if (!state.serverConfigured) {
			showBlock(S.recoveryBlock({ kind: 'unconfigured' }, handlers(), { what: 'your graph' }));
			return;
		}
		if (state.error && !state.snapshot) {
			hideProgress();
			showBlock(
				S.stateBlock({
					tone: 'error',
					title: 'The graph could not be indexed',
					message: state.error,
					// A failed index must end somewhere the user can act, never on a
					// spinner that never resolves.
					actions: [{ label: 'Try again', kind: 'primary', onClick: () => build() }],
					detail: state.error,
				})
			);
			return;
		}
		if (!state.snapshot) {
			if (state.building) {
				// No graph yet: the card IS the panel. This is the 3.5-minute first
				// index, and the branch that used to render an empty div.
				showProgress(state.progress, false);
				return;
			}
			hideProgress();
			showIdle();
			return;
		}

		showGraph();
		const rebuilt = ensureCy(state.snapshot);
		if (rebuilt) {
			applyFilters();
		}
		renderRail(state.snapshot);
		renderStatus();
		if (state.building) {
			// A graph is already on screen, so the card floats over it and the old
			// graph stays readable while the new one indexes.
			showProgress(state.progress, true);
		} else {
			hideProgress();
			// A rebuild that failed leaves the previous graph up, which on its own
			// looks like nothing happened. Say what went wrong, above the canvas.
			if (state.error) {
				showBuildError(state.error);
			}
		}
	}

	/** Inline failure strip for a rebuild that had a graph to fall back on. */
	function showBuildError(message) {
		const strip = el('div', 'graph-error-strip');
		strip.setAttribute('role', 'alert');
		strip.appendChild(el('span', 'graph-error-text', 'Rebuild failed: ' + message));
		const retry = el('button', 'btn btn-secondary', 'Try again');
		retry.addEventListener('click', () => build());
		const dismiss = el('button', 'graph-error-dismiss', '×');
		dismiss.title = 'Dismiss';
		dismiss.setAttribute('aria-label', 'Dismiss');
		dismiss.addEventListener('click', () => strip.remove());
		strip.appendChild(retry);
		strip.appendChild(dismiss);
		const prev = stage.querySelector('.graph-error-strip');
		if (prev) {
			prev.remove();
		}
		stage.insertBefore(strip, toolbar);
	}

	// ---- wiring ------------------------------------------------------------

	search.addEventListener('input', () => {
		query = search.value;
		applyHighlight();
	});
	labelSel.addEventListener('change', () => {
		labelMode = labelSel.value;
		if (cy) {
			cy.nodes('[kind = "memory"]').toggleClass('labels-off', labelMode === 'off');
			cy.style(stylesheet()).update();
		}
	});
	focusBtn.addEventListener('click', () => {
		focusMode = !focusMode;
		focusBtn.classList.toggle('on', focusMode);
		applyHighlight();
	});

	if (ctx.refresh) {
		ctx.refresh.addEventListener('click', () => build());
	}
	const fitBtn = ctx.panel.querySelector('[data-graph-fit]');
	if (fitBtn) {
		fitBtn.addEventListener('click', () => cy && cy.animate({ fit: { padding: 40 }, duration: 250 }));
	}
	const relayoutBtn = ctx.panel.querySelector('[data-graph-relayout]');
	if (relayoutBtn) {
		relayoutBtn.addEventListener('click', () => {
			if (cy) {
				cy.layout(layoutOptions()).run();
				positionScopeChips();
			}
		});
	}

	host.onMessage((msg) => {
		if (!msg || typeof msg !== 'object') {
			return;
		}
		if (msg.type === 'state') {
			state = msg.state;
			render();
		} else if (msg.type === 'progress') {
			if (state) {
				state.progress = msg.progress;
			}
			showProgress(msg.progress, !!(state && state.snapshot));
		}
	});

	// The canvas is display:none until the panel is active, so cytoscape measures
	// a zero-size container on first mount. Re-fitting on every panel entry is
	// what makes the first visit to the tab show a graph instead of a dot.
	ctx.onRefresh = () => {
		if (cy) {
			cy.resize();
			cy.fit(undefined, 40);
			positionScopeChips();
		}
	};

	// Cytoscape caches its container size, so anything that changes the canvas box
	// leaves it rendering into stale dimensions: the graph squashes and the scope
	// chips drift off their clusters. Three things do that here — the window
	// resizing, the inspector opening, and the inspector closing — and one
	// observer on the canvas covers all three without any of them having to know
	// the graph exists.
	if (window.ResizeObserver) {
		let pending = 0;
		new ResizeObserver(() => {
			if (pending) {
				return;
			}
			pending = requestAnimationFrame(() => {
				pending = 0;
				if (cy) {
					cy.resize();
					positionScopeChips();
				}
			});
		}).observe(canvas);
	}

	// Follow the shell's light/dark flip.
	new MutationObserver(() => {
		if (cy) {
			cy.style(stylesheet()).update();
			rebuildScopeChips();
		}
		if (state && state.snapshot) {
			renderRail(state.snapshot);
		}
	}).observe(document.body, { attributes: true, attributeFilter: ['class'] });

	host.postMessage({ type: 'ready' });
};
