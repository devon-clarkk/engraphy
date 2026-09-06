"""The only Anthropic client in the repo (design/09 §Module layout).

Every LLM role in the harness -- extractor, confirm-band adjudicator, reader,
judge -- goes through this module, for three reasons:

1. **The engine stays clean.** IMPLEMENTER.md rule 4 forbids LLM calls in the
   engine; concentrating them in one file under `bench/` makes the boundary
   auditable and keeps `anthropic` out of the engine's dependency closure.
2. **Tests stay hermetic.** `StubLLM` satisfies the same protocol, so every
   extraction, adjudication and scoring test runs with no network and no key.
3. **The manifest can describe what ran.** Model id, effort, prompt hashes and
   token usage all come from here, so a published number can state exactly which
   configuration produced it.

The `anthropic` import is deliberately **lazy** (inside the constructor, not at
module scope): `bench/tests` is collected in the same pytest run as the engine's
suite, and an unconditional import would make the whole suite require the
`bench` extra to be installed.

## Two API facts this module is built around

**There is no `temperature`.** `temperature`, `top_p` and `top_k` were removed
on Opus 4.8/4.7 and return HTTP 400. The usual benchmark-harness reflex --
"pin temperature to 0 for reproducibility" -- is not available, and code that
sets it does not degrade gracefully, it fails. Determinism is therefore not
assumed anywhere in this harness: judge stability is *measured* by a calibration
run and reported alongside accuracy (design/09 §Answer extraction and judging).
Structured output via `output_config.format` does most of the work that
temperature pinning used to, by removing format variance from the sample space.

**Token counts come from the API, not tiktoken.** tiktoken is OpenAI's
tokenizer and undercounts Claude tokens substantially -- worse on code and
non-English text. Since "tokens returned into the agent's context" is a headline
metric, an estimator with a systematic bias would quietly flatter or penalize
the result. `count_tokens` is the only counter used, and its model id is
recorded in the manifest.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "DEFAULT_MODEL",
    "OPENAI_ROLE_MODELS",
    "OPENAI_ROLE_MODEL_ENV",
    "PROMPTS_DIR",
    "ROLE_MODELS",
    "AnthropicClient",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "StubLLM",
    "load_prompt",
    "openai_model_for",
    "openai_role_manifest",
    "prompt_hash",
]

# design/09 §Neutrality: model ids are recorded in the run manifest. Opus 4.8 is
# the default for every role -- the harness must not quietly downgrade a role to
# a cheaper model, because extraction quality and judge quality both move the
# headline number, and a reader comparing against a competitor's published
# figure is entitled to know they were not produced by a weaker model.
DEFAULT_MODEL = "claude-opus-4-8"

# Which model performs which role, and over which provider. The manifest records
# this verbatim, and `test_every_role_has_a_recorded_model_configuration` asserts
# every role is covered.
#
# Engraphy has no API funding, so both routes are free: Claude through the CLI on
# the operator's subscription, and Gemini's free tier. The split is not purely
# economic -- **the judge is deliberately a different vendor from the system
# under test**, which removes the "graded its own homework" objection that
# same-vendor judging always invites (design/09 §Neutrality).
#
# The extractor gets the strongest available model because it decides what
# enters memory at all; a weak extractor caps every downstream number. Devon's
# call (2026-07-22): extractor and reader on Opus, since extraction quality sets
# Engraphy's ceiling; the judge on Sonnet (recorded on the run's Claude route, see
# run.CLAUDE_JUDGE_MODEL), because binary answer-matching does not need Opus and
# a lighter judge conserves plan headroom.
#
# **Explicit model ids, never the CLI aliases.** Verified live: `--model opus`
# resolved to a Haiku id (the CLI runs a second model for internal work, and
# `_resolved_model` reports whichever emitted the most output tokens -- for a
# short reply that is sometimes the internal one). `--model claude-opus-4-8`
# resolves to `claude-opus-4-8` exactly. Pinning by full id is what makes the
# manifest's recorded model the model that actually ran.
ROLE_MODELS: dict[str, dict[str, str]] = {
    "extractor":   {"provider": "claude-cli", "model": "claude-opus-4-8"},
    "reader":      {"provider": "claude-cli", "model": "claude-opus-4-8"},
    "adjudicator": {"provider": "claude-cli", "model": "claude-opus-4-8"},
    # `gemini-flash-latest` was the pin until 2026-07-22, when a real run proved
    # its free-tier allowance is **20 requests per day**, not the ~1,500 the docs
    # imply -- the daily cap is per project PER MODEL, and the flagship Flash
    # alias gets almost none of it. `gemini-flash-lite-latest` is the free-tier
    # model with a usable allowance. Judging is a binary comparison against a
    # gold answer, which is the kind of task a Lite model does well; judge
    # instability is measured on every run rather than assumed either way.
    "judge":       {"provider": "gemini",     "model": "gemini-flash-lite-latest"},
}

# --------------------------------------------------- the reproducible route
# What each role runs when the whole harness routes through an OpenAI-compatible
# endpoint (`bench.core.run --provider openai`). `bench/RUN-LOCOMO.md` is the
# walkthrough; this is the pin.
#
# **These ids are the ones the published LoCoMo figure was produced with**, and
# they are pinned here rather than only in the documentation so that a run which
# quietly used something else is visible in the manifest as a difference from
# this table. The extractor and reader are Opus because extraction quality sets
# the ceiling on every downstream number; the judge is Sonnet because binary
# answer-matching against a supplied gold answer does not need Opus (Devon,
# 2026-07-22). Best-of-3 majority grading is part of the same pin and lives in
# `judge.JUDGE_PASSES`.
#
# **The model ids are the pin, the endpoint is not.** These are Claude ids and
# several endpoints serve them: Anthropic's own OpenAI-compatibility layer, and
# gateways such as OpenRouter or LiteLLM. Reproducing the *configuration* means
# reaching these ids; the base URL is whichever route the operator has.
#
# **Changing any of them changes the number.** A run on different reader and
# judge models measures a different system, and the report should not be
# compared to the published figure. Every id here is overridable so that
# comparison is possible on purpose, and each override is recorded in the
# manifest so it is never accidental:
#
#     ENGRAPHY_BENCH_EXTRACTOR_MODEL
#     ENGRAPHY_BENCH_READER_MODEL
#     ENGRAPHY_BENCH_ADJUDICATOR_MODEL
#     ENGRAPHY_BENCH_JUDGE_MODEL
OPENAI_ROLE_MODELS: dict[str, str] = {
    "extractor": "claude-opus-4-8",
    "reader": "claude-opus-4-8",
    "adjudicator": "claude-opus-4-8",
    "judge": "claude-sonnet-5",
}

OPENAI_ROLE_MODEL_ENV: dict[str, str] = {
    "extractor": "ENGRAPHY_BENCH_EXTRACTOR_MODEL",
    "reader": "ENGRAPHY_BENCH_READER_MODEL",
    "adjudicator": "ENGRAPHY_BENCH_ADJUDICATOR_MODEL",
    "judge": "ENGRAPHY_BENCH_JUDGE_MODEL",
}


def openai_model_for(role: str) -> str:
    """The model id one role runs on over the OpenAI-compatible route.

    The pinned id unless its environment variable overrides it. Resolution lives
    here, next to the pin, so there is one authority for what ran and the
    manifest can record both the pin and the deviation.
    """
    from bench.core.providers import _setting

    try:
        pinned = OPENAI_ROLE_MODELS[role]
    except KeyError:
        raise LLMError(
            f"unknown role {role!r}; the harness has {sorted(OPENAI_ROLE_MODELS)}"
        ) from None
    return _setting(OPENAI_ROLE_MODEL_ENV[role], pinned)


def openai_role_manifest() -> dict:
    """The role table for the manifest, marking every id that is not the pin.

    A reader of a published run must be able to tell a reproduction of the
    pinned configuration from a run that swapped a model, without diffing this
    file against the report.
    """
    out: dict[str, dict] = {}
    for role, pinned in OPENAI_ROLE_MODELS.items():
        resolved = openai_model_for(role)
        entry = {"provider": "openai-compat", "model": resolved, "pinned_model": pinned}
        if resolved != pinned:
            entry["overridden_by"] = OPENAI_ROLE_MODEL_ENV[role]
        out[role] = entry
    return out


PROMPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "prompts"

# Roomy but bounded. Extraction over a 40-turn window can legitimately produce a
# long draft list; a truncated response (stop_reason "max_tokens") is treated as
# an error rather than silently accepted as a short extraction.
DEFAULT_MAX_TOKENS = 16000


class LLMError(RuntimeError):
    """An LLM call failed, or returned something unusable."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One completion. `data` is populated only for schema-constrained calls."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    stop_reason: str = ""
    data: dict | None = None
    seconds: float = 0.0


