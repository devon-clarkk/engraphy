"""The verify-restore sentinel (design/04 s.Backup contract; DECISIONS-DELTA
2026-07-20 "verify-restore sentinel RESOLVED").

One reserved, engine-owned node per space, minted by `engraphy-admin space create`,
whose survival is what `verify-restore` asserts. It exists so the monthly restore
proof is *self-contained* -- before this, `verify-restore` could only check a node
id the operator remembered to supply and maintain, which is exactly the kind of
canary that rots unnoticed until the day it is needed.

Four properties, each load-bearing:

- **Reserved type** `engram_sentinel`, registered by `space create` itself rather
  than by a pack. Packs are otherwise the only writers of the registries, so this
  type is exempted everywhere that fact is assumed: `pack validate` refuses a pack
  declaring it, `doctor`'s registry-drift diff does not report it as drift, and
  `pack upgrade`'s destructive phase does not try to remove it.
- **`status='archived'` from birth.** Not a new invisibility mechanism -- archived
  is already filtered out of search, briefing and dedup candidacy by every read
  path, so the sentinel cannot surface to an agent, be returned as a duplicate
  candidate, or be merged into. Reusing the existing filter is the whole point:
  no new code can forget about it.
- **A deterministic constant unit vector** as its embedding, NOT a model output.
  `space create` must never need the ~523MB embedding model loaded (it is a
  bootstrap verb that runs before anything else, including in the admin sidecar,
  which has no reason to carry torch). Constant also means the value is identical
  in every space and every restore, so a corrupted restore shows up as a changed
  vector rather than as noise.
- **Its id in per-space config** under `sentinel.node_id`, so `verify-restore`
  locates it without being told.

`embedding_model` is deliberately a marker string, not `MODEL_ID`: a future
model migration re-embeds rows keyed on `embedding_model` (design/04 s.Engine
migrations), and the sentinel must never be swept into that -- its vector is
constant *by contract*, and re-embedding it would destroy the only property
verify-restore checks.
"""
import math

from engraphy.core.embedding import DIMS

#: Reserved node type name. Matches node_types' `^[a-z][a-z0-9_]{1,40}$` CHECK.
SENTINEL_NODE_TYPE = "engram_sentinel"

#: Node type names reserved for the engine, refused on BOTH the pack-authoring
#: path (`packs.validate_reserved_names`) and the write path
#: (`dedup._validate_not_reserved_type`) -- ruled 2026-07-21, and shared from
#: here precisely so those two refusals cannot drift apart. A name in this set
#: is writable only by the privileged `space create` CLI path; over the tool
#: surface it is `ENGRAPHY_VALIDATION`, never an insert.
RESERVED_NODE_TYPES = frozenset({SENTINEL_NODE_TYPE})

#: Per-space config key holding the sentinel node's uuid (design/04).
SENTINEL_CONFIG_KEY = "sentinel.node_id"

#: Marker in `nodes.embedding_model` -- see the module docstring on why this is
#: not the real model id.
SENTINEL_EMBEDDING_MODEL = "engraphy-sentinel-constant-v1"

SENTINEL_TYPE_DESCRIPTION = (
    "Engine-owned restore sentinel. Not writable by agents; not part of any pack."
)

#: Fixed content. Title obeys nodes' 3..200 CHECK, body its 1..8000 CHECK.
#: The text is addressed to whoever finds this row in a database and wonders
#: what it is -- that reader is the entire audience, since no agent can see it.
SENTINEL_TITLE = "Engraphy restore sentinel"
SENTINEL_BODY = (
    "This node is minted automatically when a space is created and exists only so "
    "that `engraphy-admin verify-restore` can prove a backup restored correctly. It is "
    "archived, so it never appears in search, briefings, or duplicate detection, and "
    "its embedding is a fixed constant rather than a model output. Do not edit or "
    "delete it: if it disappears, restore verification for this space silently "
    "degrades to a skip."
)

SENTINEL_ATTR_SPEC = {"attrs": {}}


def constant_unit_vector() -> list[float]:
    """The sentinel's embedding: every component equal, L2 norm exactly 1.

    Uniform rather than, say, a hash-derived vector because the requirement is
    determinism and model-independence, not distinctiveness -- the sentinel is
    archived, so it is never a search or dedup candidate and never competes with
    real content for similarity. `1/sqrt(DIMS)` per component is the simplest
    thing that is a genuine unit vector, which keeps it well-formed input for
    pgvector's cosine operator if anything ever does compare it.
    """
    return [1.0 / math.sqrt(DIMS)] * DIMS


def vector_literal() -> str:
    """`constant_unit_vector()` as a pgvector text literal, for the one INSERT
    that writes it (`space create`, which has no embedding pipeline in scope)."""
    return "[" + ",".join(repr(component) for component in constant_unit_vector()) + "]"
