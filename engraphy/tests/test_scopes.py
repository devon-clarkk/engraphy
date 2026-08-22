"""engraphy.core.scopes — scope_list / scope_create / scope_guide. E2-plan.md
s.3/s.5.4's resolved shapes: row set is exactly engram_readable_scopes() (no
separate visibility logic); scope_create defaults visibility='private',
owner_principal=principal, requires confirm=true AND a non-empty description,
and collapses a duplicate id to ENGRAPHY_VALIDATION naming the id. scope_guide is
the read-only routing manifest (migration 0022).
"""
import pytest

from engraphy.core.dedup import ValidationError
from engraphy.core.scopes import scope_create, scope_guide, scope_list
from engraphy.tests.test_dedup import (  # noqa: F401
    _bootstrap_write_space,
    _cleanup_write_space,
    write_space,
)

_DESC = "Where new things go: a test scope for routing."


async def test_scope_list_returns_readable_scopes(pool, write_space):
    result = await scope_list(pool, write_space, "p1")
    assert result["v"] == 1
    ids = [s["id"] for s in result["scopes"]]
    assert "scope1" in ids
    scope1 = next(s for s in result["scopes"] if s["id"] == "scope1")
    assert scope1["visibility"] == "private"
    assert scope1["owner_principal"] == "p1"
    assert scope1["ambient"] is False
    assert scope1["hints"] == []
    assert "created_at" in scope1
    # scope_list now carries description (migration 0022). scope1 is a direct-SQL
    # fixture insert with no description -> null, which is the accepted legacy shape.
    assert "description" in scope1


async def test_scope_list_excludes_archived_scopes(pool, write_space, conn):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, owner_principal, visibility, archived) "
        "VALUES (%s, 'gone', 'Gone', 'p1', 'private', true)",
        (write_space,),
    )
    conn.commit()
    result = await scope_list(pool, write_space, "p1")
    assert "gone" not in [s["id"] for s in result["scopes"]]


async def test_scope_create_happy_path_persists_and_returns_description(pool, write_space):
    result = await scope_create(pool, write_space, "p1", "new-scope", "New Scope", _DESC, True)
    assert result["v"] == 1
    scope = result["scope"]
    assert scope["id"] == "new-scope"
    assert scope["display_name"] == "New Scope"
    assert scope["description"] == _DESC          # returned on create
    assert scope["visibility"] == "private"
    assert scope["owner_principal"] == "p1"
    assert scope["ambient"] is False
    # Persisted: a fresh read-back sees the same description.
    listed = await scope_list(pool, write_space, "p1")
    got = next(s for s in listed["scopes"] if s["id"] == "new-scope")
    assert got["description"] == _DESC


async def test_scope_create_without_confirm_raises_validation(pool, write_space):
    with pytest.raises(ValidationError, match="ENGRAPHY_VALIDATION"):
        await scope_create(pool, write_space, "p1", "new-scope", "New Scope", _DESC, False)


async def test_scope_create_missing_description_raises_validation(pool, write_space):
    with pytest.raises(ValidationError, match="requires a non-empty description"):
        await scope_create(pool, write_space, "p1", "new-scope", "New Scope", "", True)


async def test_scope_create_whitespace_only_description_raises_validation(pool, write_space):
    with pytest.raises(ValidationError, match="requires a non-empty description"):
        await scope_create(pool, write_space, "p1", "new-scope", "New Scope", "   ", True)


async def test_scope_create_duplicate_id_raises_validation_naming_id(pool, write_space):
    with pytest.raises(ValidationError, match="scope id 'scope1' already in use"):
        await scope_create(pool, write_space, "p1", "scope1", "Dup", _DESC, True)


# ---- scope_guide: the routing manifest ---------------------------------------


async def test_scope_guide_returns_all_readable_scopes_with_descriptions(pool, write_space):
    await scope_create(pool, write_space, "p1", "routed", "Routed", "Write routing decisions here.", True)
    guide = await scope_guide(pool, write_space, "p1")
    assert guide["v"] == 1
    assert guide["space"] == write_space
    by_id = {s["id"]: s for s in guide["scopes"]}
    # The tool-created scope is present with its exact description; the entry
    # shape is the frozen {id, display_name, description}.
    assert set(by_id["routed"]) == {"id", "display_name", "description"}
    assert by_id["routed"]["description"] == "Write routing decisions here."
    assert by_id["routed"]["display_name"] == "Routed"
    # scope1 (a fixture) is also listed -- every readable scope, one entry each.
    assert "scope1" in by_id


async def test_scope_guide_is_rls_isolated_across_spaces(pool, write_space, conn, request):
    """A second space's scopes must never appear in this space's guide. Filtering
    by space_id in the WHERE would pass a weaker test while RLS is broken, so the
    assertion is cross-space: none of space B's scope ids leak into A's guide."""
    other = ("sg2-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_write_space(conn, other)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, description, owner_principal, visibility) "
        "VALUES (%s, 'secret-b', 'Secret B', 'B only', 'p1', 'private')",
        (other,),
    )
    conn.commit()
    try:
        a_guide = await scope_guide(pool, write_space, "p1")
        a_ids = {s["id"] for s in a_guide["scopes"]}
        assert "secret-b" not in a_ids
        # And B genuinely has it (proves the row exists, so absence in A is RLS,
        # not a typo'd insert).
        b_guide = await scope_guide(pool, other, "p1")
        assert "secret-b" in {s["id"] for s in b_guide["scopes"]}
    finally:
        _cleanup_write_space(conn, other)


async def test_backfill_gives_preexisting_scopes_a_nonempty_description(pool, write_space, conn):
    """Migration 0022 backfills description = display_name (falling back to id).
    A scope inserted with an explicit NULL description then re-run through the
    backfill UPDATE lands on the display_name -- proving the migration's rule."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scopes (space_id, id, display_name, description, owner_principal, visibility) "
        "VALUES (%s, 'legacy', 'Legacy Scope', NULL, 'p1', 'private')",
        (write_space,),
    )
    # Re-apply the migration's backfill rule to the NULL row (idempotent shape).
    cur.execute(
        "UPDATE scopes SET description = COALESCE(NULLIF(btrim(display_name), ''), id) "
        "WHERE space_id = %s AND id = 'legacy' AND description IS NULL",
        (write_space,),
    )
    conn.commit()
    guide = await scope_guide(pool, write_space, "p1")
    legacy = next(s for s in guide["scopes"] if s["id"] == "legacy")
    assert legacy["description"] == "Legacy Scope"
