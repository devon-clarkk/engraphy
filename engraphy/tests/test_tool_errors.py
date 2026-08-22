"""engraphy.server.tools.errors.to_tool_error — the KeyError branch specifically
(design/07's error codes for the typed-exception/CheckViolation branches are
already exercised indirectly through every other tool test)."""
from engraphy.server.tools.errors import to_tool_error


def test_key_error_translates_to_validation_naming_the_missing_key():
    try:
        {}["scope"]
    except KeyError as exc:
        result = to_tool_error(exc)
    assert result.code == "VALIDATION"
    assert "scope" in result.message
