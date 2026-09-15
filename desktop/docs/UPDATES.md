# Updating Engraphy desktop

What ships today, how far one click reaches on each platform, and what each of
the remaining options would unlock. Written 31 August 2026.

## What ships today

The app reads `https://engraphy.tech/version.json` once a day and shows a
banner when a newer version is published, naming the running version and the
published one. The banner offers the release asset for this platform, the
release notes, and a per-version dismissal.

The check is a plain GET of a static path. Nothing about the install goes into
the URL, so there is no version telemetry, and every failure leaves the banner
hidden: offline is a normal state. **Check for new versions** in Settings turns
it off, which is `updateCheckEnabled` in `engraphy-settings.json`.

`ENGRAPHY_MANIFEST_URL` points the check at another copy of the manifest, for a
deployment that would rather its clients not reach engraphy.tech.

See DECISIONS.md section 21 for why the manifest is the version source rather
than the GitHub release API, and why the distribution channel decides what the
banner offers.

## The ceiling on each platform

| Channel | What one click does today | What delivers a new version |
| --- | --- | --- |
| Microsoft Store (MSIX) | Opens the release notes | The Store, silently, on its own |
| Direct download (NSIS) | Opens the installer download | The user runs the installer |
| macOS (dmg) | No build published yet | Not applicable yet |

**Direct download is a two-step ceiling, and the second step belongs to
Windows.** The app can put the installer in the user's hands and no further:
running an installer while the application it replaces is open is something
Windows arbitrates, not something Engraphy can drive from inside the process it
is about to overwrite. The banner states the download size and the confirmation
prompt in advance, because meeting either unannounced is where an update gets
abandoned.

## What the Microsoft Store unlocks

**Store apps update themselves, silently, at no cost.** This is the shortest
route to a genuinely one-click update path on Windows, and it is also the only
route that clears SmartScreen at zero cost, because Microsoft re-signs Store
MSIX submissions.

The prerequisites are met:

* An MSIX builds, installs, launches and renders correctly, graph view
  included, verified on Windows 11 Pro 26100 x64 and written up in
  `MSIX-BUILD-TEST.md`.
* Store developer registration is free for individuals, and has been free for
  companies since 7 May 2026.

One toolchain workaround applies and is documented in `MSIX-BUILD-TEST.md`:
electron-builder's vendored `makeappx.exe` predates Windows 11, so the current
Windows SDK binary finishes the pack from the staging layout electron-builder
has already prepared.

Submitting an MSI or EXE to the Store instead of an MSIX does not get the
package re-signed, and lands back on needing a certificate.

**This is a Devon decision: a Store account and a submission.** No certificate,
no ongoing cost, no code change beyond committing the `appx` target.

## What a code signing certificate unlocks

A certificate buys a **signed installer for direct download**, which is a
different goal from the Store's. It is what would make `electron-updater`
viable: on Windows, differential updates verify the signature of what they
download, so unattended in-place updating is gated on signing rather than on
the updater library.

| Option | Cost | Availability | SmartScreen |
| --- | --- | --- | --- |
| Microsoft Store (MSIX) | Free | Open | No warning, Microsoft re-signs |
| Azure Artifact Signing | ~10 USD/month | Individuals in the USA and Canada only | Reputation builds over time |
| OV certificate | 150 to 300 USD/year | Open | Reputation builds over time |
| EV certificate | 400+ USD/year | Open | Same as OV since 2024 |

Two constraints narrow this materially:

* **Azure Artifact Signing is not available to Devon as an individual.** It
  covers individual developers in the USA and Canada, and organisations in the
  USA, Canada, the EU and the UK. Australia is outside both.
* **An OV certificate's private key must live on an HSM or hardware token**
  under CA/Browser Forum rules since June 2023. A shipped USB token breaks
  unattended CI signing, and a cloud HSM carries its own fee.

SignPath Foundation's free OV signing for open source requires an OSI-approved
licence. Engraphy is BUSL 1.1, which is source-available, so it does not apply.

## macOS

The electron-builder config is ready: dmg for arm64 and x64, `hardenedRuntime`
enabled, and both entitlements files present. Two things stand between that and
a published build:

1. **Signing and notarising a macOS app cannot be done from Windows.** It needs
   a Mac or a `macos-latest` GitHub Actions runner, which is free for public
   repositories and is the more sensible route.
2. **Apple Developer Program enrolment, 99 USD per year**, then a Developer ID
   Application certificate and an App Store Connect API key for notarisation.
   Prefer the API key over an app-specific password: keys do not expire and do
   not trip over two-factor authentication in CI.

Once those exist, `notarize: true` under `build.mac` plus the credentials in
the build environment is the whole change.

## Devon's decisions

1. **A Microsoft Store submission.** Free, build-verified, and the only option
   that delivers silent updates and clears SmartScreen at no cost. The highest
   value item here.
2. **A code signing certificate**, if a signed direct download matters
   alongside the Store. Given the eligibility and hardware-token constraints
   above, this is a deliberate purchase rather than an obvious one.
3. **Apple Developer Program enrolment**, whenever macOS becomes a target.
4. **Where `engraphy-desktop` lives.** It has no git remote, so the tree that
   produces a published binary exists on one machine. That decision precedes
   any release workflow that builds the installer in CI.
