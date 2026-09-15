# Engraphy desktop — decisions log

A running record of the choices made building the standalone desktop app, and why.
Newest context first within each section. Avoid em dashes per house style.

## 1. Framework: Electron (not Tauri)

**Decision: Electron.** Verified empirically before committing:

- **No Rust toolchain on this machine.** `rustc --version` and `cargo --version`
  both return "not found". Tauri needs Rust plus the MSVC build tools; standing
  that up on Windows is a heavy install with real build-failure risk, and the
  task requires producing the Windows artifact now.
- **The MCP client is Node-native and ESM.** The extension's client speaks MCP
  Streamable HTTP through `@modelcontextprotocol/sdk` (checked: `"type":"module"`,
  version 1.30.0). Electron's main process IS Node, so `mcpClient.ts` and the
  pure helper modules port verbatim. Tauri's webview has no Node, so the same
  code would force reimplementing the transport (session handshake, SSE/JSON
  negotiation) in Rust. That is the opposite of "reuse the existing MCP client".
- **One toolchain builds both OSes.** electron-builder produces a Windows NSIS
  installer and a macOS dmg from a single Node setup, with no per-target native
  compiler.

Tauri would be lighter on disk, but "small app" does not outweigh a rewrite of
the one piece the task explicitly says to reuse, plus a toolchain that is not
present.

## 2. Bundling: esbuild main + preload to CommonJS

The SDK is ESM-only (`"type":"module"` + subpath exports). A plain `tsc` -> CJS
main process throws `ERR_REQUIRE_ESM` at `require()` time. So `esbuild.js`
bundles `src/main/main.ts`, `src/preload/preload.ts`, and `stub/stub-server.ts`
each into a single CJS file (`platform:node`, `external:['electron']`), exactly
as the VS Code extension bundles itself. Consequences:

- The SDK is inlined into `out/main.js`, so it is a **devDependency**, and the
  packaged app ships **no `node_modules`** at all. The asar is just the bundled
  `out/` tree plus `package.json` (verified with `asar list`).
- The SDK is pinned to **1.30.0** exactly (same as the extension), not floated.

## 3. Code reuse and provenance

- `src/main/client/{mcpClient,toolResult,statsModel,webviewMessages}.ts` are
  **copied verbatim** from an earlier webview build of the VS Code extension
  (v0.4.0), with only a provenance header added. They have no `vscode`/DOM
  import, so they
  are the trust boundary and the MCP contract, unchanged. Fix upstream bugs in
  the extension and re-sync; do not diverge here.
- `renderer/css/confirm.css`, `stats.css` and `renderer/views/confirm.js`,
  `stats.js` are **ported** from the extension's `media/`. The render logic
  (cards, sparklines, sections, onboarding block) is byte-for-byte the same; only
  a small shell header changed per file (documented at the top of each): the
  top-level IIFE became a `window.mount*(ctx)` factory, `acquireVsCodeApi()`
  became the preload IPC bridge, busy state is scoped to the panel instead of
  `document.body`, the loop-mark URI comes from a global, and the `window`
  `message` listener became a channel-scoped `host.onMessage` so the two views
  cannot cross-talk in a single window.
- `renderer/views/explorer.js` and `settings.js` are **written fresh** (the
  extension used a native TreeView and native input boxes, neither of which
  ports). They hold the same discipline as the copied code: every server string
  goes through `textContent`, never `innerHTML`.

## 4. Theme shim: brand palette via the `--vscode-*` variables

The copied CSS reads every structural colour from injected `--vscode-*`
variables. `renderer/css/theme.css` **defines those variables** mapped onto the
Engraphy brand palette from `Engraphy-design/brand/brand-guidelines.md` (Verdant
`#4C7A59` on Cream `#F3F1E6`), for light and dark. `app.js` sets `body.vscode-dark`
/ `body.vscode-light` from the OS colour scheme, which also fires the copied
CSS's own dark-lift rules for `--brand-accent-text`. `--radius` is bumped from
the VS Code 6px to the brand's ~12px card feel. This is the highest-reuse path:
the extension's UI renders as Engraphy, not as "the extension in a window".

