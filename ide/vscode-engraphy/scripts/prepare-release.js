#!/usr/bin/env node
// Checks a requested extension version before ide-release.yml tags it, and
// writes its changelog heading. Run from ide/vscode-engraphy:
//
//   node scripts/prepare-release.js 0.6.1
//
// The version must be plain x.y.z and no older than package.json's, and
// CHANGELOG.md must already hold a "## <version>" section, because that
// section is the release notes. A "(unreleased)" marker on that heading is
// dropped. package.json is set by `npm version` in the workflow, which keeps
// package-lock.json in step; nothing here commits or tags.

'use strict';

const fs = require('fs');
const path = require('path');

const SEMVER = /^(\d+)\.(\d+)\.(\d+)$/;

/** Negative, zero or positive as a is older than, equal to, or newer than b. */
function compareVersions(a, b) {
	const x = a.match(SEMVER).slice(1).map(Number);
	const y = b.match(SEMVER).slice(1).map(Number);
	for (let i = 0; i < 3; i++) {
		if (x[i] !== y[i]) {
			return x[i] - y[i];
		}
	}
	return 0;
}

/**
 * Decide whether `version` can be released over `current`, given the
 * changelog text. Returns the changelog to write, or the problem to report.
 */
function planRelease(version, current, changelog) {
	if (!SEMVER.test(version)) {
		return { ok: false, problem: `"${version}" is not a plain x.y.z version.` };
	}
	if (SEMVER.test(current) && compareVersions(version, current) < 0) {
		return { ok: false, problem: `${version} is older than package.json's ${current}.` };
	}
	const heading = new RegExp(`^## ${version.replace(/\./g, '\\.')}( \\(unreleased\\))?(\\r?)$`, 'm');
	if (!heading.test(changelog)) {
		return {
			ok: false,
			problem: `CHANGELOG.md has no "## ${version}" section. Write the release notes there first.`,
		};
	}
	return { ok: true, changelog: changelog.replace(heading, `## ${version}$2`) };
}

if (require.main === module) {
	const root = path.join(__dirname, '..');
	const version = process.argv[2] || '';
	const current = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version;
	const changelogPath = path.join(root, 'CHANGELOG.md');
	const plan = planRelease(version, current, fs.readFileSync(changelogPath, 'utf8'));
	if (!plan.ok) {
		console.error(`::error::${plan.problem}`);
		process.exit(1);
	}
	fs.writeFileSync(changelogPath, plan.changelog);
	console.log(`Releasing ${version} over package.json's ${current}, under the heading "## ${version}".`);
}

module.exports = { compareVersions, planRelease };
