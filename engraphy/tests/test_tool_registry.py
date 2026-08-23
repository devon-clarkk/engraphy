"""engraphy.server.tool_registry -- per-space tool assembly and alias
resolution, tested directly (test_app.py exercises the same code indirectly
through a real MCP session)."""
from engraphy.server.tool_registry import CORE_DISPATCH, list_tools_for_space, resolve_dispatch
from engraphy.tests.test_dedup import write_space  # noqa: F401


async def test_list_tools_for_space_returns_all_core_tools_with_base_descriptions(pool, write_space):
    entries = await list_tools_for_space(pool, write_space)
    names = {e["name"] for e in entries}
    # Every core tool appears; the four admin_* tools are also present by default
    # (space_admin_tools unset -> enabled) and are covered by test_admin.py.
    assert set(CORE_DISPATCH) <= names
    write_entry = next(e for e in entries if e["name"] == "write")
    assert write_entry["description"].startswith("Dedup-banded write")
    assert write_entry["inputSchema"]["required"] == ["body", "scope", "title", "type"]
    # traverse's `direction` is required: core_traverse raises on a missing/
    # invalid value (design/07), so the documentary schema must declare it even
    # though the dispatcher reads it via arguments.get().
    traverse_entry = next(e for e in entries if e["name"] == "traverse")
    assert traverse_entry["inputSchema"]["required"] == ["direction", "start_id"]


async def test_list_tools_for_space_applies_pack_description_override(pool, write_space, conn):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, 'pack.tool_descriptions', %s::jsonb)",
        (write_space, '{"write": "Custom write description."}'),
    )
    conn.commit()
    entries = await list_tools_for_space(pool, write_space)
    write_entry = next(e for e in entries if e["name"] == "write")
    assert write_entry["description"] == "Custom write description."
    # An untouched tool keeps its base description.
    get_entry = next(e for e in entries if e["name"] == "get")
    assert get_entry["description"].startswith("Full nodes")


async def test_list_tools_for_space_includes_pack_aliases(pool, write_space, conn):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, 'pack.tool_aliases', %s::jsonb)",
        (write_space, ('{"log_error": {"binds": "write", "preset": {"type": "error"}, '
                       '"description": "Record something that went wrong."}}')),
    )
    conn.commit()
    entries = await list_tools_for_space(pool, write_space)
    alias_entry = next(e for e in entries if e["name"] == "log_error")
    assert alias_entry["description"] == "Record something that went wrong."
    assert alias_entry["inputSchema"] == next(e for e in entries if e["name"] == "write")["inputSchema"]


async def test_resolve_dispatch_direct_core_tool_has_no_action_override(pool, write_space):
    resolved = await resolve_dispatch(pool, write_space, "write", {"scope": "scope1"})
    core_name, dispatcher, merged_args, action = resolved
    assert core_name == "write"
    assert dispatcher is CORE_DISPATCH["write"]
    assert merged_args == {"scope": "scope1"}
    assert action is None


async def test_resolve_dispatch_unknown_tool_name_returns_none(pool, write_space):
    assert await resolve_dispatch(pool, write_space, "not_a_tool", {}) is None


async def test_resolve_dispatch_alias_merges_preset_and_carries_audit_identity(pool, write_space, conn):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, 'pack.tool_aliases', %s::jsonb)",
        (write_space, '{"log_error": {"binds": "write", "preset": {"type": "error"}}}'),
    )
    conn.commit()
    resolved = await resolve_dispatch(
        pool, write_space, "log_error", {"scope": "scope1", "type": "widget"},
    )
    core_name, dispatcher, merged_args, action = resolved
    assert core_name == "write"
    assert dispatcher is CORE_DISPATCH["write"]
    assert merged_args["type"] == "error"  # preset wins over the caller's "widget"
    assert action == "write via log_error"


# ---- Generated schemas, and per-space isolation of them ----------------------


async def test_published_schemas_are_generated_from_the_wire_type_spec(pool, write_space):
    """The documentary `{name: {}}` body is gone: every property now carries the
    type the server actually enforces, because both come from wire_types.SPEC
    (ruled 2026-07-21). Advertised and enforced surfaces cannot drift because
    there is only one table."""
    entries = await list_tools_for_space(pool, write_space)
    schemas = {e["name"]: e["inputSchema"] for e in entries}

    assert schemas["search"]["properties"]["limit"] == {"type": "integer"}
    assert schemas["search"]["properties"]["detail"]["enum"] == ["full", "summary"]
    assert schemas["write"]["properties"]["attrs"] == {"type": "object"}
    assert schemas["get"]["properties"]["ids"]["items"]["format"] == "uuid"
    assert all(s["additionalProperties"] is False for s in schemas.values())

    # The three hand-maintained dicts this replaced included a separate one for
    # admin args; the admin tools now come from the same place as the rest.
    assert schemas["admin_token_create"]["properties"]["role"]["enum"] == [
        "readwrite", "readonly"]
    assert schemas["admin_token_create"]["required"] == [
        "client_name", "principal", "role"]


async def test_inbox_review_advertises_only_unconditional_requirements(pool, write_space):
    """A flat schema cannot say "type is required when action is promote".
    Advertising the promote fields as required would overstate the surface --
    drift in the stricter direction, which generating from one spec is meant to
    prevent just as much as the lenient direction."""
    entries = await list_tools_for_space(pool, write_space)
    schema = next(e for e in entries if e["name"] == "inbox_review")["inputSchema"]
    assert schema["required"] == ["action"]
    assert "type" in schema["properties"] and "id" in schema["properties"]


async def test_two_spaces_with_different_packs_get_different_tool_lists_and_no_schema_bleed(
        pool, write_space, conn, request):
    """`wire_types.SPEC` is module-global while tool lists are per-space, so a
    generated schema handed to one space must never be an object another space's
    call can mutate. This is the same class of hazard as the SDK's process-global
    `_tool_cache` -- the reason its validation path stays off -- so it is pinned
    here rather than assumed.

    Also the plain isolation property: each space serves its own pack's aliases
    and none of the other's.
    """
    from engraphy.tests.test_dedup import _bootstrap_write_space, _cleanup_write_space

    other = ("wr2-" + request.node.name.replace("_", "-"))[:60]
    _bootstrap_write_space(conn, other)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, 'pack.tool_aliases', %s::jsonb)",
        (write_space, '{"log_error": {"binds": "write", "preset": {"type": "error"}}}'),
    )
    cur.execute(
        "INSERT INTO config (space_id, key, value) VALUES (%s, 'pack.tool_aliases', %s::jsonb)",
        (other, '{"note_it": {"binds": "write", "preset": {"type": "widget"}}}'),
    )
    conn.commit()
    try:
        a_entries = await list_tools_for_space(pool, write_space)
        b_entries = await list_tools_for_space(pool, other)
        a_names = {e["name"] for e in a_entries}
        b_names = {e["name"] for e in b_entries}
        assert "log_error" in a_names and "log_error" not in b_names
        assert "note_it" in b_names and "note_it" not in a_names

        # Mutating one space's published schema must not reach the other's, nor
        # the spec both were generated from.
        a_search = next(e for e in a_entries if e["name"] == "search")["inputSchema"]
        a_search["properties"]["limit"]["type"] = "corrupted"
        a_search["required"].append("bogus")

        fresh = await list_tools_for_space(pool, other)
        b_search = next(e for e in fresh if e["name"] == "search")["inputSchema"]
        assert b_search["properties"]["limit"]["type"] == "integer"
        assert "bogus" not in b_search["required"]
    finally:
        _cleanup_write_space(conn, other)