## 5. Approve / merge / deny mapping (correctness, not naming)

The Engraphy server has exactly **two** resolutions for a pending duplicate:
`distinct` and `merge` (`resolve_duplicate`). The desktop UI mirrors the
extension's wiring verbatim:

- **Approve (keep distinct)** button -> `resolve_duplicate(resolution:'distinct')`
  -> keeps the write as a new, separate node.
- **Merge into <candidate>** button -> `resolve_duplicate(resolution:'merge',
  merge_into:<id>)` -> folds the write into that existing node. These buttons
  sit under the header "Deny - merge into one of these".

"Deny" is the label on the **merge** path (do not keep it as a distinct new
node), NOT a third server call. There is no third action. This matches the
extension exactly.

## 6. Token storage: OS keychain via safeStorage

The token is the identity on the server, so it is a secret. `settings.ts` stores
it with Electron `safeStorage` (DPAPI on Windows, Keychain on macOS), base64 in
`engraphy-settings.json` under `app.getPath('userData')`. When
`safeStorage.isEncryptionAvailable()` is false (e.g. a headless Linux session),
it falls back to plaintext with a persisted `tokenPlain` marker, and the
Settings UI shows a warning. The renderer never receives the token value, only
`hasToken` / `tokenInsecure`. No token is ever written to a log line. Verified
the encrypt -> decrypt round-trip in a packaged run (keychain available here, so
`tokenInsecure:false`).

## 7. Inbox review: fully included

`inbox_review` was "cheap to add" only for the list. Discard and Promote needed
real desktop replacements for the extension's native flows:

- **Discard** confirms with a native `dialog.showMessageBox` before calling
  `inbox_review(action:'discard')`.
- **Promote** opens an in-app modal (node-type dropdown from
  `STARTER_NODE_TYPES` plus an "Other" escape hatch, scope chooser when the item
  has none, title/body prefilled via `promoteDefaults`, all editable) and calls
  `inbox_review(action:'promote')`. Parked-as-pending outcomes are surfaced.

All three ship. Nothing was quietly cut.

## 8. Dev stub server

No Engraphy server answers at `127.0.0.1:8000`, and the SDK client does a real MCP
`initialize` handshake before any tool call, so a naive fixture endpoint would
fail before a panel renders. `stub/stub-server.ts` therefore uses the SDK's own
server side (low-level `Server` + `StreamableHTTPServerTransport`) behind Express,
plus an unauthenticated `/healthz`. It returns canned envelopes shaped to exactly
what the app's parsers expect and mutates in-memory state on resolve/discard/
promote so the queue visibly shrinks. It is a devDependency-only tool, excluded
from the packaged app.

## 9. Icons

`scripts/make-icons.js` rasterises the brand loop mark (Verdant on a Cream
rounded square) with `@resvg/resvg-js` to `build/icon.png` and a multi-size
`build/icon.ico` (via `png-to-ico`). electron-builder derives the macOS `.icns`
from `icon.png` on a Mac. This is a functional brand icon; Devon may replace it
with a designed one.

**Superseded in part by section 18**, which covers the optical-centring and
small-size stroke work and the move to a 1024 master.

## 10. Headless self-check kept in the build (env-gated)

`main.ts` has a smoke block gated on `ENGRAPHY_SMOKE`. With the var unset (every
normal launch, and every packaged launch) it does nothing. With it set it prints
a DOM/state snapshot and exits, which is how the app was verified end to end
(dev and packaged) and how Devon can re-verify. It is a few inert KB; left in on
purpose rather than stripped.

## 11. Security posture

`contextIsolation:true`, `nodeIntegration:false`, `sandbox:true`, a preload that
exposes only a minimal channel-tagged IPC surface, and a strict CSP meta
(`default-src 'none'; script-src 'self'; connect-src 'none'`). The renderer never
talks to the server or the network directly; main owns every MCP call. External
links route through a validated `shell.openExternal`, and both
`setWindowOpenHandler` and `will-navigate` are trapped so nothing can navigate
the app window away from its own file:// page or open a second one.

