"""`--provider` and `--judge` threading through `bench/core/run.py`.

`test_openai_provider.py` proves the client is right and `bench/smoke_openai.py`
proves a live endpoint agrees, but both build their clients directly. Neither
would notice a dropped `provider=args.provider` on a phase call or a swapped
positional in `_build_extractor`, and both of those turn into a run that silently
took the wrong route.

So this file asserts the wiring itself: that each builder returns a client on the
requested route, and that the manifest names the models and endpoints that were
actually used. Hermetic -- nothing here calls a model.

The manifest assertions run over the whole `--provider` x `--judge` cross-product
on purpose. The combination that matters is not the obvious one: `--provider
claude-cli --judge openai` is a reasonable configuration (the maintainer's free
route for reading, a neutral paid endpoint for grading) and it is the one where a
manifest built from the wrong flag names the wrong grading vendor.
"""

from __future__ import annotations

import argparse
import os

import pytest

from bench.core import providers, run
from bench.core.extract import LLMExtractor, VerbatimExtractor
from bench.core.ingest import AlwaysDistinct, LLMAdjudicate
from bench.core.llm import ROLE_MODELS, openai_model_for
from bench.core.providers import ClaudeCLIClient, GeminiClient, OpenAICompatClient
from bench.core.space import load_bench_pack

ROLES = ("extractor", "reader", "adjudicator")


@pytest.fixture(autouse=True)
def _configured(monkeypatch, tmp_path):
    """A credential the OpenAI route will accept, and no ambient configuration.

    `OpenAICompatClient` refuses to construct without a key, so a wiring test has
    to supply one; it never leaves this process because nothing here opens a
    socket.
    """
    monkeypatch.setattr(providers, "ENV_PATH", tmp_path / "absent.env")
    for name in list(os.environ):
        if name.startswith("ENGRAPHY_BENCH_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENGRAPHY_BENCH_OPENAI_BASE_URL", "https://reader.test/v1")
    monkeypatch.setenv("ENGRAPHY_BENCH_OPENAI_API_KEY", "reader-key")


def _stub_gemini_key(monkeypatch) -> None:
    """`GeminiClient` reads its key from the file, not the environment.

    Faithful to `read_env_file`'s contract, which RAISES on a miss rather than
    returning an empty string: a stub that returns "" instead would make every
    other setting resolve to "" too, and the first caller to parse one would see
    a crash that the real code path cannot produce.
    """
    real = providers.read_env_file

    def _read(key, path=None):
        if key == "GEMINI_API_KEY":
            return "gemini-key-for-tests"
        return real(key, path)

    monkeypatch.setattr(providers, "read_env_file", _read)


def _args(provider: str, judge: str) -> argparse.Namespace:
    return argparse.Namespace(provider=provider, judge=judge)


# ------------------------------------------------------------ role clients
@pytest.mark.parametrize("role", (*ROLES, "judge"))
def test_the_openai_route_builds_an_openai_client_on_the_pinned_model(role):
    client = run._client_for(role, "openai")
    assert isinstance(client, OpenAICompatClient)
    assert client.model == openai_model_for(role)
    assert client.role == role


@pytest.mark.parametrize("role", ROLES)
def test_the_cli_route_is_untouched(role):
    client = run._client_for(role, "claude-cli")
    assert isinstance(client, ClaudeCLIClient)
    assert client.model == ROLE_MODELS[role]["model"]


def test_the_extractor_builder_threads_the_route():
    """Positional, and easy to pass in the wrong order. A verbatim extractor
    takes no client at all, which is why both cases are asserted."""
    pack = load_bench_pack()
    extractor = run._build_extractor("llm", pack, "openai")
    assert isinstance(extractor, LLMExtractor)
    assert isinstance(extractor.client, OpenAICompatClient)
    assert extractor.client.model == openai_model_for("extractor")

    assert isinstance(run._build_extractor("verbatim", pack, "openai"), VerbatimExtractor)
    assert isinstance(run._build_extractor("llm", pack, "claude-cli").client, ClaudeCLIClient)


def test_the_confirm_policy_builder_threads_the_route():
    policy = run._build_policy("llm_adjudicate", "openai")
    assert isinstance(policy, LLMAdjudicate)
    assert isinstance(policy.client, OpenAICompatClient)
    assert policy.client.model == openai_model_for("adjudicator")

    assert isinstance(run._build_policy("always_distinct", "openai"), AlwaysDistinct)


