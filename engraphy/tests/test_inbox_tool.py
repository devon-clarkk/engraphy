"""engraphy.server.tools.inbox.inbox_review — the list/promote/discard MCP
tool dispatcher. Core fns (capture/list_pending/promote/discard) are
exhaustively tested in test_inbox.py; these tests cover the tool layer's own
responsibilities (action dispatch, arg mapping, error translation).
"""
import pytest

from engraphy.core.inbox import capture
from engraphy.server.auth import AuthContext, ToolError
from engraphy.server.tools.inbox import inbox_review
from engraphy.tests.test_dedup import write_space  # noqa: F401


def _ctx(space_id, principal="p1", role="readwrite"):
    return AuthContext("t1", space_id, principal, "pytest-client", role)


async def test_inbox_review_list_happy_path(pool, write_space):
    await capture(pool, write_space, "p1", "note", {"text": "a captured thought"})
    result = await inbox_review(pool, _ctx(write_space), {"action": "list"})
    assert result["v"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["kind"] == "note"
    assert result["items"][0]["status"] == "pending"
    assert result["truncated"] is False


async def test_inbox_review_discard_happy_path(pool, write_space):
    captured = await capture(pool, write_space, "p1", "note", {"text": "a captured thought"})
    result = await inbox_review(pool, _ctx(write_space), {"action": "discard", "id": captured["id"]})
    assert result == {"v": 1, "outcome": "discarded", "id": captured["id"]}


async def test_inbox_review_promote_happy_path(pool, write_space):
    captured = await capture(pool, write_space, "p1", "note", {"text": "a captured thought"})
    result = await inbox_review(pool, _ctx(write_space), {
        "action": "promote", "id": captured["id"], "type": "widget", "scope": "scope1",
        "title": "Promoted title", "body": "Promoted body.",
    })
    assert result["outcome"] == "inserted"
    assert result["node"]["title"] == "Promoted title"


async def test_inbox_review_invalid_action_translates_to_validation_error(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await inbox_review(pool, _ctx(write_space), {"action": "bogus"})
    assert exc_info.value.code == "VALIDATION"


async def test_inbox_review_discard_unknown_id_translates_to_not_found(pool, write_space):
    with pytest.raises(ToolError) as exc_info:
        await inbox_review(pool, _ctx(write_space), {
            "action": "discard", "id": "00000000-0000-4000-8000-000000000000",
        })
    assert exc_info.value.code == "NOT_FOUND"