**Message validation is now actually wired.** This section originally described
a posture the code did not fully hold: the copied client ships
`parseWebviewMessage` and `parseStatsMessage`, whose own comments call them the
trust boundary, but nothing called them. `handleMessage` read `msg.pendingId`,
`msg.mergeInto` and `msg.inboxId` straight off the raw payload, while the test
suite asserted the unused parsers' behaviour in eight checks, which made the
coverage look broader than it was. `src/main/messages.ts` now sits in front of
every fire-and-forget channel: it delegates to the frozen parsers for the
commands they model and validates the two desktop-only additions
(`promoteSubmit`, `reconnect`) itself, so an unparseable message is dropped
rather than acted on.

The practical risk here was always low (the renderer is first-party, sandboxed,
and cannot be navigated elsewhere) but the gap between the documented posture and
the code was not, and closing it is what surfaced the Promote bug in section 15.

## 12. Connection states: three, not two (and why client/ was not edited)

The extension models one failure: `no-server`, with reason `unconfigured` or
`unreachable`. Verified against the LIVE Engraphy server (0.1.0) that this is wrong
for the most common real case. The MCP SDK surfaces a 401 as:

```
Streamable HTTP error: Error POSTing to endpoint: unauthorized
```

That string carries no status digits, and it matches the `/Unauthorized/i` branch
of the copied `isConnectionError`. So a healthy, running server that simply
rejected your token rendered "No server connected. If you just installed, you
probably need to start one first." The user goes and restarts Docker, which was
never the problem.

Worse, the health badge was green while it happened: Engraphy's `/healthz` is
UNAUTHENTICATED, so it answers 200 for a server you cannot read a byte from. On
this machine, with no settings file, the app's default URL points at the live
server, so the shipped first-run experience was a green "Connected" badge above
four panels all failing with 401s.

Two changes, both in NEW desktop-owned modules rather than in `client/`, which
stays frozen per section 3:

- `src/main/connection.ts` classifies a thrown value as `auth` / `transport` /
  `config` / `tool`, reading `error.code` and the undici `cause` chain rather
  than pattern-matching prose. `computeConnection` then yields `unauthorized`
  when every band failed on auth and `unreachable` when every band failed on
  transport, preserving the extension's rule that ONE band failing is a per-band
  problem rendered inline.
- The health badge requires an AUTHENTICATED probe (`scope_list`) to go green.

**What should go upstream:** the extension has the same two bugs. The three-way
split and the authenticated health probe belong in the extension's own
`webviewMessages.ts` and `status.ts`. When that lands, re-copy and delete the
divergence here.

`scope_list` is a deliberate choice for the probe: it runs every 20 seconds, and
`engraphy/core/scopes.py` records no metrics. Polling `search` instead would have
the app inflating the ANSWERED and MEMORY_REUSED counters it displays on its own
Impact panel.

## 13. One invoke contract, because the old one silently swallowed failures

`ipcMain.handle`'s wrapper catches and RESOLVES `{ok:false, error}` so the
renderer never sees an unhandled rejection. The consequence was not obvious: a
renderer `try/catch` around `host.invoke(...)` NEVER FIRES. Every view that read
its payload straight off the result treated a failure as an empty success.
Concretely, the explorer rendered "No results for X" for a dead server and for a
rejected token alike, the detail pane pretty-printed `{"ok":false,...}` into its
JSON box, and the scope dropdown silently stayed empty.

Every handler now returns the discriminated shape from `ipcResult.ts`, and every
caller branches on `ok` before touching the payload. One contract, four bugs.

## 14. Test connection must not persist

The obvious implementation of a Test button saves the form and then probes what
was saved. That means testing a typo destroys the working connection you already
had. `testConnection` builds a throwaway `EngraphyClient` bound to the candidate
values instead, so nothing is written until Save. An omitted token means "use the
stored one", matching Save's semantics.

## 15. Verification: unit checks plus a scripted state harness

`npm test` (124 checks) covers the pure modules, and carries the extension's own
suite verbatim as a re-sync guard for the frozen `client/` copies.

