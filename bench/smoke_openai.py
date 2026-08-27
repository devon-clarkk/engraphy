"""Preflight for the OpenAI-compatible route (`bench/RUN-LOCOMO.md`).

A full LoCoMo run is thousands of model calls over many hours. Discovering on
hour six that the endpoint ignores `response_format`, or that the judge model id
is not served there, is an expensive way to learn it. This script makes one real
call per role -- extractor, reader, adjudicator, judge -- against the endpoint the
run will use, with the same schemas and the same client, and says which of them
work.

    python -m bench.smoke_openai

Run it as a module from the repo root: `python bench/smoke_openai.py` puts
`bench/` on `sys.path` instead of the repo root, so the `bench.core` imports fail.

Deliberately NOT a pytest test, for the same reason `bench/smoke_live.py` is not:
it costs money, needs a credential, and must never run in CI. The hermetic half
of this coverage lives in `bench/tests/test_openai_provider.py`, which asserts the
request shape with a fake transport; this asserts that a real endpoint agrees.

Nine or ten calls, all small. Exits non-zero if any role fails.
"""

from __future__ import annotations

import argparse
import json

from bench.core.corpus import Question, Session, Turn
from bench.core.extract import ExtractWindow, LLMExtractor, NodeDraft, validate_against_pack
from bench.core.ingest import LLMAdjudicate
from bench.core.judge import JUDGE_PASSES, Judge
from bench.core.llm import LLMError, openai_model_for
from bench.core.providers import OpenAICompatClient, QuotaExhausted
from bench.core.space import load_bench_pack

ROLES = ("extractor", "reader", "adjudicator", "judge")

