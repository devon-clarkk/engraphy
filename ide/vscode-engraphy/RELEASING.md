# Releasing the extension

A release is one action: **Actions > Release extension > Run workflow**, from
`main`, with the version to publish.

Before you run it, write that version's release notes as a `## <version>`
section in `CHANGELOG.md`. A `(unreleased)` marker on the heading is fine; the
release drops it.

The run then:

1. checks the version: plain `x.y.z`, no older than `package.json`, not already
   tagged, and with its changelog section;
2. runs `check-types` and `test-client`;
3. commits the version bump on `release/ide-v<version>` and tags that commit
   `ide-v<version>`;
4. packages the extension and publishes that package to the VS Code
   Marketplace;
5. opens a pull request that carries the bump into `main`, with `ci` already
   running on it. Merge it like any other. A merge commit keeps the tagged
   commit in `main`'s history.

Nothing is pushed to `main`, so the `main: CI must pass` ruleset governs a
release the same way it governs every other change. When `main` already
carries the version and its heading, there is nothing to bump: the tag goes on
`main`'s commit and no pull request is opened.

## One-time setup

- `VSCE_PAT`, a Marketplace personal access token, stored as a repository
  secret (Settings > Secrets and variables > Actions). It is set.
- Settings > Actions > General > Workflow permissions: tick **Allow GitHub
  Actions to create and approve pull requests**, so the run opens the pull
  request itself. Without it the release still publishes, and the run's
  summary links the branch, one click from the pull request.

## When a step fails

- Before the tag is pushed: fix the cause and run the workflow again. Nothing
  was pushed.
- Publishing, after the tag is pushed: open the run and choose **Re-run failed
  jobs**. The tag stays, and publishing and the pull request run again from it.
  A fresh run of the whole workflow is refused, because the tag exists.
- To publish an existing tag again: **Actions > ide-publish > Run workflow**,
  with the tag.

## Trying it without releasing

Tick **dry run**. It can start from any branch. It checks, tests, commits and
packages, uploads the package as a run artifact, and pushes, tags, publishes
and opens nothing.