Unit tests cannot show that a panel RENDERS a state rather than crashing or
blanking, which is what this round of work was about, so `scripts/smoke.js`
launches the real app once per scenario in a throwaway user-data profile and
asserts on the rendered DOM: connected, empty, unauthorized (against the live
server), unreachable, unconfigured, onboarding, loading, and the settings
round-trip. Every scenario also asserts the window is not blank and nothing
crashed.

The connected scenario drives the whole review surface: search, open a record,
follow its links, Approve, Merge, and Promote on both an item that carries a
scope and one that does not (the scope-chooser branch). The loading scenario
needs the stub's STUB_DELAY_MS knob, because against a local stub the first
paint completes in milliseconds and the skeletons are otherwise unobservable.

`--packaged` runs the same scenarios against `release/win-unpacked/Engraphy.exe`,
because packaging is where asar packing and the copied renderer tree can go wrong
without the dev run noticing.

This paid for itself immediately. Driving the Promote modal found that Promote
had never worked: `confirm.js` posted
`{ type: 'promoteSubmit', inboxId, type, scope, title, body }`, where the
shorthand `type` (the NODE type) overwrote the message's own `type`
discriminator, because an object literal keeps the last value. The message
arrived as `{type:'note',...}`, the host's switch never matched it, and the
button closed the modal, set the panel busy, and did nothing. No error anywhere.
No unit test could have caught it: every function involved was individually
correct. The field is now `nodeType`, and both a unit check and the smoke
scenario pin it.

Discard is deliberately NOT covered: it confirms through a native
`dialog.showMessageBox`, which blocks the main process and cannot be driven from
the renderer. Verify it by hand.

`ensureConnected` in the frozen `client/mcpClient.ts` has no in-flight guard, so
the several callers that race at startup each build a transport and the losers
are orphaned rather than closed. The 20-second authenticated probe and
`reconnect()` (which fans out to four callers) make it fire more often than it
used to. It leaks MCP sessions rather than crashing, and the file is frozen, so
it is listed here as an upstream fix alongside the two in section 12.

## 16. The brand mark had never rendered

The sidebar logo and every large mark in the empty and recovery states painted
nothing, in dev and in the packaged app, from the first commit.
`assets/loop-mark.svg` carried a comment that mentioned the CSS custom property
`--brand-mark` by name. A double hyphen is ILLEGAL inside an XML comment, so the
file was not well-formed, Chromium refused to parse it as an image, and a CSS
mask whose image fails to load paints nothing AND logs nothing.

Diagnosed by loading the asset in a real window rather than by inspection. Worth
recording that the first hypothesis (a CSP `img-src 'self'` mismatch on `file://`)
was wrong: it was tested and discarded, because once the comment is fixed the
plain asset path loads fine. `scripts/copy-renderer.js` now fails the build on a
malformed comment in any shipped SVG.

## 17. Window chrome

- **A real application menu.** `win.removeMenu()` is a shipping defect on macOS:
  with no menu there is no Cmd+Q, Cmd+C, Cmd+V or Cmd+W, and those are not
  optional on that platform. The template uses the standard `appMenu` /
  `editMenu` / `windowMenu` roles plus nav (Cmd/Ctrl+1..4), Refresh and Reconnect
  accelerators. Windows and Linux keep the bar hidden so the custom title bar
  owns the top, but the accelerators still fire.
- **Custom title bar** per platform: `titleBarOverlay` on Windows,
  `hiddenInset` plus a traffic-light inset on macOS, native frame on Linux. The
  CSS reserves the corner the OS paints its own buttons into.
- **backgroundColor follows `nativeTheme`.** It was hardcoded to Cream, so every
  launch in dark mode flashed a bright cream rectangle before the renderer
  painted. The window is also held hidden until `ready-to-show`.
- **Window bounds are restored, display-aware.** Position is only reapplied when
  it still intersects a display that exists right now, so a window last closed on
  a since-unplugged second monitor cannot reopen entirely off-screen with no way
  to drag it back. The logic lives in the pure `windowState.ts` and is tested.

## 18. Icon

