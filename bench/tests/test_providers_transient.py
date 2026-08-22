"""The transient CLI-launch retry, and the stop-class buckets.

A Claude Code self-update swaps `claude.exe` in place, so a launch can fail with
FileNotFoundError/PermissionError for a few seconds. The client must re-resolve
the binary and retry within a bounded budget (self-heal), surface a longer outage
as a distinct `TransientCLIError` (so the supervisor relaunches, not halts), and
NOT retry a launched-but-hung process. These pin exactly that.

Hermetic: subprocess.run, time.sleep and time.monotonic are all faked, so no real
CLI, no real waiting.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from bench.core import providers
from bench.core.llm import LLMError
from bench.core.providers import ClaudeCLIClient, TransientCLIError, stop_class


class _Clock:
    """A monotonic clock the test drives: sleeping advances it, so the launch
    budget is exhausted in a handful of iterations rather than real seconds."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _ok_payload() -> str:
    return json.dumps({"result": "ok", "usage": {}, "modelUsage": {}})


def _fake_completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")


def test_launch_recovers_after_a_transient_missing_binary(monkeypatch):
    """Two FileNotFoundErrors then success: the call recovers, and the binary is
    re-resolved every attempt (not pinned to a stale path)."""
    resolves = {"n": 0}
    monkeypatch.setattr(providers, "_resolve_binary",
                        lambda name: (resolves.__setitem__("n", resolves["n"] + 1), ["claude.exe"])[1])
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise FileNotFoundError("claude.exe momentarily absent")
        return _fake_completed(_ok_payload())

    monkeypatch.setattr(providers.subprocess, "run", fake_run)

    client = ClaudeCLIClient(model="sonnet")
    resp = client.complete("system", "user")
    assert resp.text == "ok"
    assert calls["n"] == 3                 # retried twice, succeeded on the third
    assert resolves["n"] >= 3              # re-resolved each attempt (+ the __init__ one)


def test_launch_exhausts_budget_and_raises_transient(monkeypatch):
    """A persistent missing binary surfaces as TransientCLIError after the
    budget, NOT as a plain LLMError -- so the supervisor relaunches, not halts."""
    clock = _Clock()
    monkeypatch.setattr(providers.time, "monotonic", clock)
    monkeypatch.setattr(providers.time, "sleep", lambda s: setattr(clock, "t", clock.t + s))
    monkeypatch.setattr(providers, "_resolve_binary", lambda name: ["claude.exe"])
    monkeypatch.setattr(providers.subprocess, "run",
                        lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("gone")))

    client = ClaudeCLIClient(model="sonnet")
    with pytest.raises(TransientCLIError):
        client.complete("system", "user")


def test_launch_retries_a_locked_binary_too(monkeypatch):
    """PermissionError (binary locked mid-write during the swap) is the same
    self-healing class as absent, so it is retried, not surfaced immediately."""
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    monkeypatch.setattr(providers, "_resolve_binary", lambda name: ["claude.exe"])
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise PermissionError("claude.exe locked")
        return _fake_completed(_ok_payload())

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    assert ClaudeCLIClient().complete("s", "u").text == "ok"
    assert calls["n"] == 2


def test_launch_does_not_retry_a_hung_process(monkeypatch):
    """A TimeoutExpired means the process DID launch and then hung -- a different
    failure. It must surface as a plain LLMError immediately, never retried as a
    launch blip (which would mask a genuinely stuck CLI)."""
    monkeypatch.setattr(providers, "_resolve_binary", lambda name: ["claude.exe"])
    monkeypatch.setattr(providers.subprocess, "run",
                        lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 300)))
    client = ClaudeCLIClient()
    with pytest.raises(LLMError) as ei:
        client.complete("s", "u")
    assert "timed out" in str(ei.value)
    assert not isinstance(ei.value, TransientCLIError)


def test_stop_class_buckets():
    """The three responses the supervisor keys on."""
    assert stop_class("8 consecutive reader errors; last: TransientCLIError: gone") == "transient"
    assert stop_class("the CLI could not be launched (claude) after 6 attempts") == "transient"
    assert stop_class("usage limit: Claude CLI usage limit reached: 429 ...") == "usage"
    assert stop_class("quota: Gemini daily quota exhausted") == "usage"
    assert stop_class("5 consecutive judge failures; last: malformed schema response") == "halt"