FAILURES: list[str] = []
NOTES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{(': ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(f"{label}: {detail}")
    return condition


def note(text: str) -> None:
    NOTES.append(text)
    print(f"  [note] {text}")


def _client(role: str) -> OpenAICompatClient:
    return OpenAICompatClient.for_role(role, openai_model_for(role))


# --------------------------------------------------------------------- roles
def probe_extractor() -> None:
    """The hardest call in the harness, and the one endpoints differ on.

    Structured output against a schema built from the live pack: nested objects,
    enums, arrays and `additionalProperties: false` throughout. An endpoint that
    handles the judge's two-field verdict can still fail here, so this is the
    check worth trusting before committing to a run.
    """
    pack = load_bench_pack()
    client = _client("extractor")
    turns = (
        Turn(speaker="Caroline", text="I adopted a greyhound last spring, her name is Pepper.",
             turn_id="D1:1"),
        Turn(speaker="Melanie", text="Lovely! I'm her sister, so I'll be dog-sitting no doubt.",
             turn_id="D1:2"),
        Turn(speaker="Caroline", text="I always book aisle seats when I fly, never the window.",
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

    if not check("structured extraction returned drafts", len(result.nodes) > 0,
                 f"{len(result.nodes)} drafts"):
        note("An endpoint that ignores response_format returns prose here. Set "
             "ENGRAPHY_BENCH_OPENAI_STRUCTURED=tool_call and run this again.")
        return
    for draft in result.nodes:
        print(f"         - {draft.node_type}: {draft.title!r} attrs={draft.attrs}")
    check("every draft uses a node type the pack declares",
          all(d.node_type in pack["node_types"] for d in result.nodes))
    bad = [(d.title, errs) for d in result.nodes if (errs := validate_against_pack(d, pack))]
    check("every draft passes the engine's attr-spec validator", not bad, str(bad))
    check("usage was reported", result.input_tokens > 0 and result.output_tokens > 0,
          f"{result.input_tokens} in / {result.output_tokens} out")


def probe_reader() -> None:
    """Prose, no schema. Both halves matter: an answer the memory supports, and a
    refusal where it does not. A reader that never abstains scores the adversarial
    category at zero."""
    client = _client("reader")
    memory = json.dumps({
        "v": 1,
        "results": [
            {"node": {"type": "event", "title": "Caroline adopted a greyhound",
                      "body": "Caroline adopted a greyhound named Pepper last spring."}},
            {"node": {"type": "preference", "title": "Prefers aisle seats",
                      "body": "Caroline always books an aisle seat when flying."}},
        ],
    })
    system = ("Answer the question using only the memory provided. If the memory does "
              "not contain the answer, reply with exactly INSUFFICIENT.")

    answered = client.complete(
        system, f"Memory:\n{memory}\n\nQuestion: What is Caroline's dog called?",
        max_tokens=500, effort="low")
    print(f"         answer: {answered.text.strip()[:160]!r}")
    check("answered from the envelope", "pepper" in answered.text.lower(),
          answered.text.strip()[:80])

    abstained = client.complete(
        system, f"Memory:\n{memory}\n\nQuestion: What is Caroline's cat called?",
        max_tokens=500, effort="low")
    print(f"         abstention: {abstained.text.strip()[:160]!r}")
    check("abstains when the memory lacks the answer", "INSUFFICIENT" in abstained.text,
          abstained.text.strip()[:80])


def probe_adjudicator() -> None:
    """The confirm-band policy. Only exercised by `--arm ...:llm_adjudicate`, so a
    failure here does not block the published arm -- but it should be reported
    rather than discovered later."""
    policy = LLMAdjudicate(_client("adjudicator"))
    pending = {
        "outcome": "needs_confirmation",
        "pending_id": "p1",
        "candidates": [{"id": "cand-1", "similarity": 0.88,
                        "title": "Prefers aisle seats",
                        "body": "Caroline always books an aisle seat when flying."}],
    }
    paraphrase = NodeDraft("d1", "preference", "Always books an aisle seat",
                           "Caroline books aisle seats on flights, never window.",
                           attrs={"strength": "hard"})
    decided = policy.decide(pending, paraphrase)
    print(f"         paraphrase -> {decided.resolution} ({decided.reason[:90]})")
    check("a true paraphrase is a decision, not a fallback", policy.fallbacks == 0,
          decided.reason[:120])

    contradiction = NodeDraft("d2", "preference", "Now prefers window seats",
                              "Caroline switched to window seats after the aisle got noisy.",
                              attrs={"strength": "soft"})
    contradicted = policy.decide(pending, contradiction)
    print(f"         contradiction -> {contradicted.resolution}")
    check("a contradiction is not merged away", contradicted.resolution == "distinct",
          contradicted.reason[:120])


def probe_judge() -> None:
    """The real `Judge`, on the real prompt, through the real best-of-3 path.

    Not a hand-rolled verdict call: grading is the part of the method a reviewer
    is most entitled to distrust, so the preflight exercises exactly the code the
    run scores with.
    """
    judge = Judge(_client("judge"))
    question = Question(question_id="smoke-1", haystack_id="smoke", category="single-hop",
                        text="What is Caroline's dog called?", gold_answer="Pepper")

    right = judge.grade_majority(question, "Her dog is named Pepper.")
    print(f"         matching answer -> correct={right.correct} ({right.reason[:90]})")
    check("graded a matching answer correct", right.correct is True, right.error or "")
    check("the verdict came from the judge, not an error path",
          right.graded_by == "judge", right.graded_by)

    wrong = judge.grade_majority(question, "Her dog is named Rex.")
    print(f"         wrong answer -> correct={wrong.correct}")
    check("graded a wrong answer incorrect", wrong.correct is False, wrong.error or "")
    check(f"best-of-{JUDGE_PASSES} ran every pass",
          judge.calls == 2 * JUDGE_PASSES, f"{judge.calls} calls for 2 questions")


PROBES = {
    "extractor": probe_extractor,
    "reader": probe_reader,
    "adjudicator": probe_adjudicator,
    "judge": probe_judge,
}


def main() -> int:
    ap = argparse.ArgumentParser(prog="bench.smoke_openai")
    ap.add_argument("--roles", default=",".join(ROLES),
                    help="comma-separated subset of " + ", ".join(ROLES))
    args = ap.parse_args()
    wanted = [r.strip() for r in args.roles.split(",") if r.strip()]
    unknown = [r for r in wanted if r not in PROBES]
    if unknown:
        raise SystemExit(f"--roles: unknown role(s) {unknown}; have {list(PROBES)}")

    # Configuration first, and with no credential in it. An operator who mistyped
    # a base URL should see that before any call is billed.
    print("Resolved configuration (no credentials shown)")
    for role in ROLES:
        try:
            print(f"  {role:12s} {json.dumps(_client(role).describe())}")
        except LLMError as exc:
            print(f"  {role:12s} UNCONFIGURED: {exc}")
            FAILURES.append(f"{role} is unconfigured: {exc}")
    if FAILURES:
        print("\nNothing was called. Set the variables named above and run this again.")
        return 1

    for role in wanted:
        print(f"\n{role}")
        try:
            PROBES[role]()
        except QuotaExhausted as exc:
            check(f"{role} completed", False, f"quota exhausted: {exc}")
            note("A quota stop is clean and resumable in a real run; here it means "
                 "the preflight could not finish.")
        except LLMError as exc:
            check(f"{role} completed", False, str(exc)[:300])

    print("\n" + "=" * 62)
    if NOTES:
        print("Worth knowing:")
        for text in NOTES:
            print(f"  - {text}")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for text in FAILURES:
            print(f"  - {text}")
        print("\nFix these before starting a run. bench/RUN-LOCOMO.md has the "
              "endpoint table.")
        return 1
    print("\nEvery role works on this endpoint. The run in bench/RUN-LOCOMO.md "
          "will go through.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