`scripts/make-icons.js` measures the mark's ink from the rendered alpha channel
rather than assuming it is centred in its viewBox. It is not: the brand path's
ink centre sits 5.5 units above the geometric centre, so the first version's icon
was visibly high in the tile and scaled to about half of it. It now scales about
the ink centre to 72% of the tile, and thickens the monoline stroke for the 16
and 32 pixel rasters, where the brand stroke-width lands near one physical pixel
and the loop dissolves into a smudge. Path and colours are unchanged and still
per the brand guidelines.

## 19. Memories auto-lists on open (the "I connected and saw nothing" report)

Reported: a real Engraphy server, a valid readwrite token, and the Memories panel
showed nothing. Diagnosed against that live server rather than the stub.

**It was not a bug in the read path.** The live authorized read works end to end:
the badge reads `Connected`, `search {scope:'all', query:''}` returns everything
the token can read, and the record card renders. What happened is that the panel
was SEARCH-DRIVEN and ran no query on mount: it displayed an idle prompt
("Search your memory graph" plus a "Show everything" button) and waited. To a
user who had just finished connecting, an empty page is indistinguishable from a
broken one, so the design was the defect even though the code was correct.

Memories now auto-browses on open, with three constraints:

- Failures route through the same path as a manual search, so unconfigured /
  unreachable / rejected-token servers still land on their recovery block rather
  than on a failed query. All four failure scenarios were re-run.
- Switching tabs does NOT re-query. `onRefresh` retries only when the panel is
  stuck on a failure block, which is the "I fixed my token, now show me" case;
  the shell also calls it after a successful save or reconnect.
- A genuinely empty space now says "No memories yet" and explains how one gets
  added, instead of the older "Nothing readable yet".

The kick-off call sits at the BOTTOM of the module on purpose: function
declarations hoist but the `let busy / lastFailed / userSearched` bindings do
not, so calling it earlier throws on the temporal dead zone.

Two further findings from the same live session:

- **Node types were wrong for a real space.** `STARTER_NODE_TYPES` is the starter
  pack's list. The live space holds `note` and `project`; the starter list offers
  `project_ref`. The promote form therefore offered a type the space does not use
  and buried the right one behind "Other...". Main now remembers the types search
  actually returned and puts them first, keeping the starter entries beneath
  rather than replacing them (the observed set only reflects the last search and
  could be a narrow slice). Covered by unit tests; not exercisable against the
  live space, whose inbox is empty.
- **Ambient scopes make the scope filter look broken, and are not our bug.** That
  space's `personal-devon` scope is `ambient: true`, so the server unions it into
  every scoped read. Picking any scope in the dropdown still shows that scope's
  node, and even a nonexistent scope returns it rather than erroring. That is
  server behaviour worth explaining to a user, not something the app should
  paper over.

The durable outcome is `scripts/smoke.js --only=live`, gated on
`ENGRAPHY_LIVE_TOKEN` so no secret enters the repo and skipped when unset. It is
the scenario whose absence let this ship: everything else proved the app against
the stub or against failure conditions, and nothing proved a genuine authorized
read renders.

## 20. The graph is INDEXED client-side, not fetched

**The problem.** The Memories panel is a list, and a list cannot answer "what
shape is my memory?". A graph view needs every node, every edge, and which scope
each node belongs to — all at once.

**Engraphy will not hand that over.** There is no whole-graph read on the MCP
surface, and every read that exists is capped deliberately:

| Tool | Cap |
| --- | --- |
| `search` | 25 results |
| `traverse` | 50 **walk rows**, depth ≤ 4 |
| `briefing` | 10 per non-semantic section; the semantic one needs a `hint` |

On top of that, the server enforces **60 reads per minute per token**. So the
binding cost of a whole-graph view is CALL COUNT, not latency — each call is
about 10 ms against a local server, and the wall clock is almost entirely the
rate-limit pacing.

**Rejected: add a `graph` tool to Engraphy.** It would make this one call. It was
not done. Engraphy is a different repo, the change was not asked for, it forces a
local image rebuild, and it would silently break the graph view against any
server without the tool. `wire_types.py` also states that its argument table is a
transcription of a normative design document rather than an independent design,
so adding a tool properly means touching design docs and decision logs in that
repo — a large unasked expansion in the wrong place. The portable version is
built instead. A server-side tool remains the right answer eventually, and is
worth proposing on its own terms.

