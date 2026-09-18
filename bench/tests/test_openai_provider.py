"""The OpenAI-compatible route (`bench/core/providers.py`, `bench/RUN-LOCOMO.md`).

Hermetic by construction, like the rest of `bench/tests`: every test injects a
fake opener, so the whole request-building and response-parsing path runs with no
network and no credential. That matters more here than for the other clients --
this is the route a third party reproduces a published number on, so the request
shape is part of the published method and a change to it should fail a test
rather than surface as an unexplained score difference.

What is asserted is the wire, not the plumbing: which JSON body each structured
mode sends, which field the payload is read back from, and which HTTP failures
become a clean resumable stop rather than a wrong answer.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error

import pytest

from bench.core.judge import JUDGE_SCHEMA, Judge
from bench.core.llm import (
    OPENAI_ROLE_MODELS,
    LLMError,
    openai_model_for,
    openai_role_manifest,
)
from bench.core import providers
from bench.core.providers import (
    OPENAI_STRUCTURED_MODES,
    OPENAI_TOOL_NAME,
    OpenAICompatClient,
    QuotaExhausted,
    _looks_like_hard_quota,
    _retry_after,
)

@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch, tmp_path):
    """Neither the operator's shell nor the operator's `.env` may reach a test.

    `_setting` reads the environment and then the repo's gitignored `.env`, which
    on a machine that actually runs benchmarks holds real values. A test asserting
    what the pinned configuration is would then be asserting what that machine
    happens to be configured for, and would pass or fail by accident.
    """
    monkeypatch.setattr(providers, "ENV_PATH", tmp_path / "absent.env")
    for name in list(os.environ):
        if name.startswith("ENGRAPHY_BENCH_"):
            monkeypatch.delenv(name, raising=False)


SCHEMA = {
    "type": "object",
    "properties": {"correct": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["correct", "reason"],
    "additionalProperties": False,
}


def _response(message: dict, *, finish: str = "stop", model: str = "served-model-id",
              usage: dict | None = None) -> dict:
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7},
    }


class FakeOpener:
    """Stands in for `urllib.request.urlopen`.

    Records every request body it is handed and replays a scripted list of
    outcomes: a dict is returned as a 200, an exception is raised. That is enough
    to drive both the happy path and every retry branch without a socket.
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.bodies: list[dict] = []
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, req, timeout):
        self.bodies.append(json.loads(req.data.decode("utf-8")))
        self.urls.append(req.full_url)
        self.headers.append(dict(req.headers))
        if not self.outcomes:
            raise AssertionError("FakeOpener ran out of scripted outcomes")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _ctx(json.dumps(outcome).encode("utf-8"))


class _ctx:
    """Minimal context manager matching what `urlopen` yields."""

    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self.payload


def _http_error(code: int, body: str, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.test/v1/chat/completions", code, "err",
        headers or {}, io.BytesIO(body.encode("utf-8")),
    )


def _client(outcomes, **kw) -> tuple[OpenAICompatClient, FakeOpener]:
    opener = FakeOpener(outcomes)
    client = OpenAICompatClient(
        kw.pop("model", "test-model"),
        base_url=kw.pop("base_url", "https://example.test/v1"),
        api_key=kw.pop("api_key", "k-test"),
        opener=opener,
        max_retries=kw.pop("max_retries", 2),
        **kw,
    )
    return client, opener


