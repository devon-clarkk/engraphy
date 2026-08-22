"""Live API smoke test for the LLM seam (design/09 §Module layout).

The whole harness's LLM layer was built against a documentation reference and
tested entirely against `StubLLM`. That proves the harness's own logic and
proves nothing about the wire. This script is the missing half: it makes one
real call per role and asserts on what comes back.

Deliberately NOT a pytest test. It costs money, needs a credential, and must
never run in CI or in a routine `pytest bench/tests` — the suite's hermeticity
is a feature. It is a script for the same reason the repo's other one-shot
scripts are (see `scripts/`): it exists to be run by a human, once, when
something needs proving.

    python -m bench.smoke_live

Run it as a module, from the repo root -- `python bench/smoke_live.py` puts
`bench/` on sys.path rather than the repo root, so the `bench.core` imports fail.

Requires the `claude` CLI (subscription auth -- NOT an API key) and, for the
judge, GEMINI_API_KEY in the repo's gitignored `.env`. Free on both routes.
Exits non-zero on any failure.
"""

from __future__ import annotations

import json

from bench.core.corpus import Session, Turn
from bench.core.extract import ExtractWindow, LLMExtractor, NodeDraft, validate_against_pack
from bench.core.ingest import LLMAdjudicate
from bench.core.llm import LLMError, ROLE_MODELS
from bench.core.providers import (
    ClaudeCLIClient,
    GeminiClient,
    QuotaExhausted,
    read_env_file,
)
from bench.core.space import load_bench_pack

FINDINGS: list[str] = []
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def note(text: str) -> None:
    """Record a difference from the reference, whether or not it broke anything."""
    FINDINGS.append(text)
    print(f"  [note] {text}")