**Rejected: raise `rate.read_per_min`.** It is a per-space server setting in
Devon's `config` table. Changing someone's server config to make a client feel
faster is not the client's call. Proposed, not done.

### Two constraints picked the algorithm

**1. Indexing must not move the usage counters.** `search` is exactly what the
`stats` tool counts as `questions_asked`, and every result it returns adds to
`memory_reused`. A 16-scope search sweep adds roughly 16 questions and 390
reuses; the space's real 30-day totals are 48 and 268. Drawing a picture would
have more than doubled the numbers on the Impact & usage panel. `briefing` and
`traverse` are explicitly NOT counted (`engraphy/core/metrics.py` says so in as
many words), so the default index uses only those two, and the `search` sweep is
opt-in, off by default, and labelled in the UI as moving the counters.

**2. Depth 2, not 3.** `traverse` orders walk rows by `(depth, src, dst, type)`
BEFORE applying its 50-row limit, so on truncation the shallow rows are the ones
that survive — which means a start node's own edges (all depth-1 rows) are still
complete unless that node alone has 50 of them. That makes "the start is
edge-complete" safe even on a truncated walk, and lets a non-truncated depth-2
walk close the start AND all its neighbours in one call. Depth 3 was measured and
is strictly worse: at this graph's average degree (~3.7) the depth-3 frontier
blows past 50 rows on 95% of calls, collapsing the rule back to one node per call
while wasting the rows. Measured: 192 traverse calls at depth 3 versus 166 at
depth 2, for worse coverage.

### Seeding needs several hints per scope

The pack's briefing has two sections: a semantic `relevant` one that returns
nothing without a `hint`, and a `recent_notes` one filtered to type `note` within
14 days. One briefing per scope with no hint reached 43 seeds and 13 of 16
scopes; one hinted briefing reached 80 seeds and 15 of 16; three hints reach 93
and all 16. A fourth hint bought one more memory for 16 more reads, which is
where it stops. Briefing is metrics-free, so these extra calls cost only time.

### What is NOT reached, and why it is reported

A walk only reaches what is linked to a seed. That excludes link-less memories —
and, less obviously, small **islands** of memories linked only to each other in a
scope whose hints did not surface them, which is what the live gap actually
turned out to be (9 of the 10 missed nodes had degree ≥ 1). Default coverage is
234 of 239 memories and 434 of 437 links, all 16 scopes.

A node with more links than one 50-row read can return is handled by partitioning
the read — first by direction, then by relationship name. A unit test covers the
case that motivated the second axis: 70 edges all pointing the same way defeat the
direction split on its own, because both halves are 70 and 0. If a single
(direction, relationship) slice still overflows, nothing finer exists on the
frozen surface, so the node is counted in `unreadableHubs` and the status line
says `N link lists incomplete` rather than passing an under-drawn graph off as
complete.

### Rendering: scopes are compound parents

cytoscape + fcose, vendored locally by `copy-renderer.js` — the renderer CSP is
`script-src 'self'` with `connect-src 'none'`, so a CDN script is blocked and the
renderer cannot fetch anything itself. Each scope is a cytoscape **compound
parent** holding its memories, so the layout engine is what separates the regions
and each parent is sized by what it contains, rather than us hand-tuning per-scope
forces.

Two rendering traps, both of which shipped once and looked like design failures:

- **`hsl()` must be comma-separated.** cytoscape parses colours itself and its
  parser predates space-separated CSS Color 4 syntax, so `hsl(120 45% 55%)` is
  unrecognised and the property silently falls back to its default. Sixteen
  scopes rendered identically grey despite each having its own hue. The HTML
  chips (real CSS) were coloured correctly the whole time, which is what gave it
  away.
- **`background-opacity` is a separate property.** The alpha channel of an
  `rgba()` background-color is ignored, so a 6%-alpha tint painted at full
  opacity — sixteen solid slabs that swamped the nodes inside them.

Scope names are **HTML chips over the canvas**, not cytoscape labels. A cytoscape
label lives in the scene graph and scales with zoom, so at the zoom that fits 234
memories a 13px scope name renders around 4px and every cluster is anonymous. The
chips sit in screen space at a fixed size and track their cluster's bounding box
on pan/zoom.