@pytest.mark.parametrize(
    ("judge_provider", "client_type"),
    [("openai", OpenAICompatClient), ("gemini", GeminiClient), ("claude", ClaudeCLIClient)],
)
def test_the_judge_builder_honours_its_own_flag(judge_provider, client_type, monkeypatch):
    _stub_gemini_key(monkeypatch)
    judge = run._build_judge(judge_provider)
    assert isinstance(judge.client, client_type)


# --------------------------------------------------------------- manifest
@pytest.mark.parametrize("provider", ["claude-cli", "openai"])
@pytest.mark.parametrize("judge", ["claude", "gemini", "openai"])
def test_the_manifest_names_the_route_each_role_actually_took(provider, judge):
    table = run._role_models_manifest(_args(provider, judge))
    assert set(table) == {"extractor", "reader", "adjudicator", "judge"}

    expected_non_judge = "openai-compat" if provider == "openai" else "claude-cli"
    for role in ROLES:
        assert table[role]["provider"] == expected_non_judge, role

    expected_judge = {"openai": "openai-compat", "claude": "claude-cli", "gemini": "gemini"}
    assert table["judge"]["provider"] == expected_judge[judge]


@pytest.mark.parametrize("provider", ["claude-cli", "openai"])
@pytest.mark.parametrize("judge", ["claude", "gemini", "openai"])
def test_the_manifest_judge_matches_the_judge_the_run_would_build(provider, judge, monkeypatch):
    """The load-bearing assertion. `provider_config` and `role_models` are built
    by different code paths, and a manifest whose two halves disagree about who
    graded the answers is worse than one that omits both."""
    _stub_gemini_key(monkeypatch)
    recorded = run._role_models_manifest(_args(provider, judge))["judge"]
    built = run._build_judge(judge).client
    assert recorded["provider"] == built.provider
    assert recorded["model"] == built.model


def test_provider_config_covers_every_role_on_the_openai_route():
    config = run._provider_config_manifest(_args("openai", "openai"))
    assert set(config) == {"extractor", "reader", "adjudicator", "judge"}
    for entry in config.values():
        assert entry["base_url_host"] == "reader.test"
        assert "reader-key" not in str(entry)


def test_provider_config_covers_only_the_judge_when_only_the_judge_is_on_it():
    config = run._provider_config_manifest(_args("claude-cli", "openai"))
    assert set(config) == {"judge"}


def test_provider_config_is_empty_when_nothing_is_on_the_openai_route():
    assert run._provider_config_manifest(_args("claude-cli", "gemini")) == {}


def test_a_missing_credential_is_recorded_rather_than_raised(monkeypatch):
    """`--phases report` is run over finished artifacts, sometimes with no key
    present. Losing a report to a manifest field would be a poor trade."""
    monkeypatch.delenv("ENGRAPHY_BENCH_OPENAI_API_KEY")
    config = run._provider_config_manifest(_args("openai", "openai"))
    assert "error" in config["reader"]


# ------------------------------------------------------- neutrality statement
def test_a_separate_judge_endpoint_is_recorded_as_separate(monkeypatch):
    monkeypatch.setenv("ENGRAPHY_BENCH_JUDGE_BASE_URL", "https://judge.test/v1")
    monkeypatch.setenv("ENGRAPHY_BENCH_JUDGE_API_KEY", "judge-key")
    route = run._judge_route_manifest("openai")
    assert route["judge_endpoint_is_separate"] is True
    assert "own endpoint" in run._judge_banner("openai")


def test_a_shared_judge_endpoint_says_so_in_words():
    """Not left to be inferred from two matching hostnames."""
    route = run._judge_route_manifest("openai")
    assert route["judge_endpoint_is_separate"] is False
    assert "SAME endpoint" in route["judge_endpoint_note"]
    assert "SAME ENDPOINT" in run._judge_banner("openai")


def test_the_other_routes_carry_their_existing_statements():
    assert run._judge_route_manifest("claude") == {}
    assert "SAME-VENDOR" in run._judge_neutrality("claude")
    assert "Cross-vendor" in run._judge_neutrality("gemini")
    assert "cross-vendor" in run._judge_neutrality("openai").lower()


# --------------------------------------------------------------------- CLI
def test_the_flags_offer_exactly_the_routes_that_are_implemented():
    assert set(run.PROVIDERS) == {"claude-cli", "openai"}
    assert set(run.JUDGE_PROVIDERS) == {"claude", "gemini", "openai"}
    for provider in run.PROVIDERS:
        for role in ROLES:
            assert run._client_for(role, provider) is not None
