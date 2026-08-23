"""Generator for the exhaustive visibility matrix (design/06 §Testing).

Dimensions:  visibility in {private, team-read, team-write}
           x grant in {none, read, write}
           x actor in {owner, member, space_admin-as-member}
           x operation in {select, insert, update, edge_create_one_end,
                           edge_read_both_ends, traverse_through, dedup_candidate}

Emits (case_id, setup, operation, expect_allow) tuples consumed by
test_visibility_matrix.py against raw SQL (RLS truth before server truth).
Expected outcomes derive MECHANICALLY from design/06 §Visibility model — if any
combination feels ambiguous while implementing, that is a QUESTIONS.md entry,
not a judgment call.

Derivation (mirrors engraphy_readable_scopes()/engraphy_writable_scopes() in
design/implementation/visibility-and-rls-plan.md verbatim — these two functions
are the SINGLE AUTHORITY; nothing here may diverge from their bodies):

    readable(visibility, is_owner, grant)
        = visibility in ('team-read', 'team-write')
          or is_owner
          or grant in ('read', 'write')       # any grant row grants read, level unchecked

    writable(visibility, is_owner, grant)
        = visibility == 'team-write'
          or is_owner
          or grant == 'write'                 # only a write-level grant grants write

Trap this file exists to catch: **the role dimension (`member` vs
`space_admin-as-member`) must never change an outcome.** Neither SQL function
references `principals.role` — a space_admin's extra power is over membership,
tokens, and visibility *settings* (design/06 §Space administration), never over
data reads/writes. If a future edit makes `space_admin-as-member` diverge from
`member` for the same (visibility, grant), that is a leak, not a feature — the
`role_is_never_a_read_exception` check below fails loudly rather than silently
encoding it as "expected".

Per-operation reduction: every scenario pairs the scope under test (`X`, whose
visibility/grant/actor vary) against a second, FIXED endpoint the actor always
owns outright (their own private scope — always readable and writable to them).
Under that fixed pairing the edge/traversal/dedup rules in design/06
mechanically reduce as follows (shown, not assumed — see `_expect` below):

  - select                = readable(X)
  - insert                = writable(X)
  - update                = readable(X) and writable(X)          (USING + WITH CHECK)
  - edge_create_one_end   = readable(X) and (writable(X) or writable(own))  == readable(X)
  - edge_read_both_ends   = readable(X) and readable(own)                  == readable(X)
  - traverse_through      = readable(X)          (a walk continues only through readable hops)
  - dedup_candidate       = readable(X)          ("exactly the read surface of the write's context")

Several operations collapse to the same boolean as `select` given this fixed
pairing — that is a real, mechanically-derived consequence of the rules (not an
oversight): the *value* is simple, but the SQL machinery producing it for edges
and traversal (join-through-both-endpoints, CTE hop filtering) is materially
different code from the `nodes` SELECT policy, so a bug in that different code
path is still caught even though the expected answer is the same number.
"""

from itertools import product

VISIBILITIES = ("private", "team-read", "team-write")
GRANTS = ("none", "read", "write")
ACTORS = ("owner", "member", "space_admin-as-member")
OPERATIONS = (
    "select",
    "insert",
    "update",
    "edge_create_one_end",
    "edge_read_both_ends",
    "traverse_through",
    "dedup_candidate",
)


def _readable(visibility: str, is_owner: bool, grant: str) -> bool:
    if visibility in ("team-read", "team-write"):
        return True
    if is_owner:
        return True
    return grant in ("read", "write")


def _writable(visibility: str, is_owner: bool, grant: str) -> bool:
    if visibility == "team-write":
        return True
    if is_owner:
        return True
    return grant == "write"


def _expect(operation: str, visibility: str, is_owner: bool, grant: str) -> bool:
    readable_x = _readable(visibility, is_owner, grant)
    writable_x = _writable(visibility, is_owner, grant)
    # The fixed second endpoint ("own") is always owner-true, any grant value.
    readable_own = _readable("private", True, "none")
    writable_own = _writable("private", True, "none")

    if operation == "select":
        return readable_x
    if operation == "insert":
        return writable_x
    if operation == "update":
        return readable_x and writable_x
    if operation == "edge_create_one_end":
        return readable_x and (writable_x or writable_own)
    if operation == "edge_read_both_ends":
        return readable_x and readable_own
    if operation == "traverse_through":
        return readable_x
    if operation == "dedup_candidate":
        return readable_x
    raise ValueError(f"unknown operation: {operation}")  # pragma: no cover


def generate_cases():
    """Yield the exhaustive matrix as plain dicts.

    Each dict: {case_id, visibility, grant, actor, actor_role, is_owner,
    operation, expect_allow, setup}. `setup` is the raw-SQL scenario a test
    builds: which principal owns the scope, the scope's visibility, the
    scope_grants row for the actor (if `grant != 'none'`), and the actor's
    `principals.role` (space_admin for the third actor value, member
    otherwise) — role is threaded through ONLY so the trap check below can
    prove it is inert, never because a test should branch on it.
    """
    for visibility, grant, actor in product(VISIBILITIES, GRANTS, ACTORS):
        is_owner = actor == "owner"
        actor_role = "space_admin" if actor == "space_admin-as-member" else "member"
        # Grant rows are meaningless for the owner (owner access never checks
        # scope_grants) — still emitted for a fully exhaustive cartesian
        # product; the assertion below confirms grant is inert for owners too.
        for operation in OPERATIONS:
            expect_allow = _expect(operation, visibility, is_owner, grant)
            case_id = f"{visibility}__grant-{grant}__{actor}__{operation}"
            yield {
                "case_id": case_id,
                "visibility": visibility,
                "grant": grant,
                "actor": actor,
                "actor_role": actor_role,
                "is_owner": is_owner,
                "operation": operation,
                "expect_allow": expect_allow,
                "setup": {
                    "scope_visibility": visibility,
                    "scope_owner_principal": "owner-principal" if not is_owner else "actor-principal",
                    "actor_principal": "actor-principal",
                    "actor_role": actor_role,
                    "scope_grant": None if grant == "none" else {
                        "principal": "actor-principal",
                        "level": grant,
                    },
                },
            }


def role_is_never_a_read_exception():
    """Assert `member` and `space_admin-as-member` never diverge, for any
    (visibility, grant, operation). Raises AssertionError with the first
    counterexample; called by test_visibility_matrix.py as a standalone
    regression guard in addition to the per-case assertions.
    """
    by_key = {}
    for case in generate_cases():
        key = (case["visibility"], case["grant"], case["operation"])
        if case["actor"] == "owner":
            continue
        by_key.setdefault(key, {})[case["actor"]] = case["expect_allow"]
    for key, outcomes in by_key.items():
        member = outcomes.get("member")
        admin = outcomes.get("space_admin-as-member")
        if member != admin:
            raise AssertionError(
                f"role leaked into a data-access decision at {key}: "
                f"member={member} space_admin-as-member={admin}"
            )
    return True


if __name__ == "__main__":
    cases = list(generate_cases())
    print(f"{len(cases)} cases generated")
    role_is_never_a_read_exception()
    print("role_is_never_a_read_exception: OK")