### The sweep has to live where the graph does

Shipped once, wrong: the deep-sweep checkbox was inside the first-run block,
which renders only while there is NO snapshot. Since the cache loads as soon as
the panel opens, that block never renders again after the first build — so the
switch disappeared for good and **Rebuild** could only ever ask for a non-sweep
index. The feature was documented as opt-in while being, in practice,
unreachable. It now lives in the toolbar, where it exists whenever the panel
does, with both checkboxes driven from one variable so they cannot disagree.

The same review found `clearCache` implemented on both sides of the IPC boundary
and posted by nobody — a dead branch. It is now the **Clear index** action in the
status line, which is also how you get back to the first-run screen.

Neither was catchable by the tests as written: the graph scenario asserted on a
FRESH profile, where the first-run block does render. The smoke run now drives
both controls while a graph is on screen. It does not RUN the sweep, because that
spends `search` calls and would move the very counters the Impact & usage panel
reports.

### The first index rendered NOTHING, for three and a half minutes

The worst bug in this feature, and it survived every test run.

`render()`'s no-snapshot branch calls `showBlock()`, which hides `shell`. The
progress overlay lives inside `stage` inside `shell`. So on a FIRST build the
card was painted into a subtree that had just been hidden, and the panel showed
an empty `div` for the entire index: no spinner, no bar, no text, no counts.
Silence for three and a half minutes reads as a freeze, which is exactly how it
was reported.

It survived testing because a REBUILD works fine: a snapshot exists then, the
shell stays visible, and the overlay shows. Every iteration after the first build
took that path, and the one scenario that did index from scratch only waited for
the overlay to disappear rather than checking anything was visible while it was
there. The regression test now asserts `offsetParent !== null` on the card, not
its presence: presence would have passed throughout.

The card is now built once and re-parented into whichever container is on screen
— `blockHost` when there is no graph yet, the floating `overlay` when there is.

Four things came out of fixing it properly:

- **The bar is weighted across phases** (`PHASE_SPAN`). Per-phase fractions
  filled the bar during seeding, snapped it back to zero when walking started,
  and filled it again, which reads as a restart. The renderer also clamps it
  monotonic, because the only walk estimate available is `closed / known` and
  `known` grows as memories are discovered, so the raw ratio dips whenever a walk
  finds more than it closes.
- **Rate-limit pauses are announced.** The pacer sleeps up to a minute at a time,
  and nothing observable happens during it: no call, no counter, no event. That
  is the stretch most likely to be read as a crash, so `ReadPacer` now reports
  its waits and the panel counts them down live.
- **The spinner is the load-bearing element.** Every number on the card can sit
  unchanged for a minute; the spinner is what separates working from hung.
- **The click paints immediately.** `build()` shows the card before posting to
  main rather than waiting for `building` to echo back, because "I clicked and
  nothing happened" is the impression the whole fix exists to remove.

End states are now explicit in all three directions: success swaps to the graph,
a failure with no graph to fall back on lands on an error block with a retry, and
a failed rebuild keeps the old graph and explains itself in a strip above the
canvas. The failure path is covered against a real server that refuses the read.

### One robustness fix this shook out

A bad escape in `views/graph.js` made the file a syntax error. Because
`index.html` loads the views as plain `<script>` tags in sequence, the parse
failure took out `app.js` with it: the window opened, the title bar drew, and
three panels unrelated to the change silently rendered empty, with the only
evidence in a devtools console nobody had open. `copy-renderer.js` now parses
every renderer script at build time (`new Function`, which parses without
executing) and fails the build, alongside the SVG guard that exists for the same
class of silent failure.

## 21. Update checking, and why the channel decides the button

The app asks `engraphy.tech/version.json` once a day whether a newer version is
published, and shows a banner naming both versions when there is one.

**The manifest, not the GitHub release API.** The release tag is the engine's
version. At `v0.2.0` this app is 0.1.0, so a client that read the tag would tell
a current install to update to a version that is not its own. The manifest
states each product under its own key (`versionCheck.ts`), and
`engraphy-web/build-version.py` derives every number from the surface that
already publishes it, so a release bumps one place.

