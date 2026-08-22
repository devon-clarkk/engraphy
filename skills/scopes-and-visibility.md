# Scopes and visibility

Memory in Engraphy lives in **scopes**. A scope is a named container with its own
visibility. To write to the right place and to understand what you can and can't
see, you need the basics of this model — you do not need its internals.

## Your identity is your token — you never name it

Your bearer token binds you to a **space** (a hard isolation boundary) and a
**principal** (who you are within it). **No tool takes a space or principal
argument.** You cannot read another space, and you cannot act as another
principal — not because it's forbidden per call, but because there's no way to
express it. Everything you read and write is automatically filtered to what your
principal is allowed to see and do.

Because of this, every memory you get back is attributed: its `scope` and its
`author` are always visible, so "who recorded this" is answerable in shared
contexts.

## Scopes and their visibility

Use `scope_list()` to see the scopes you can read. Each has a `visibility`:

| Visibility | Who can read it | Who can write it |
|------------|-----------------|------------------|
| `private` (default) | only the owner | only the owner |
| `team-read` | owner + teammates | only the owner |
| `team-write` | owner + teammates | owner + teammates |

- Your **personal scope** (`personal-<your-principal>`) is private and *ambient*
  — ambient means it rides along on all your queries automatically, so your own
  preferences, habits, and self-knowledge are always in play without your naming
  the scope. On a fresh single-person space this personal scope may be your only
  writable scope at first.
- **`private` means invisible, including its existence.** You cannot see a
  teammate's private scopes or the memory in them, in any read path — not in
  search, not in traversal, not in edge lists.
- **`team-read`** is how someone opens their memory to teammates ("my
  professional memory, open to the team") while keeping write control.
- **`team-write`** is shared project context — one graph everyone edits, with
  updates visible to all immediately.

`search(scope: "all")` spans every scope you can read — which naturally includes
teammates' `team-read` and shared `team-write` scopes, without you knowing their
names. That is the intended way to "check what the team knows."

## Writing to the right scope

When you `write`, choose a scope you can write to:

- Personal facts, drafts, your own working memory → your personal scope.
- Shared project or team knowledge → the relevant `team-write` scope.

If you name a scope that doesn't exist, that you can't see, or that you can see
but can't write to, the write is refused with **`SCOPE_UNKNOWN`** — and the
message is deliberately the same for all three cases (existence is information;
the engine won't confirm a scope you lack access to by giving a different error).
`scope_list` shows you what's actually writable.

To make a new private scope of your own: `scope_create(id, display_name,
confirm: true)`. New scopes are **private to their creator** by default. (Opening
a scope up — changing visibility, granting another principal access — and adding
members or minting tokens are administrative actions restricted to space
administrators; if you're not one, those tools return a role error. That is
expected, not a bug.)

## What "not found" means

Across every tool, a permission failure on a specific id looks **identical to
the id not existing** — `get` returns it under `missing`, other tools simply omit
it. Engraphy never says "you're not allowed to see this," because saying so would
itself leak that the thing exists. Treat a not-found as "not available to me" and
move on; don't retry it as if it were a transient error.