def main() -> int:
    pack = load_bench_pack()
    # Claude through the CLI on the operator's subscription. Never the API key --
    # ClaudeCLIClient strips ANTHROPIC_API_KEY from the subprocess environment,
    # because its presence would silently bill credits instead.
    client = ClaudeCLIClient(model=ROLE_MODELS["extractor"]["model"])
    judge_client = None
    try:
        read_env_file("GEMINI_API_KEY")
        judge_client = GeminiClient(model=ROLE_MODELS["judge"]["model"])
    except LLMError as exc:
        note(f"judge (Gemini) unavailable: {exc}")

    print(f"extractor/reader: {client.provider} {client.model}")
    judge_desc = f"{judge_client.provider} {judge_client.model}" if judge_client else "UNAVAILABLE"
    print(f"judge: {judge_desc}\n")

    # ---------------------------------------------------------------- 1
    print("1. token counting")
    envelope = json.dumps(
        {"v": 1, "results": [{"node": {"title": "A test memory", "body": "Body text."}}]}
    )
    try:
        client.count_tokens(envelope)
        check("CLI refuses to fake a payload token count", False, "it returned a number")
    except LLMError:
        check("CLI refuses to fake a payload token count", True,
              "raises rather than returning an unusable number")
    if judge_client:
        n = judge_client.count_tokens(envelope)
        check("Gemini counts its own tokens", n > 0, f"{n} tokens (judge-side only)")
        note("Gemini countTokens answers for models whose generateContent 404s -- "
             "a token-count or construction check is NOT proof a model is reachable")

    # ---------------------------------------------------------------- 2
    print("\n2. extraction (structured output against the pack-derived schema)")
    turns = (
        Turn(speaker="Caroline", text="I adopted a greyhound last spring, her name is Pepper.",
             turn_id="D1:1"),
        Turn(speaker="Melanie", text="Lovely! I'm her sister, so I'll be dog-sitting no doubt.",
             turn_id="D1:2"),
        Turn(speaker="Caroline", text="I always book aisle seats when I fly — never the window.",
             turn_id="D1:3"),
    )
    window = ExtractWindow(
        haystack_id="smoke",
        session=Session("s1", turns, timestamp="8 May, 2023"),
        turns=turns,
        window_index=0,
        prior_titles=(),
    )
    result = LLMExtractor(client, pack).extract(window)
    check("produced at least one draft", len(result.nodes) > 0, f"{len(result.nodes)} drafts")
    check("reported input tokens", result.input_tokens > 0, str(result.input_tokens))
    check("reported output tokens", result.output_tokens > 0, str(result.output_tokens))

    for d in result.nodes:
        print(f"        - {d.node_type}: {d.title!r} attrs={d.attrs}")
    check(
        "every draft uses a declared node type",
        all(d.node_type in pack["node_types"] for d in result.nodes),
    )
    bad = [(d.title, errs) for d in result.nodes if (errs := validate_against_pack(d, pack))]
    check("every draft passes the engine's attr-spec validator", not bad, str(bad))
    check(
        "every draft is within the DDL title/body bounds",
        all(3 <= len(d.title) <= 200 and 1 <= len(d.body) <= 8000 for d in result.nodes),
    )
    if result.edges:
        print(f"        edges: {[(e.src_local_id.split(':')[-1], e.edge_type, e.dst_local_id.split(':')[-1]) for e in result.edges]}")
        check(
            "edge types are declared in the pack",
            all(e.edge_type in pack["edge_types"] for e in result.edges),
        )
    else:
        note("extraction returned no edges for a window with a clear person relationship")

    # ---------------------------------------------------------------- 3
    print("\n3. reading (an answer from a retrieval envelope, as the reader will do)")
    memory = json.dumps(
        {
            "v": 1,
            "results": [
                {"node": {"type": "event", "title": "Caroline adopted a greyhound",
                          "body": "Caroline adopted a greyhound named Pepper last spring."}},
                {"node": {"type": "preference", "title": "Prefers aisle seats",
                          "body": "Caroline always books an aisle seat when flying."}},
            ],
        }
    )
    read_system = (
        "Answer the question using only the memory provided. If the memory does not "
        "contain the answer, reply with exactly INSUFFICIENT."
    )
    r = client.complete(read_system, f"Memory:\n{memory}\n\nQuestion: What is Caroline's dog called?",
                        max_tokens=500, effort="low")
    print(f"        answer: {r.text.strip()[:160]!r}")
    check("answered from the envelope", "pepper" in r.text.lower(), r.text.strip()[:80])
    check("stop_reason is end_turn", r.stop_reason == "end_turn", r.stop_reason)

    r2 = client.complete(read_system, f"Memory:\n{memory}\n\nQuestion: What is Caroline's cat called?",
                         max_tokens=500, effort="low")
    print(f"        abstention: {r2.text.strip()[:160]!r}")
    check("abstains when the memory lacks the answer", "INSUFFICIENT" in r2.text, r2.text.strip()[:80])

    # ---------------------------------------------------------------- 4
    print("\n4. judging (binary verdict, structured output)")
    judge_schema = {
        "type": "object",
        "properties": {"correct": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["correct", "reason"],
        "additionalProperties": False,
    }
    judge_system = (
        "You grade an answer against a gold answer. Return correct=true only if the "
        "answer conveys the same fact as the gold answer."
    )
    if judge_client is None:
        note("judging checks skipped entirely -- no GEMINI_API_KEY in .env")
    else:
        j = judge_client.complete(
            judge_system,
            'Question: What is Caroline\'s dog called?\nGold: Pepper\n'
            'Answer: "Her dog is named Pepper."',
            schema=judge_schema, max_tokens=500,
        )
        check("returned parsed structured data", isinstance(j.data, dict), str(j.data))
        check("graded the matching answer correct",
              bool(j.data and j.data.get("correct") is True), str(j.data))

        j2 = judge_client.complete(
            judge_system,
            'Question: What is Caroline\'s dog called?\nGold: Pepper\n'
            'Answer: "Her dog is named Rex."',
            schema=judge_schema, max_tokens=500,
        )
        check("graded a wrong answer incorrect",
              bool(j2.data and j2.data.get("correct") is False), str(j2.data))

    # ---------------------------------------------------------------- 5
    print("\n5. adjudication (the confirm-band policy, live)")
    policy = LLMAdjudicate(client)
    envelope_pending = {
        "outcome": "needs_confirmation",
        "pending_id": "p1",
        "candidates": [{"id": "cand-1", "similarity": 0.88,
                        "title": "Prefers aisle seats",
                        "body": "Caroline always books an aisle seat when flying."}],
    }
    same = NodeDraft("d1", "preference", "Always books an aisle seat",
                     "Caroline books aisle seats on flights, never window.",
                     attrs={"strength": "hard"})
    d = policy.decide(envelope_pending, same)
    print(f"        paraphrase -> {d.resolution} ({d.reason[:90]})")
    check("a true paraphrase is a decision, not a fallback", policy.fallbacks == 0, d.reason)

    contradiction = NodeDraft("d2", "preference", "Now prefers window seats",
                              "Caroline switched to window seats after the aisle got noisy.",
                              attrs={"strength": "soft"})
    d2 = policy.decide(envelope_pending, contradiction)
    print(f"        contradiction -> {d2.resolution} ({d2.reason[:90]})")
    check("a contradiction is not merged away", d2.resolution == "distinct", d2.reason)

    # ---------------------------------------------------------------- 6
    print("\n6. error paths")
    try:
        ClaudeCLIClient(binary="claude-does-not-exist").complete("s", "u")
        check("a missing CLI binary raises LLMError", False, "no exception raised")
    except LLMError as exc:
        check("a missing CLI binary raises LLMError", "was not found" in str(exc),
              str(exc)[:90])

    try:
        read_env_file("NO_SUCH_KEY_SHOULD_EXIST")
        check(".env miss raises and names the file+key", False, "no exception raised")
    except LLMError as exc:
        msg = str(exc)
        check(".env miss raises and names the file+key",
              ".env" in msg and "NO_SUCH_KEY_SHOULD_EXIST" in msg, msg[:90])

    # A Pro model on the free tier is refused at construction, not at request
    # time -- the free tier lost Pro in April 2026.
    try:
        GeminiClient(model="gemini-2.5-pro", api_key="unused")
        check("a non-free-tier model is refused up front", False, "constructed anyway")
    except LLMError as exc:
        check("a non-free-tier model is refused up front", "free tier" in str(exc), str(exc)[:80])

    if judge_client is not None:
        # Quota exhaustion must be a clean, resumable stop -- at 1,500/day this
        # WILL be met, and it must not crash a run mid-flight.
        probe = GeminiClient(model=ROLE_MODELS["judge"]["model"], rpd=0)
        try:
            probe.complete("s", "u")
            check("daily quota raises QuotaExhausted", False, "no exception raised")
        except QuotaExhausted as exc:
            check("daily quota raises QuotaExhausted", True, str(exc)[:70])
        except LLMError as exc:
            check("daily quota raises QuotaExhausted", False, f"wrong type: {exc}")
        check("QuotaExhausted is catchable as LLMError",
              issubclass(QuotaExhausted, LLMError))

    # ---------------------------------------------------------------- done
    print("\n" + "=" * 62)
    if FINDINGS:
        print("Differences from the reference / things worth knowing:")
        for f in FINDINGS:
            print(f"  - {f}")
    else:
        print("No differences from the documented reference.")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nAll live checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