**The renderer never makes the request.** Its CSP is `connect-src 'none'` and
that stays true: main runs the check and the result arrives as a view-model like
every other push.

**The distribution channel decides what the banner offers**, which is why
`updateModel.ts` exists separately from `versionCheck.ts`. Whether a newer
version exists and what this install should do about it are different questions:

* **Microsoft Store** (`process.windowsStore`). The Store delivers new versions
  on its own, so the banner says so and offers **no download**. Handing an MSIX
  install an `.exe` would put a second copy of Engraphy on the machine, updating
  independently of the first.
* **Installer.** The banner offers the release asset for this platform and arch,
  and stops there: Windows runs the installer, not Engraphy. The size and the
  confirmation prompt are both stated before the click, because meeting either
  unannounced is where an update gets abandoned.

A release with no build for this machine offers the notes rather than a button
that cannot deliver what its label promises.

**What it does not do.** No prompt for a version already dismissed, and
dismissal is per version so it is never a permanent off switch. No banner when
the running version is ahead of the published one, which is what every local
build looks like during a release cycle. No message when the check fails:
offline is a normal state. The request is a plain GET of a static path with
nothing about this install in it, so there is no version telemetry to opt out
of, and **Check for new versions** in Settings stops it. That toggle applies on
change rather than on Save: it is a preference about the app, not part of the
connection the Save button tests and stores.

**Silent in-place update is not wired**, and the route to it is not a
certificate. See `docs/UPDATES.md`.

## Parity scope (what is in, what is deliberately out)

**In (the read + review surface):** memory explorer (search / get / traverse),
stats panel, confirm-write queue (pending approve/merge + inbox promote/discard),
connection/health indicator, and a Settings screen with persistence.

**In (added on top of the prototype):** first-run onboarding replacing the VS
Code walkthrough, an explicit Reconnect path (menu, banner, and every recovery
block), a structured node record card instead of a raw JSON dump, and real
loading / empty / error / disconnected states on all four panels.

**Out (IDE-specific bits, by design):** registering the MCP server with VS
Code/Copilot, and the Docker `compose up` "start local server" command (the
onboarding guide shows the commands rather than running Docker on the user's
behalf). The hosted-cloud placeholder is now covered honestly in onboarding step
2: it says there is no hosted service today rather than implying a signup.
`engraphy.pending.resolveById` is also out: it was a command-palette escape hatch
for an interface with no list, and the desktop app shows the list. The `briefing`
tool is available on the ported client but, as in the extension, has no dedicated
panel.

## Left for Devon

- **Code signing + notarization** (both OSes). The Windows installer is unsigned
  (SmartScreen will warn); macOS needs Developer ID signing + notarization to run
  without a Gatekeeper prompt. The entitlements plist that signing requires is
  already in `build/`. See README.
- **A designed app icon**, if the generated loop icon is not the final one. The
  generator is brand-correct and now optically centred, but it is still the mark
  on a plain tile rather than a designed icon.
- **The macOS build itself.** It cannot be produced or run from Windows, so the
  dmg, the `hiddenInset` title bar with its traffic-light inset, and the macOS
  menu roles are configured but UNVERIFIED. Everything else was verified on
  Windows, including against the packaged exe.
- ~~**A live authorized read.**~~ Done. The graph work needed one, so a token was
  minted against the local server (`graphviewer-dev-20260822`, space `devon`),
  used to verify the authorized path end to end — the `live` and `graph` smoke
  scenarios, and the Impact & usage panel against real `metrics_rollup` data —
  and then **revoked**. Both scenarios stay skipped unless `ENGRAPHY_LIVE_TOKEN`
  is set, so mint a fresh one to re-run them.
- **A live `deep sweep` run.** The opt-in `search` sweep is unit-tested against a
  fake server that reproduces the real caps, but it was deliberately NOT run
  against the live space: it moves `questions_asked` and `memory_reused`, which
  would visibly distort the Impact & usage panel. Worth doing once, knowingly, to
  confirm it closes the last few memories the walk cannot reach.