@runtime_checkable
class LLMClient(Protocol):
    """The seam. `StubLLM` and `AnthropicClient` both satisfy it."""

    model: str

    def complete(
        self,
        system: str,
        user: str,
        *,
        schema: dict | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = "high",
    ) -> LLMResponse: ...

    def count_tokens(self, text: str) -> int: ...


class AnthropicClient:
    """Real client. Constructed only by `run.py`; never by a test."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        max_retries: int = 4,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover -- depends on the extra
            raise LLMError(
                "the anthropic SDK is not installed. Install the harness extra: "
                "pip install -e '.[bench]'"
            ) from exc

        self._anthropic = anthropic
        # No api_key argument when unset: the SDK resolves ANTHROPIC_API_KEY, then
        # ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile. Passing
        # api_key=None explicitly would defeat the profile path.
        self._client = (
            anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
            if api_key
            else anthropic.Anthropic(max_retries=max_retries)
        )
        self.model = model
        self._token_cache: dict[str, int] = {}

    def complete(
        self,
        system: str,
        user: str,
        *,
        schema: dict | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = "high",
    ) -> LLMResponse:
        # NOTE: no temperature / top_p / top_k. They are removed on Opus 4.8 and
        # return 400 -- see the module docstring. Do not "restore determinism"
        # by adding them back.
        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": effort},
        }
        if schema is not None:
            # Structured output removes format variance from the sample space,
            # which is most of what temperature pinning used to buy us. The
            # schema is enforced server-side, so a malformed extraction is a
            # retry inside the SDK rather than a parse failure here.
            kwargs["output_config"]["format"] = {"type": "json_schema", "schema": schema}

        started = time.perf_counter()
        try:
            resp = self._client.messages.create(**kwargs)
        except Exception as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        elapsed = time.perf_counter() - started

        if resp.stop_reason == "refusal":
            raise LLMError(f"model declined the request (stop_reason=refusal, model={resp.model})")
        if resp.stop_reason == "max_tokens":
            # Never accepted silently: a truncated extraction looks exactly like
            # a conversation that contained few facts, and would understate the
            # store's contents without anything in the result to show for it.
            raise LLMError(
                f"response hit max_tokens ({max_tokens}) and is truncated; raise max_tokens "
                "rather than accepting a partial result"
            )

        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        data = None
        if schema is not None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LLMError(f"schema-constrained response was not valid JSON: {exc}") from exc

        return LLMResponse(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            model=resp.model,
            stop_reason=resp.stop_reason or "",
            data=data,
            seconds=elapsed,
        )

    def count_tokens(self, text: str) -> int:
        """Token count for the retrieval envelope -- the headline metric.

        Cached by content hash: a benchmark run counts the same reader prompt
        hundreds of times, and the count is a pure function of (model, text).
        """
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        hit = self._token_cache.get(key)
        if hit is not None:
            return hit
        try:
            resp = self._client.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": text}],
            )
        except Exception as exc:
            raise LLMError(f"count_tokens failed: {type(exc).__name__}: {exc}") from exc
        self._token_cache[key] = resp.input_tokens
        return resp.input_tokens


@dataclass
class StubLLM:
    """Scripted client for tests. Records calls; never touches the network.

    `responses` is consumed in order. A `dict` is returned as structured data,
    a `str` as text, and an `Exception` is raised -- which is how error paths
    get covered without patching internals.
    """

    responses: list = field(default_factory=list)
    model: str = "stub-model"
    # Deterministic stand-in for count_tokens: whitespace-delimited words. Only
    # ever used in tests, where the property under test is the accounting logic
    # (what gets counted, and into which bucket), not the tokenizer itself.
    calls: list[dict] = field(default_factory=list)
    _i: int = 0

    def complete(
        self,
        system: str,
        user: str,
        *,
        schema: dict | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = "high",
    ) -> LLMResponse:
        self.calls.append(
            {"system": system, "user": user, "schema": schema, "effort": effort}
        )
        if self._i >= len(self.responses):
            raise LLMError(f"StubLLM exhausted after {len(self.responses)} responses")
        item = self.responses[self._i]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return LLMResponse(
                text=json.dumps(item),
                data=item,
                input_tokens=self.count_tokens(system + user),
                output_tokens=self.count_tokens(json.dumps(item)),
                model=self.model,
                stop_reason="end_turn",
            )
        return LLMResponse(
            text=str(item),
            input_tokens=self.count_tokens(system + user),
            output_tokens=self.count_tokens(str(item)),
            model=self.model,
            stop_reason="end_turn",
        )

    def count_tokens(self, text: str) -> int:
        return len(str(text).split())


def load_prompt(name: str) -> str:
    """Read a committed prompt file. Prompts are files, not string literals, so
    they can be hashed into the manifest and diffed between runs."""
    path = PROMPTS_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise LLMError(f"prompt file not found: {path}") from exc


def prompt_hash(name: str) -> str:
    """SHA-256 of a prompt file, for the run manifest.

    design/09 §Neutrality: a result is not publishable without the hashes of the
    prompts that produced it. A prompt edited between runs must be visible as a
    changed hash, not discoverable only by re-reading the diff.
    """
    digest = hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"