# ------------------------------------------------------------- request shape
def test_a_plain_call_sends_system_and_user_and_no_response_format():
    """The reader is schema-less, and must not be sent a structured-output field.

    An endpoint handed `response_format` for a prose answer is entitled to
    constrain the reply to JSON, which would break every reader answer at once.
    """
    client, opener = _client([_response({"content": "Cairo."})])
    resp = client.complete("SYSTEM", "USER")

    body = opener.bodies[0]
    assert body["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]
    assert "response_format" not in body
    assert "tools" not in body
    assert resp.text == "Cairo."
    assert resp.data is None
    assert opener.urls[0] == "https://example.test/v1/chat/completions"
    assert opener.headers[0]["Authorization"] == "Bearer k-test"


def test_the_trailing_slash_on_a_base_url_does_not_double_up():
    """Anthropic's documented base URL ends in a slash and OpenAI's does not.

    Both have to produce one `/chat/completions`, because a doubled slash is a
    404 on some gateways and a silent redirect that drops the POST body on
    others.
    """
    client, opener = _client([_response({"content": "ok"})],
                             base_url="https://api.anthropic.com/v1/")
    client.complete("s", "u")
    assert opener.urls[0] == "https://api.anthropic.com/v1/chat/completions"


def test_temperature_is_absent_unless_configured():
    """Reasoning-tier models reject a temperature, and this harness never claimed
    determinism -- judge stability is measured, not pinned."""
    client, opener = _client([_response({"content": "ok"})])
    client.complete("s", "u")
    assert "temperature" not in opener.bodies[0]

    client, opener = _client([_response({"content": "ok"})], temperature="0")
    client.complete("s", "u")
    assert opener.bodies[0]["temperature"] == 0.0


# ------------------------------------------------------- structured output
def test_json_schema_mode_sends_response_format_and_parses_the_content():
    client, opener = _client(
        [_response({"content": '{"correct": true, "reason": "matches"}'})],
        structured="json_schema",
    )
    resp = client.complete("s", "u", schema=SCHEMA)

    fmt = opener.bodies[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == SCHEMA
    assert resp.data == {"correct": True, "reason": "matches"}


def test_json_schema_mode_does_not_set_strict():
    """`strict: true` would force every optional key in the extraction schema to
    be emitted on every memory, which changes what the extractor produces and so
    changes what the run measures. Conformance is checked by the pack validator."""
    client, opener = _client([_response({"content": "{}"})], structured="json_schema")
    client.complete("s", "u", schema=SCHEMA)
    assert opener.bodies[0]["response_format"]["json_schema"]["strict"] is False


def test_tool_call_mode_forces_one_named_call_and_reads_its_arguments():
    """The mode Anthropic's compatibility layer needs: it ignores
    `response_format` outright but supports a function's `parameters`."""
    arguments = '{"correct": false, "reason": "wrong city"}'
    client, opener = _client(
        [_response({"content": None,
                    "tool_calls": [{"id": "call_1", "type": "function",
                                    "function": {"name": OPENAI_TOOL_NAME,
                                                 "arguments": arguments}}]},
                   finish="tool_calls")],
        structured="tool_call",
    )
    resp = client.complete("s", "u", schema=SCHEMA)

    body = opener.bodies[0]
    assert body["tools"][0]["function"]["name"] == OPENAI_TOOL_NAME
    assert body["tools"][0]["function"]["parameters"] == SCHEMA
    assert body["tool_choice"] == {"type": "function",
                                   "function": {"name": OPENAI_TOOL_NAME}}
    assert "response_format" not in body
    assert resp.data == {"correct": False, "reason": "wrong city"}
    assert resp.text == arguments


def test_tool_call_mode_says_what_to_do_when_no_tool_call_comes_back():
    client, _ = _client([_response({"content": "Sure, the answer is yes."})],
                        structured="tool_call")
    with pytest.raises(LLMError, match="no tool call"):
        client.complete("s", "u", schema=SCHEMA)


def test_json_object_mode_puts_the_schema_in_the_system_prompt():
    """The shape is only asked for in this mode, so it has to reach the model."""
    client, opener = _client([_response({"content": '{"correct": true, "reason": "y"}'})],
                             structured="json_object")
    resp = client.complete("SYSTEM", "u", schema=SCHEMA)

    body = opener.bodies[0]
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["content"].startswith("SYSTEM")
    assert json.dumps(SCHEMA) in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "u"}
    assert resp.data == {"correct": True, "reason": "y"}


def test_prose_where_json_was_required_names_the_endpoint_that_does_this():
    """An endpoint ignoring `response_format` returns prose, and the operator
    needs the fix in the error rather than in a support thread."""
    client, _ = _client([_response({"content": "I am Claude, an AI assistant."})],
                        structured="json_schema")
    with pytest.raises(LLMError, match="tool_call"):
        client.complete("s", "u", schema=SCHEMA)


def test_an_unknown_structured_mode_is_refused_at_construction():
    with pytest.raises(LLMError, match="structured-output mode"):
        OpenAICompatClient("m", api_key="k", structured="magic")


def test_every_declared_structured_mode_is_actually_implemented():
    """The tuple is what the documentation and the manifest quote, so a value in
    it that the client rejects would be a documented setting that does not work."""
    for mode in OPENAI_STRUCTURED_MODES:
        assert OpenAICompatClient("m", api_key="k", structured=mode).structured == mode


# ------------------------------------------------------------------ failures
def test_a_truncated_response_is_an_error_not_a_short_extraction():
    """`finish_reason: length` on an extraction is indistinguishable from a
    conversation that held few facts, and would understate the store silently."""
    client, _ = _client([_response({"content": '{"correct": tr'}, finish="length")])
    with pytest.raises(LLMError, match="truncated"):
        client.complete("s", "u", schema=SCHEMA, max_tokens=32)


def test_a_refusal_is_reported_as_one():
    client, _ = _client([_response({"content": "", "refusal": "I cannot help with that."})])
    with pytest.raises(LLMError, match="declined"):
        client.complete("s", "u")


def test_a_rejected_credential_says_so_rather_than_retrying():
    """A 401 never clears by waiting, and burning four retries on it delays the
    one message the operator needs."""
    client, opener = _client([_http_error(401, '{"error": {"message": "bad key"}}')])
    with pytest.raises(LLMError, match="rejected the credential"):
        client.complete("s", "u")
    assert len(opener.bodies) == 1


def test_a_spent_balance_is_a_clean_resumable_stop():
    """`QuotaExhausted` is what `phase_judge` treats as stop-and-keep-everything.
    Misreading it as a generic failure records wrong answers instead."""
    body = '{"error": {"code": "insufficient_quota", "message": "exceeded your current quota"}}'
    client, _ = _client([_http_error(429, body)])
    with pytest.raises(QuotaExhausted):
        client.complete("s", "u")


def test_a_burst_rate_limit_is_retried_and_succeeds():
    client, opener = _client([
        _http_error(429, '{"error": {"code": "rate_limit_exceeded"}}', {"Retry-After": "0"}),
        _response({"content": "second time"}),
    ])
    assert client.complete("s", "u").text == "second time"
    assert len(opener.bodies) == 2


def test_a_server_error_is_retried():
    client, opener = _client([_http_error(503, "upstream unavailable"),
                              _response({"content": "recovered"})])
    assert client.complete("s", "u").text == "recovered"
    assert len(opener.bodies) == 2


def test_the_output_cap_field_switches_once_and_is_remembered():
    """OpenAI's newer models refuse `max_tokens` and name the replacement. The
    switch must not consume a retry, and the second call must not repeat it."""
    refusal = ('{"error": {"message": "Unsupported parameter: \'max_tokens\' is not '
               'supported with this model. Use \'max_completion_tokens\' instead."}}')
    client, opener = _client([_http_error(400, refusal),
                              _response({"content": "ok"}),
                              _response({"content": "ok again"})],
                             max_retries=2)

    assert client.complete("s", "u").text == "ok"
    assert "max_tokens" in opener.bodies[0]
    assert "max_completion_tokens" in opener.bodies[1]

    client.complete("s", "u")
    assert "max_completion_tokens" in opener.bodies[2]
    assert "max_tokens" not in opener.bodies[2]


def test_a_400_that_is_not_about_the_output_cap_is_reported():
    client, opener = _client([_http_error(400, '{"error": {"message": "unknown model"}}')])
    with pytest.raises(LLMError, match="HTTP 400"):
        client.complete("s", "u")
    assert len(opener.bodies) == 1


def test_hard_quota_classification_does_not_swallow_a_plain_rate_limit():
    assert _looks_like_hard_quota('{"code": "insufficient_quota"}')
    assert _looks_like_hard_quota("requests per day limit reached")
    assert not _looks_like_hard_quota('{"code": "rate_limit_exceeded"}')
    assert not _looks_like_hard_quota("")


def test_retry_after_is_honoured_bounded_and_falls_back():
    assert _retry_after({"Retry-After": "12"}, 2.0) == 12.0
    assert _retry_after({"Retry-After": "99999"}, 2.0) == 300.0
    assert _retry_after({"Retry-After": "soon"}, 2.0) == 2.0
    assert _retry_after({}, 2.0) == 2.0
    assert _retry_after(None, 2.0) == 2.0


def test_a_missing_key_is_refused_with_the_variable_name_in_the_message():
    with pytest.raises(LLMError, match="ENGRAPHY_BENCH_OPENAI_API_KEY"):
        OpenAICompatClient("m", api_key="")


# ------------------------------------------------------------- manifest facts
def test_the_served_model_id_is_recorded_not_the_requested_one():
    """The pinning guarantee: an endpoint that silently substitutes a cheaper
    model must be visible in the manifest, not hidden behind the id we asked for."""
    client, _ = _client([_response({"content": "ok"}, model="claude-sonnet-5-20260101")])
    assert client.complete("s", "u").model == "claude-sonnet-5-20260101"


def test_describe_carries_the_host_and_never_the_key():
    client, _ = _client([], base_url="https://api.anthropic.com/v1/",
                        api_key="sk-secret", structured="tool_call")
    described = client.describe()
    assert described["base_url_host"] == "api.anthropic.com"
    assert described["structured_output"] == "tool_call"
    assert "sk-secret" not in json.dumps(described)


def test_token_counting_stays_unavailable():
    """The manifest's `token_counter_note` says token figures are absent by
    construction. A client quietly returning numbers would contradict it."""
    client, _ = _client([])
    with pytest.raises(LLMError, match="does not count tokens"):
        client.count_tokens("some payload")


def test_the_pinned_role_models_cover_every_role_the_harness_runs():
    assert set(OPENAI_ROLE_MODELS) == {"extractor", "reader", "adjudicator", "judge"}
    for role in OPENAI_ROLE_MODELS:
        assert openai_model_for(role)


def test_an_overridden_model_is_marked_as_such_in_the_manifest(monkeypatch):
    """A run that swapped a model measures a different system, so the manifest
    has to show the deviation rather than just the id."""
    monkeypatch.setenv("ENGRAPHY_BENCH_JUDGE_MODEL", "some-other-judge")
    table = openai_role_manifest()
    assert table["judge"]["model"] == "some-other-judge"
    assert table["judge"]["pinned_model"] == OPENAI_ROLE_MODELS["judge"]
    assert table["judge"]["overridden_by"] == "ENGRAPHY_BENCH_JUDGE_MODEL"
    assert "overridden_by" not in table["reader"]


def test_for_role_reads_the_judge_variables_and_falls_back(monkeypatch):
    monkeypatch.setenv("ENGRAPHY_BENCH_OPENAI_BASE_URL", "https://shared.test/v1")
    monkeypatch.setenv("ENGRAPHY_BENCH_OPENAI_API_KEY", "shared-key")
    monkeypatch.delenv("ENGRAPHY_BENCH_JUDGE_BASE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHY_BENCH_JUDGE_API_KEY", raising=False)

    fallen_back = OpenAICompatClient.for_role("judge", "m")
    assert fallen_back.base_url == "https://shared.test/v1"

    monkeypatch.setenv("ENGRAPHY_BENCH_JUDGE_BASE_URL", "https://judge.test/v1")
    monkeypatch.setenv("ENGRAPHY_BENCH_JUDGE_API_KEY", "judge-key")
    split = OpenAICompatClient.for_role("judge", "m")
    assert split.base_url == "https://judge.test/v1"
    assert split.api_key == "judge-key"
    # The reader stays on the shared endpoint -- that separation is what makes
    # cross-vendor judging reachable on this route.
    assert OpenAICompatClient.for_role("reader", "m").base_url == "https://shared.test/v1"


# ---------------------------------------------------- the seam holds together
def test_the_judge_grades_through_this_client_unchanged():
    """The point of the seam: swapping the route must not change the scoring
    path. Same `Judge`, same `JUDGE_SCHEMA`, same verdict shape."""
    from bench.core.corpus import Question

    client, opener = _client(
        [_response({"content": '{"correct": true, "reason": "same fact"}'})] * 3,
        structured="json_schema",
    )
    question = Question(question_id="q1", haystack_id="conv-26", category="single-hop",
                        text="Where does Caroline live?", gold_answer="Cairo")

    verdict = Judge(client).grade_majority(question, "She lives in Cairo.")

    assert verdict.correct is True
    assert verdict.graded_by == "judge"
    # Best-of-3 is part of the published method and applies on every route.
    assert len(opener.bodies) == 3
    assert opener.bodies[0]["response_format"]["json_schema"]["schema"] == JUDGE_SCHEMA
