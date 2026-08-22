"""Per-run space and per-haystack scope provisioning (design/09 §Isolation).

The isolation model, in one place:

* **One space per run.** The space id embeds the run id, so a rerun can never
  contaminate a prior one and an aborted run can be dropped wholesale.
* **One scope per haystack.** Dedup candidate queries are scope-filtered, so
  facts from conversation A cannot become merge candidates for conversation B.
* **The bench pack declares no ambient scopes**, which is what makes the point
  above true rather than merely intended -- an ambient scope would be unioned
  into every candidate query regardless of the scope filter.

`assert_no_ambient_scopes` is called on every provision rather than trusted from
the YAML: the pack file is one careless edit away from re-introducing
cross-haystack leakage, and the symptom would look like an unusually good dedup
result rather than like a bug.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

from engraphy.admin import packs

__all__ = [
    "BENCH_PACK_PATH",
    "RunSpace",
    "assert_no_ambient_scopes",
    "load_bench_pack",
    "provision_run_space",
    "scope_id_for",
]

BENCH_PACK_PATH = pathlib.Path(__file__).resolve().parents[1] / "pack" / "bench-pack.yaml"
_REPO = pathlib.Path(__file__).resolve().parents[2]

# The packs the run dimension can select, by name. These are the SHIPPED packs,
# used verbatim except for one isolation-required transform (below). Their file
# hashes go in the manifest so "we ran the shipped starter/conversational pack"
# is verifiable against the committed files.
PACK_FILES: dict[str, pathlib.Path] = {
    "starter": _REPO / "packs" / "starter" / "pack.yaml",
    "conversational": _REPO / "packs" / "conversational" / "pack.yaml",
    "bench": BENCH_PACK_PATH,
}

# Space, scope and principal ids all share one DDL constraint:
#   CHECK (id ~ '^[a-z0-9][a-z0-9-]{1,62}$')
# Hyphens, not underscores, and 63 characters total. Dataset ids routinely
# contain ':', '/', '_' and spaces (LoCoMo's "conv-26", LongMemEval's
# "gpt4_1234"), so they are slugged rather than passed through.
_SAFE = re.compile(r"[^a-z0-9]+")
_ID_MAX = 63


class IsolationError(RuntimeError):
    """The isolation model has been violated or cannot be guaranteed."""


@dataclass(frozen=True, slots=True)
class RunSpace:
    """Everything the ingest and retrieval paths need to address the store."""

    space_id: str
    principal: str
    pack_name: str
    pack_version: int

    def scope_for(self, haystack_id: str) -> str:
        return scope_id_for(haystack_id)


def load_bench_pack(path: pathlib.Path | None = None) -> dict:
    """Load and validate the bench pack, refusing an ambient-scope declaration."""
    pack = packs.load_pack_file(path or BENCH_PACK_PATH)
    errors = packs.validate(pack)
    if errors:
        raise IsolationError(f"bench pack does not validate: {errors}")
    assert_no_ambient_scopes(pack)
    return pack


def load_named_pack(name: str) -> tuple[dict, bool]:
    """Load a shipped pack by name for the benchmark, stripping ambient scopes.

    Returns `(pack, ambient_stripped)`.

    **The strip is an isolation requirement, applied identically to every pack,
    and it is not tuning.** Both the starter and conversational packs ship
    `ambient_scopes: [personal]`. Under an ambient scope, `ambient_scope_set_async`
    unions that scope into *every* write's dedup candidate set, so conversation
    A's facts would become merge candidates for conversation B and both the dedup
    and accuracy numbers would be silently corrupted -- the exact leak
    `assert_no_ambient_scopes` exists to catch, and the reason the bench pack was
    created ambient-free in the first place. Removing it is symmetric across the
    packs being compared, so it cannot favour one, and it is recorded in the
    manifest alongside the untouched file hash. The ontology under test -- node
    types, edges, attrs, briefing -- is left exactly as shipped.
    """
    path = PACK_FILES.get(name)
    if path is None:
        raise IsolationError(f"unknown pack {name!r}; known: {sorted(PACK_FILES)}")
    pack = packs.load_pack_file(path)
    ambient_stripped = bool(pack.pop("ambient_scopes", None))
    errors = packs.validate(pack)
    if errors:
        raise IsolationError(f"pack {name!r} does not validate: {errors}")
    assert_no_ambient_scopes(pack)
    return pack, ambient_stripped


def pack_file_hash(name: str) -> str:
    """SHA-256 of a pack's shipped file (before the ambient strip), for the
    manifest. The strip is a documented in-memory transform; the hash pins the
    file a reader can inspect."""
    import hashlib

    path = PACK_FILES[name]
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def assert_no_ambient_scopes(pack: dict) -> None:
    """The load-bearing check (design/09 §Isolation).

    An ambient scope is unioned into the candidate set of every write via
    `ambient_scope_set_async`, which would let one haystack's facts become merge
    candidates for another's. Both the dedup and the accuracy numbers would be
    wrong, and wrong in the flattering direction.
    """
    declared = pack.get("ambient_scopes")
    if declared:
        raise IsolationError(
            f"bench pack declares ambient_scopes={declared!r}. Ambient scopes are unioned "
            "into every write's dedup candidate set, which leaks facts across haystacks and "
            "silently inflates the dedup result. Remove the key (design/09 §Isolation)."
        )


def _slug(raw: str, *, prefix: str, kind: str) -> str:
    slug = _SAFE.sub("-", raw.strip().lower()).strip("-")
    if not slug:
        raise IsolationError(f"{kind} {raw!r} has no usable characters for an id")
    out = f"{prefix}{slug}"[:_ID_MAX].rstrip("-")
    return out


def scope_id_for(haystack_id: str) -> str:
    """Deterministic scope id for a haystack.

    Deterministic rather than generated so that a rerun addresses the same
    scopes and a result row can be traced back to its haystack by hand.
    Truncation is why `provision_run_space` checks for collisions: two long
    haystack ids differing only past the limit would otherwise share a scope.
    """
    return _slug(haystack_id, prefix="hs-", kind="haystack id")


def provision_run_space(
    conn,
    *,
    run_id: str,
    haystack_ids: list[str],
    principal: str = "bench",
    pack_path: pathlib.Path | None = None,
    pack: dict | None = None,
    space_config: dict | None = None,
) -> RunSpace:
    """Create the run's space, principal, pack registry, and one scope per
    haystack, on a superuser (non-RLS) connection.

    Provisioning is admin work -- it writes `spaces`, `principals`, and the
    registry tables, none of which the app role may write. The ingest path that
    follows uses the app-role pool and real RLS, so nothing here weakens what is
    under test.

    `pack` may be an already-loaded, ambient-stripped pack dict (the pack
    dimension passes one per pack); otherwise the bench pack is loaded. A pack's
    node_types live at the SPACE level, so two packs cannot share one space --
    the caller gives each pack its own space (run id suffixed by pack name).

    `space_config`: optional per-space `config` rows (e.g. `{"dedup.t_high":
    0.98}`). Each key/value is upserted into the non-RLS `config` reference table
    the engine's `dedup._resolve_config` already reads on every write -- the
    sanctioned path for tuning a run space's governance values without touching
    engine code or plumbing `write(thresholds=…)`. Values are stored as jsonb,
    the exact shape `engraphy-admin config set` writes (Phase A, fact-searchability).
    """
    if pack is None:
        pack = load_bench_pack(pack_path)
    space_id = space_id_for(run_id)

    # Derive and collision-check every scope id BEFORE writing anything. Doing
    # it mid-provision would leave a half-built space behind on failure, and the
    # retry would then die on a duplicate pack apply rather than on the real
    # fault -- which is exactly how this ordering bug was found.
    scope_ids: dict[str, str] = {}
    for hid in haystack_ids:
        sid = scope_id_for(hid)
        if sid in scope_ids and scope_ids[sid] != hid:
            # Truncation or slugging collapsed two distinct haystacks onto one
            # scope. That would silently merge two corpora -- exactly the
            # contamination the scoping exists to prevent, so it is fatal.
            raise IsolationError(
                f"haystacks {scope_ids[sid]!r} and {hid!r} both map to scope {sid!r}; "
                "scope id derivation is lossy for this corpus"
            )
        scope_ids[sid] = hid

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO spaces (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (space_id, f"Benchmark run {run_id}"),
    )
    cur.execute(
        "INSERT INTO principals (space_id, id, display_name) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        (space_id, principal, "Benchmark harness"),
    )
    packs.apply(pack, space_id, cur)

    for sid, hid in scope_ids.items():
        cur.execute(
            "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility) "
            "VALUES (%s, %s, %s, %s, 'private') ON CONFLICT DO NOTHING",
            (space_id, sid, f"Haystack {hid}", principal),
        )

    _apply_space_config(cur, space_id, space_config)

    return RunSpace(
        space_id=space_id,
        principal=principal,
        pack_name=pack["pack"],
        pack_version=int(pack["version"]),
    )


def _apply_space_config(cur, space_id: str, space_config: dict | None) -> None:
    """Upsert per-space `config` rows, mirroring `engraphy-admin config set`'s
    encoding exactly (value as jsonb, ON CONFLICT DO UPDATE). No-op when None/empty.
    Runs on the same superuser provisioning connection; `config` is a non-RLS
    reference table, so no policy work is needed."""
    if not space_config:
        return
    from psycopg.types.json import Jsonb
    for key, value in space_config.items():
        cur.execute(
            "INSERT INTO config (space_id, key, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (space_id, key) DO UPDATE SET value = EXCLUDED.value",
            (space_id, key, Jsonb(value)),
        )


def space_id_for(run_id: str) -> str:
    return _slug(run_id, prefix="bench-", kind="run id")
