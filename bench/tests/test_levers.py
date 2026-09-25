"""The matched-convention pass, the reader's verify contract, the arm width, and the
offline engine. No database writes and no model calls: every client is a fake."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from bench import offline
from bench.core import reference
from bench.core.answer import READER_CONTRACTS, Reader, _output_contract, split_reply
from bench.core.corpus import Question
from bench.core.judge import Verdict
from bench.core.llm import LLMError, LLMResponse
from bench.core.providers import QuotaExhausted
from bench.core.run import Checkpoint, parse_arm, retrieval_config
from bench.replay import group_of, transition_table

# Written by executing the upstream prompts.py at the pinned commit.
VENDORED = {
    "answer.md": "79c9f09bcc8d5e9e8b7e9786af587b02a67d366ab79285fc148b73fd20f6297b",
    "judge.md": "d248e056d993725e28fba8d16ca7081f0b59deae272ef294f3c6b00d48eac02b",
    "judge_system.md": "36c007917faf1ab84516cdca577fb523711a9b993706fbae8ae37806e6f9adcc",
}

# The contract recorded in the manifest of run fullrun-conv-20260916.
PUBLISHED_GROUNDED_CONTRACT = (
    "## Output format for this evaluation\n\nYou are being evaluated, so you cannot ask the "
    "user or issue further tool calls -- answer only from the memory already provided in "
    "this message. Answer in one concise sentence, or a bare phrase where the question asks "
    "for a name, a date, a number, or a place. No preamble, no restating the question. If "
    "the memory neither states the answer nor supports a grounded inference, reply with "
    "exactly the single word INSUFFICIENT."
)


def q(category="single-hop", gold="Paris", abstain=False, qid="conv-1:q0") -> Question:
    return Question(question_id=qid, haystack_id="conv-1", text="Where did Ada move?",
                    category=category, gold_answer=gold, abstain_expected=abstain)


class FakeClient:
    def __init__(self, text="", data=None, raises=None):
        self.text, self.data, self.raises = text, data, raises
        self.calls = []

    def complete(self, system, user, **kw):
        self.calls.append((system, user, kw))
        if self.raises:
            raise self.raises
        return LLMResponse(text=self.text, data=self.data, model="fake", seconds=0.1)


# ------------------------------------------------------------------ reference
@pytest.mark.parametrize(("name", "sha"), sorted(VENDORED.items()))
def test_vendored_prompts_are_byte_identical_to_the_pinned_upstream(name, sha):
    text = (reference.PROMPT_DIR / name).read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == sha


def test_memories_render_oldest_first_without_ranks_and_keep_attributes():
    env = {"results": [
        {"node": {"title": "Later", "body": "Second\nline", "created_at": "2026-02-02"},
         "score": 0.9},
        {"node": {"title": "Earlier", "body": "", "attrs": {"occurred_on": "2023-05"},
                  "created_at": "2026-01-01"}, "score": 0.1},
    ]}
    text = reference.render_memories(env)
    lines = text.splitlines()
    assert lines[0].startswith("The following memories are presented in chronological order")
    assert lines[2] == "(unknown date) Earlier (occurred_on: 2023-05)"
    assert lines[3] == "(unknown date) Later: Second line"
    assert "0.9" not in text and "[1]" not in text


def test_an_empty_envelope_takes_the_reference_empty_form():
    assert reference.render_memories({"results": []}) == "(No relevant memories found)"


def test_answer_is_taken_after_the_last_marker():
    assert reference.extract_answer("step 1...\nANSWER: a\nANSWER: Paris") == "Paris"
    assert reference.extract_answer("no marker") == "no marker"


def test_open_domain_gold_is_cut_at_the_first_semicolon_only_for_open_domain():
    assert reference.gold_for_judge(q("open-domain-knowledge", "Likely yes; she loves it")) \
        == "Likely yes"
    assert reference.gold_for_judge(q("single-hop", "a; b")) == "a; b"


def test_reference_date_is_the_latest_session_by_parsed_date():
    hay = SimpleNamespace(sessions=[
        SimpleNamespace(session_id="session_1", timestamp="1:56 pm on 8 May, 2023"),
        SimpleNamespace(session_id="session_2", timestamp="10:37 am on 27 June, 2023"),
        SimpleNamespace(session_id="session_3", timestamp="4:04 pm on 20 Jan, 2023"),
    ])
    assert reference.reference_date(hay) == "10:37 am on 27 June, 2023"


def test_prompt_fills_every_field_and_defaults_the_date():
    prompt = reference.answer_prompt(q(), {"results": []}, None)
    assert "{" not in prompt.replace("{{", "")
    assert "around 2023" in prompt and "Question: Where did Ada move?" in prompt


def test_reference_reader_sends_an_empty_system_and_extracts_the_answer():
    client = FakeClient(text="Step 7...\nANSWER: Paris")
    ans = reference.ReferenceReader(client).read(q(), {"results": []}, "8 May, 2023")
    assert ans.text == "Paris" and ans.raw.endswith("ANSWER: Paris")
    assert client.calls[0][0] == ""


def test_reference_reader_records_a_failure_and_lets_a_quota_stop_through():
    ans = reference.ReferenceReader(FakeClient(raises=LLMError("boom"))).read(
        q(), {"results": []}, None)
    assert ans.error and ans.text == ""
    with pytest.raises(QuotaExhausted):
        reference.ReferenceReader(FakeClient(raises=QuotaExhausted("cap"))).read(
            q(), {"results": []}, None)


@pytest.mark.parametrize(("label", "correct"), [("CORRECT", True), ("correct", True),
                                                ("WRONG", False), ("", False)])
def test_reference_judge_reads_the_label(label, correct):
    client = FakeClient(data={"reasoning": "r", "label": label})
    v = reference.ReferenceJudge(client).grade(q("open-domain-knowledge", "yes; because"), "yes")
    assert v.correct is correct and v.graded_by == "reference_judge"
    system, user, kw = client.calls[0]
    assert system == reference.JUDGE_SYSTEM_PROMPT and "Gold answer: yes\n" in user
    assert kw["schema"] == reference.JUDGE_SCHEMA


def test_reference_judge_error_is_never_a_verdict():
    v = reference.ReferenceJudge(FakeClient(raises=LLMError("x"))).grade(q(), "a")
    assert v.graded_by == "judge_error"


def test_conventions_manifest_names_source_hashes_and_differences():
    m = reference.conventions_manifest(judge_passes=1)
    assert m["source"]["commit"].startswith("4b61c5d")
    assert m["prompts"]["judge"]["sha256"] == "sha256:" + VENDORED["judge.md"]
    assert any("unknown date" in d for d in m["differences"])


# --------------------------------------------------------------------- reader
def test_direct_contract_is_the_published_contract_byte_for_byte():
    assert _output_contract("grounded", "direct") == PUBLISHED_GROUNDED_CONTRACT
    assert "CHECK:" not in _output_contract("strict", "direct")


def test_verify_contract_adds_the_check_and_answer_lines():
    text = _output_contract("grounded", "verify")
    assert text.startswith(PUBLISHED_GROUNDED_CONTRACT) and "`CHECK:`" in text
    assert set(READER_CONTRACTS) == {"verify", "direct"}


@pytest.mark.parametrize(("raw", "contract", "expected"), [
    ("CHECK: Jon; bank account; 'Jon shut down his bank account'\nANSWER: He closed it.",
     "verify", ("Jon; bank account; 'Jon shut down his bank account'", "He closed it.")),
    ("CHECK: none\nANSWER: INSUFFICIENT", "verify", ("none", "INSUFFICIENT")),
    ("**ANSWER:** INSUFFICIENT", "verify", ("", "INSUFFICIENT")),
    ("Paris", "verify", ("", "Paris")),
    ("CHECK: x\nANSWER: y", "direct", ("", "CHECK: x\nANSWER: y")),
])
def test_split_reply(raw, contract, expected):
    assert split_reply(raw, contract) == expected


def test_reader_grades_only_the_answer_and_keeps_the_check():
    from bench.core.retrieve import Retrieval
    reader = Reader(FakeClient(text="CHECK: none\nANSWER: INSUFFICIENT"), contract="verify")
    ans = reader.read(q(), Retrieval(envelope={"results": []}, strategy="search_only"))
    assert ans.text == "INSUFFICIENT" and ans.abstained and ans.as_dict()["reader_check"] == "none"


def test_reader_loads_a_skill_from_a_given_path(tmp_path):
    skill = tmp_path / "old-skill.md"
    skill.write_text("# old skill\n", encoding="utf-8")
    reader = Reader(None, contract="direct", skill_path=skill)
    assert reader.system.startswith("# old skill")
    assert reader.system_manifest["governing_skill"] == str(skill)


# ----------------------------------------------------------------------- arms
def test_default_arm_id_is_unchanged_and_width_is_recorded():
    arm = parse_arm("llm-conversational:search_only")
    assert arm.arm_id == "llm-conversational/search_only/always_distinct"
    assert retrieval_config(arm) == {"strategy": "search_only", "limit": 10, "detail": "full"}


def test_width_option_in_any_position():
    a = parse_arm("llm-conversational:search_only:k=20")
    b = parse_arm("llm:search_only:multi-hop:always_distinct:k=15")
    assert a.arm_id.endswith("/k20") and retrieval_config(a)["limit"] == 20
    assert b.categories == ("multi-hop",) and b.policy == "always_distinct" and b.k == 15


@pytest.mark.parametrize("spec", ["llm:search_only:k=0", "llm:search_only:k=x",
                                  "llm:search_only:width=5", "llm:search_then_traverse:k=20"])
def test_bad_width_options_are_refused(spec):
    with pytest.raises(SystemExit):
        parse_arm(spec)


# -------------------------------------------------------------------- offline
def test_offline_passes_checkpoint_skip_errors_resume_and_finish(tmp_path):
    ck = Checkpoint(tmp_path / "pass")
    todo = [{"arm": "a/reference-conventions", "question_id": f"c:q{i}"} for i in range(4)]
    calls = []

    def read_one(item):
        calls.append(item["question_id"])
        failed = item["question_id"] == "c:q3" and len(calls) <= 4
        return {**item, "haystack_id": "c", "category": "single-hop", "abstain_expected": False,
                "answer": "x", "error": "boom" if failed else ""}

    first = offline.read_pass(ck, todo, read_one, concurrency=2)
    assert first.startswith(offline.RESIDUAL) and "boom" in first
    assert len(ck.rows("answers.jsonl")) == 3
    assert offline.read_pass(ck, todo, read_one, concurrency=2) is None  # resume re-reads q3
    assert len(ck.rows("answers.jsonl")) == 4

    def grade_one(row):
        return Verdict(correct=row["question_id"] != "c:q0", graded_by="reference_judge")

    assert offline.judge_pass(ck, grade_one, concurrency=2) is None
    agg = offline.finish(ck, {"instrument": "test"}, "t")
    b = agg["a/reference-conventions"]["overall_excl_adversarial"]
    assert (b["n"], b["correct"]) == (4, 3)
    m = json.loads(ck.path("manifest.json").read_text(encoding="utf-8"))
    assert m["quota_stop"] is False and m["rows_answered_but_ungraded"] == 0
    assert ck.path("report.md").exists()


def test_a_pass_with_rows_left_failing_stops_for_a_relaunch(tmp_path):
    ck = Checkpoint(tmp_path / "pass")
    ck.append("answers.jsonl", {"arm": "a", "question_id": "c:q0", "answer": "x"})

    def grade_one(row):
        return Verdict(correct=False, graded_by="judge_error", error="unrecognized model")

    reason = offline.judge_pass(ck, grade_one, concurrency=1)
    assert reason.startswith(offline.RESIDUAL) and "unrecognized model" in reason
    assert offline.stop_class(reason) == "transient"
    assert offline.stop_class("5 consecutive judge failures; last: x") == "hard"


def test_offline_quota_stops_cleanly(tmp_path, capsys):
    ck = Checkpoint(tmp_path / "pass")

    def read_one(item):
        raise QuotaExhausted("cap")

    reason = offline.read_pass(ck, [{"arm": "a", "question_id": "c:q0"}], read_one,
                               concurrency=1)
    assert reason.startswith("usage limit")
    offline.stop(ck, {}, reason)
    assert "[stop] class=usage" in capsys.readouterr().out
    assert json.loads(ck.path("manifest.json").read_text(encoding="utf-8"))["quota_stop"]


# --------------------------------------------------------------------- replay
def test_replay_groups_and_transitions():
    rows = [
        {"abstain_expected": True, "answer": "INSUFFICIENT", "correct": True},
        {"abstain_expected": False, "answer": "INSUFFICIENT", "correct": False},
        {"abstain_expected": False, "answer": "Paris", "correct": True},
        {"abstain_expected": False, "answer": "Rome", "correct": False},
    ]
    assert [group_of(r) for r in rows] == ["adversarial", "declined", "correct", "wrong"]
    results = [
        {"group": "declined", "source_correct": False, "correct": True,
         "source_answer": "INSUFFICIENT", "abstained": False, "graded_by": "judge"},
        {"group": "correct", "source_correct": True, "correct": True,
         "source_answer": "Paris", "abstained": False, "graded_by": "reused"},
    ]
    t = transition_table(results)
    assert t["declined"]["wrong_to_right"] == 1 and t["declined"]["declined_after"] == 0
    assert t["correct"]["verdicts_reused"] == 1 and t["correct"]["right_to_wrong"] == 0
