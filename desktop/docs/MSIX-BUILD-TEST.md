# MSIX / Store packaging test

Run on 2026-08-22 against `feat/graph-viewer`, Windows 11 Pro 26100, x64.

## Verdict

**A working MSIX build succeeds, installs, launches, and renders, graph view
included. The free Microsoft Store path is technically viable.**

The `script-src 'self'` concern did not materialise. Vendored Cytoscape loads
and runs from inside the packaged app under MSIX.

One toolchain defect has to be worked around: electron-builder's bundled
`makeappx.exe` cannot run on this machine. It is not a defect in the app.

## What was done

`package.json` gained an `appx` target and an `appx` config block, and
`build/appx/` gained the four required tiles (`StoreLogo` 50x50,
`Square150x150Logo`, `Square44x44Logo` 44x44, `Wide310x150Logo`), generated
from `build/icon.png`.

Both changes are **uncommitted** in the working tree, for review.

## Issue 1: electron-builder cannot pack the MSIX

`npx electron-builder --win appx` gets all the way through packaging and then
dies:

```
• building        target=AppX arch=x64 file=release\Engraphy 0.1.0.appx
• AppX is not signed  reason=Windows Store only build
⨯ spawn UNKNOWN   failedTask=build
    at AppXTarget.build (app-builder-lib/src/targets/AppxTarget.ts:146:14)
```

`AppxTarget.js:122` runs `makeappx.exe` from electron-builder's own vendor
directory, not from the Windows SDK:

```
%LOCALAPPDATA%\electron-builder\Cache\winCodeSign\winCodeSign-2.6.0\windows-10\x64\makeappx.exe
```

That file exists, but it is dated June 2019 and will not start:

> The application has failed to start because its side-by-side configuration
> is incorrect.

It depends on a VC++ SxS assembly that is not present on Windows 11. `spawn
UNKNOWN` is electron-builder surfacing that failure without the message.

**Workaround:** the Windows SDK ships a current `makeappx.exe`
(`10.0.26100.0`). electron-builder leaves a complete, valid staging layout at
`release/__appx-x64/` (`AppxManifest.xml` plus `mapping.txt`), so the SDK tool
finishes the job with the arguments electron-builder had already prepared:

```
& "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\makeappx.exe" `
    pack /o /f release\__appx-x64\mapping.txt /p release\Engraphy-0.1.0-x64.msix
```

```
Packing 77 file(s) ... Package creation succeeded.
```

Result: `Engraphy-0.1.0-x64.msix`, 118,930,055 bytes.

For CI this is worth wiring as an explicit post-step rather than relying on
electron-builder's vendored tool, or by pointing electron-builder at the SDK
copy.

## Issue 2 (not an issue): does it actually run

Installed **without any signing certificate** by registering the loose layout
in Developer Mode, which still gives real package identity and the MSIX
runtime:

```
Add-AppxPackage -Register release\msix-layout\AppxManifest.xml
```

```
Name            : Engraphy.EngraphyDesktop
PackageFullName : Engraphy.EngraphyDesktop_0.1.0.0_x64__tyga331841ps4
Status          : Ok
```

Launched under package identity with `Invoke-CommandInDesktopPackage` and
driven with the existing smoke harness (`ENGRAPHY_SMOKE`), pointed at the stub
MCP server.

Note: environment variables do **not** propagate through
`Invoke-CommandInDesktopPackage`. They have to be set at user scope so the
activated process inherits them. That cost a couple of confusing runs.

### The renderer under MSIX

`ELECTRON_ENABLE_LOGGING` + `ELECTRON_LOG_FILE` captured this console line from
the packaged app:

```
INFO:CONSOLE(23) "You have set a custom wheel sensitivity. ...",
source: file:///.../release/msix-layout/app/resources/app.asar/out/renderer/vendor/cytoscape.min.js (23)
```

Cytoscape emits that warning when a **Core is instantiated**, not merely when
the file is parsed, so a live Cytoscape instance existed inside the MSIX
container, loaded from `app.asar` over `file://` under `script-src 'self'`.
There were **zero** CSP violation entries in the log.

This is the load-bearing evidence for the verdict.

### The graph view

The MSIX run and a non-MSIX control run of the same build were driven through
the identical scenario. The captured graph-panel screenshots are
**byte-identical**:

```
03a422b71ae71e8408e4e1f12ce69578792310fde0e5391586fbe8bf9a7c01cd  ctlshot-graph.png   (non-MSIX control)
03a422b71ae71e8408e4e1f12ce69578792310fde0e5391586fbe8bf9a7c01cd  msix4-graph.png     (MSIX)
03a422b71ae71e8408e4e1f12ce69578792310fde0e5391586fbe8bf9a7c01cd  msix6-graph.png     (MSIX, rerun)
```

The control run's stdout, which MSIX activation cannot give us, reports what
that state contains:

```
ENGRAPHY_SMOKE_GRAPH {"libs":true,"idleShown":true,"statusText":"5 memories · 0 links · 2 scopes",
  "cy":{"memories":5,"scopeClusters":2,"labelled":5,"parented":5,"laidOut":5}}
ENGRAPHY_SMOKE_GRAPH_READ {"labelsVisibleAtZoom":5,"inspectorOpen":true,...}
```

`libs:true` is `!!window.cytoscape && !!window.cytoscapeFcose`.

Note what this does and does not prove. Identical screenshots prove both runs
reached the *same final UI state*; on their own they would not rule out a
failed harvest, since that also ends at idle. They show the graph **idle**
because the harness clears the graph as part of its own control assertions
(`idleAfterClear:true`) before the screenshot phase, and both runs end there.

The graph cache file is likewise absent after all three runs, including the
non-MSIX control whose stdout proves it succeeded. So cache-absence is the
normal post-clear state, not an MSIX failure:

```
ctl-profile    non-MSIX, stdout-verified successful   no engraphy-graph-*.json
msix-profile4  MSIX                                   no engraphy-graph-*.json
msix-profile6  MSIX, rerun                            no engraphy-graph-*.json
```

An earlier MSIX run against a stub that returned no graph nodes did write a
cache (`briefingCalls: 6`, both scopes discovered), which separately confirms
that network, IPC, and disk writes all work under package identity.

## Store submission: what is still needed

Technically nothing further. The remaining gate is an account, which is
Devon's to create:

- **Partner Center individual developer registration.** A one-time paid
  signup; confirm the current amount in Partner Center. Until that account
  exists, nothing can be reserved or submitted.
- The reserved Store app name then replaces the placeholder `identityName`
  (`Engraphy.EngraphyDesktop`) and `publisher` (`CN=Engraphy`) in
  `package.json`.

**The cost win:** apps submitted to the Store are signed by Microsoft. No
separate OV or EV code-signing certificate is needed for that channel, which
is the recurring cost the direct-download NSIS installer would otherwise
carry.

Nothing was submitted to the Store. The test package was unregistered and the
temporary environment variables were removed afterwards.
