#!/usr/bin/env node
// Checks a requested desktop version before desktop-release.yml tags it. Run
// from desktop:
//
//   node scripts/prepare-release.js 0.1.1
//
// The version must be plain x.y.z and no older than package.json's.
// package.json is set by `npm version` in the workflow, which keeps
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

/** Decide whether `version` can be released over `current`. */
function planRelease(version, current) {
	if (!SEMVER.test(version)) {
		return { ok: false, problem: `"${version}" is not a plain x.y.z version.` };
	}
	if (SEMVER.test(current) && compareVersions(version, current) < 0) {
		return { ok: false, problem: `${version} is older than package.json's ${current}.` };
	}
	return { ok: true };
}

if (require.main === module) {
	const root = path.join(__dirname, '..');
	const version = process.argv[2] || '';
	const current = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version;
	const plan = planRelease(version, current);
	if (!plan.ok) {
		console.error(`::error::${plan.problem}`);
		process.exit(1);
	}
	console.log(`Releasing ${version} over package.json's ${current}.`);
}

module.exports = { compareVersions, planRelease };
