"""An expired CLI session is not a usage cap, and the two must not be confused.

A cap clears by waiting: the supervisor sleeps to the reset and resumes, which is
right. An expired OAuth session never clears by waiting, so the same treatment
sleeps for hours against a wall while the log says "usage limit".

Seen live 2026-09-30: the CLI returned `"result": "Failed to authenticate: OAuth
session expired and could not be refreshed"`, the client raised QuotaExhausted,
and the supervisor slept 4.5 hours at a time for over ten hours. Before the ingest
guard existed it also recorded two conversations as ingested holding nothing.

Hermetic: `subprocess.run` is faked, so no real CLI and no waiting.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from bench.core import providers
from bench.core.providers import AuthExpired, ClaudeCLIClient, QuotaExhausted, stop_class

EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"


def _completed(stdout: str, *, rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=rc, stdout=stdout,
                                       stderr=stderr)


def _client(monkeypatch, completed: subprocess.CompletedProcess) -> ClaudeCLIClient:
    monkeypatch.setattr(providers, "_resolve_binary", lambda name: ["claude.exe"])
    monkeypatch.setattr(providers.subprocess, "run", lambda cmd, **kw: completed)
    return ClaudeCLIClient(model="sonnet")


def test_an_error_payload_naming_authentication_raises_auth_expired(monkeypatch):
    """The live shape: exit 0, `is_error`, and the reason in `result`."""
    payload = json.dumps({"is_error": True, "result": EXPIRED, "usage": {}, "modelUsage": {}})
    client = _client(monkeypatch, _completed(payload))
    with pytest.raises(AuthExpired) as caught:
        client.complete("system", "user")
    assert "claude login" in str(caught.value)


def test_a_silent_nonzero_exit_whose_output_names_authentication_is_not_a_cap(monkeypatch):
    """Exit non-zero with no stderr used to be read as a cap by default. With the
    reason in stdout it is an auth failure, and saying so is what lets an operator
    act instead of waiting."""
    payload = json.dumps({"is_error": True, "result": EXPIRED})
    client = _client(monkeypatch, _completed(payload, rc=1))
    with pytest.raises(AuthExpired):
        client.complete("system", "user")


def test_a_real_cap_is_still_a_cap(monkeypatch):
    """The resumable path must keep working: a usage message stays QuotaExhausted,
    and QuotaExhausted is not an AuthExpired."""
    payload = json.dumps({"is_error": True,
                          "result": "Claude usage limit reached; resets 5:50pm"})
    client = _client(monkeypatch, _completed(payload))
    with pytest.raises(QuotaExhausted) as caught:
        client.complete("system", "user")
    assert not isinstance(caught.value, AuthExpired)


def test_a_silent_nonzero_exit_with_no_reason_is_still_treated_as_a_cap(monkeypatch):
    """Unchanged behaviour where there is nothing to go on: stopping cleanly and
    resuming is the safe reading, because a cap misread as a bug halts a run that
    could have continued."""
    client = _client(monkeypatch, _completed(json.dumps({"result": ""}), rc=1))
    with pytest.raises(QuotaExhausted):
        client.complete("system", "user")


@pytest.mark.parametrize("reason", [
    "the Claude CLI has no usable credentials: Failed to authenticate",
    "AuthExpired: oauth session expired",
    "the Claude CLI has no usable credentials: please run /login",
])
def test_the_supervisor_halts_on_an_auth_stop(reason):
    assert stop_class(reason) == "halt"


def test_the_supervisor_still_sleeps_on_a_usage_stop():
    assert stop_class("usage limit reached; resets 5:50pm") == "usage"


def test_the_stop_reason_is_the_cli_sentence_not_the_json_envelope(monkeypatch):
    """On a non-zero exit the whole JSON document lands on stdout. The operator
    needs the sentence, so the message quotes `result` and not the envelope."""
    payload = json.dumps({"is_error": True, "result": EXPIRED, "usage": {"input_tokens": 0},
                          "modelUsage": {}, "session_id": "abc"})
    client = _client(monkeypatch, _completed(payload, rc=1))
    with pytest.raises(AuthExpired) as caught:
        client.complete("system", "user")
    message = str(caught.value)
    assert EXPIRED in message
    assert "session_id" not in message and "input_tokens" not in message
