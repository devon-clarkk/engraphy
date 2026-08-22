"""engraphy.server.tools.scopes — the scope_list/scope_guide/scope_create MCP tool
dispatchers. Core fns are exhaustively tested in test_scopes.py; these tests
cover the tool layer's own responsibilities (arg mapping, error translation).
"""
import pytest

from engraphy.server.auth import AuthContext, ToolError
from engraphy.server.tools.scopes import scope_create, scope_guide, scope_list
from engraphy.tests.test_dedup import write_space  # noqa: F401

_DESC = "A scope for the tool-layer tests."


def _ctx(space_id, principal="p1", role="readwrite"):
    return AuthContext("t1", space_id, principal, "pytest-client", role)


async def test_scope_list_tool_happy_path(pool, write_space):
    result = await scope_list(pool, _ctx(write_space), {})
    assert result["v"] == 1
    assert "scope1" in [s["id"] for s in result["scopes"]]


async def test_scope_guide_tool_happy_path(pool, write_space):
    result = await scope_guide(pool, _ctx(write_space), {})
    assert result["v"] == 1
    assert result["space"] == write_space
    assert "scope1" in [s["id"] for s in result["scopes"]]


async def test_scope_create_tool_happy_path(pool, write_space):
    result = await scope_create(pool, _ctx(write_space), {
        "id": "new-scope", "display_name": "New Scope", "description": _DESC, "confirm": True,
    })
    assert result["scope"]["id"] == "new-scope"
    assert result["scope"]["owner_principal"] == "p1"
    assert result["scope"]["description"] == _DESC


async def test_scope_create_tool_missing_confirm_translates_to_validation_error(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await scope_create(pool, _ctx(write_space), {
            "id": "new-scope", "display_name": "New Scope", "description": _DESC,
        })
    assert exc_info.value.code == "VALIDATION"


async def test_scope_create_tool_missing_description_translates_to_validation_error(pool, write_space):
    # A dispatcher-level caller that omits description (bypassing the wire
    # required-check) still collapses to the core's non-empty VALIDATION.
    with pytest.raises(ToolError) as exc_info:
        await scope_create(pool, _ctx(write_space), {
            "id": "new-scope", "display_name": "New Scope", "confirm": True,
        })
    assert exc_info.value.code == "VALIDATION"


async def test_scope_create_tool_duplicate_id_translates_to_validation_error(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await scope_create(pool, _ctx(write_space), {
            "id": "scope1", "display_name": "Dup", "description": _DESC, "confirm": True,
        })
    assert exc_info.value.code == "VALIDATION"
