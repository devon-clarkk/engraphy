"""The orchestrator and the run manifest (design/09 §Data flow, §Neutrality).

    python -m bench.core.run --dataset datasets/locomo10.json \
        --haystacks conv-26,conv-30,conv-49 \
        --arm verbatim:search_only --arm llm:search_only \
        --arm llm:search_then_traverse:multi-hop

One run owns one space (`bench-<run_id>`), and one *arm* is a fixed
(extractor, strategy, confirm-policy) triple. A strategy is never selected per
question or per category -- an arm that restricts itself to a category restricts
which questions it *asks*, not how it answers them, and the restriction is
recorded in the manifest where an auditor can see it.

## Four phases, because the phases fail differently

`ingest` -> `answer` -> `judge` -> `report`, each resumable, each skipping work
already recorded on disk. The split is not tidiness; it is what makes a
free-tier run survivable:

* **Ingest is the expensive, un-subsettable phase.** A question is unanswerable
  until its whole conversation is in the store, so ingest cost scales with
  conversations, not with questions. It is also the only phase whose output
  lives in Postgres rather than in a file, so its completion markers are written
  to `ingest.jsonl` and a haystack whose marker is missing has its scope
  **cleared before re-ingest** -- resuming into a half-written scope would
  double-write every draft that had already landed.
* **Answering runs on Claude (subscription, no quota) and judging runs on Gemini
  (1,500/day, and this run will approach it).** They are separate passes over
  separate files for exactly that reason: quota exhaustion fires *after* all the
  expensive Claude work, and if answers were only persisted alongside their
  verdicts, a stop at question 300 would throw away 300 reader calls that had
  already succeeded. Answers are checkpointed the moment each one returns.

`QuotaExhausted` is therefore a clean, resumable stop, not a crash: the run
prints what it completed and exits 0 with every artifact intact, and the same
command tomorrow picks up where it left off. The space is **never torn down on
an incomplete run** -- only an explicit `--teardown` drops it.

## Arm isolation

Each (haystack, extractor) pair gets its **own scope**: `hs-conv-26-verbatim` is
a different scope from `hs-conv-26-llm`. Without that the LLM extractor's drafts
would dedup against the verbatim extractor's notes and the two arms would
contaminate each other -- the same class of cross-contamination that
`assert_no_ambient_scopes` exists to prevent, arriving through a different door.
The scope suffix is derived through the same `scope_id_for` slugging as every
other scope, so its collision checking applies unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, UTC

import psycopg
from psycopg_pool import AsyncConnectionPool

from engraphy.core import embedding

from bench.adapters.locomo import LoCoMoLoader
from bench.core.answer import READER_STANCES, Reader, build_reader_system
from bench.core.diagnostics import compact_context, context_text, gold_support
from bench.core.corpus import Corpus, Question, dataset_digest
from bench.core.extract import LLM_RETAIN_SOURCE_TEXT as _LLM_RETAIN_SOURCE_TEXT
from bench.core.extract import LLMExtractor, VerbatimExtractor
from bench.core.ingest import AlwaysDistinct, LLMAdjudicate, ingest_haystack
from bench.core.judge import Judge, self_consistency
from bench.core.llm import ROLE_MODELS, prompt_hash
from bench.core.meter import Meter
from bench.core.providers import (
    ClaudeCLIClient, GeminiClient, QuotaExhausted, TransientRunStop, stop_class)
from bench.core.report import aggregate, load_rows, render_failures, render_report
from bench.core.retrieve import STRATEGIES, probe_search
from bench.core.score import LLMJudgeScorer
from bench.core.space import (
    PACK_FILES,
    RunSpace,
    _apply_space_config,
    load_bench_pack,
    load_named_pack,
    pack_file_hash,
    provision_run_space,
    scope_id_for,
    space_id_for,
)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # A Windows console defaults to cp1252, and a run that dies on the first
    # non-ASCII character in a progress line has lost hours of Claude calls to a
    # print statement. Progress output is not worth a crash.
    for _stream in (sys.stdout, sys.stderr):
        try:  # noqa: SIM105 -- the except clause carries the coverage pragma
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover -- redirected stream
            pass

REPO = pathlib.Path(__file__).resolve().parents[2]
RUNS = REPO / "runs"


def _load_dotenv_defaults() -> None:
    """Seed os.environ from the repo's gitignored `.env`, without overriding
    anything already set in the shell.

    The run's DB URL (ENGRAPHY_TEST_DATABASE_URL) used to live only in the shell
    that launched it -- so when that shell died, a resume defaulted to port
    5432, which on this machine is a *different* Postgres (native PG17, Devon's
    own) rather than the engraphy-pg container on 5433 that holds the ingested
    stores. Resuming there would have written garbage against an empty DB. The
    env var now persists in `.env`, and both the run and any supervisor that
    imports the run pick it up here at import time. Shell precedence is kept
    (existing values win) so an operator can still override for a one-off.
    """
    env_path = REPO / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value and name not in os.environ:
            os.environ[name] = value


_load_dotenv_defaults()

DB = os.environ.get(
    "ENGRAPHY_TEST_DATABASE_URL",
    "postgres://postgres:engraphy@localhost:5432/engraphy_dev?sslmode=disable",
)
APP_DB = DB.replace("postgres:engraphy@", "engraphy_app:engraphy_app_test_only@")

LOADERS = {"locomo": LoCoMoLoader}
EXTRACTORS = ("verbatim", "llm")
POLICIES = ("always_distinct", "llm_adjudicate")
JUDGE_PROVIDERS = ("gemini", "claude")

# The judge model when it routes through Claude. Pinned to Sonnet, deliberately
# a lighter model than the Opus extractor/reader (Devon, 2026-07-22): binary
# answer-matching against a supplied gold answer does not need Opus, and a
# lighter judge conserves Max-plan headroom on the run's longest sequential
# pass. Decoupled from ROLE_MODELS["reader"] on purpose -- the reader is Opus and
# the judge must not follow it. Explicit id, not the `sonnet` alias, for the
# same pinning reason as every other role. ROLE_MODELS["judge"] (Gemini) is left
# untouched so the cross-vendor default stays the documented posture.
CLAUDE_JUDGE_MODEL = "claude-sonnet-5"

# design/09 §Neutrality: a same-vendor judge is a deviation from the recommended
# posture, and the manifest must SAY so rather than let a reader discover it from
# a model id. Verbatim in the manifest as `judge_neutrality`.
CLAUDE_JUDGE_NEUTRALITY = (
    "THIS RUN'S JUDGE IS SAME-VENDOR (Claude), a deliberate deviation from "
    "design/09's cross-vendor rule. The rationale for that rule -- 'a harness "
    "that grades its own vendor's answers invites the marked-its-own-homework "
    "objection' -- still stands as the recommended posture for any eventual "
    "PUBLISHED number, and the Gemini judge path is retained for exactly that. "
    "It is set aside here for two reasons: (1) the task is binary answer-matching "
    "against a supplied gold answer, which is low bias-risk for same-vendor "
    "grading -- the judge is not scoring prose quality, only whether two short "
    "facts agree; and (2) the Gemini free tier's per-model daily cap (measured at "
    "500/day for flash-lite, 20/day for flash) makes a complete same-day run "
    "impossible, and this is an internal baseline, not a publishable figure. The "
    "judge still sees only question, gold and candidate -- never the envelope, "
    "strategy or extractor -- so it cannot condition on the arm it grades. Judge "
    "instability is measured on this run regardless of vendor."
)


# --------------------------------------------------------------------------- arms
@dataclass(frozen=True, slots=True)
class Arm:
    """A fixed configuration. Everything about how a question is answered is
    decided here, once, and named in the manifest.

    `pack` is a run dimension: the ontology the extractor writes against. Because
    a pack's node_types live at the space level, each pack gets its own space, so
    `pack` and `extractor` together decide which store an arm reads and writes.
    """

    extractor: str
    pack: str = "starter"
    strategy: str = "search_only"
    policy: str = "always_distinct"
    # Empty means every category. A non-empty value restricts which questions
    # this arm is *asked*, never how it answers them -- the distinction that
    # keeps `search_then_traverse` over multi-hop from being per-category
    # strategy selection (design/09 §Neutrality item 2).
    categories: tuple[str, ...] = ()

    @property
    def extractor_label(self) -> str:
        """`<extractor>-<pack>`, e.g. `llm-conversational`, `verbatim-starter`."""
        return f"{self.extractor}-{self.pack}"

    @property
    def arm_id(self) -> str:
        return f"{self.extractor_label}/{self.strategy}/{self.policy}"

    def as_dict(self) -> dict:
        return {
            "arm_id": self.arm_id,
            "extractor": self.extractor,
            "pack": self.pack,
            "strategy": self.strategy,
            "confirm_policy": self.policy,
            "categories": list(self.categories) or "all",
        }

    def wants(self, q: Question) -> bool:
        return not self.categories or q.category in self.categories


def parse_arm(spec: str) -> Arm:
    """`<extractor>[-<pack>]:strategy[:categories[:policy]]`.

    The first token folds extractor and pack together, matching the matrix
    labels: `verbatim`, `llm-starter`, `llm-conversational`. A bare extractor
    (no `-pack`) defaults to the starter pack -- and `verbatim` must, because it
    emits `note` nodes and only the starter pack declares that type.
    """
    parts = spec.split(":")
    if len(parts) < 2:
        raise SystemExit(f"--arm {spec!r}: expected <extractor>[-<pack>]:strategy[:categories[:policy]]")
    label, strategy = parts[0], parts[1]
    extractor, _, pack = label.partition("-")
    pack = pack or "starter"
    cats = tuple(c for c in (parts[2].split(",") if len(parts) > 2 and parts[2] else ()) if c)
    policy = parts[3] if len(parts) > 3 and parts[3] else "always_distinct"
    if extractor not in EXTRACTORS:
        raise SystemExit(f"--arm {spec!r}: unknown extractor {extractor!r} (have {EXTRACTORS})")
    if pack not in PACK_FILES:
        raise SystemExit(f"--arm {spec!r}: unknown pack {pack!r} (have {sorted(PACK_FILES)})")
    if strategy not in STRATEGIES:
        raise SystemExit(f"--arm {spec!r}: unknown strategy (have {sorted(STRATEGIES)})")
    if policy not in POLICIES:
        raise SystemExit(f"--arm {spec!r}: unknown confirm policy (have {POLICIES})")
    return Arm(extractor=extractor, pack=pack, strategy=strategy, policy=policy, categories=cats)


@dataclass(frozen=True, slots=True)
class ArmSpace:
    """A `RunSpace` view whose scopes are qualified by extractor.

    Satisfies the three attributes the retrieval and ingest paths actually use
    (`space_id`, `principal`, `scope_for`), so neither had to grow an "arm"
    parameter to keep the two extractors' stores apart.
    """

    space_id: str
    principal: str
    extractor: str

    def scope_for(self, haystack_id: str) -> str:
        return scope_id_for(f"{haystack_id}:{self.extractor}")


# --------------------------------------------------------------------- checkpoint
class Checkpoint:
    """Append-only JSONL artifacts, re-read on resume.

    Every write is flushed and fsync'd. A benchmark checkpoint that lives in the
    OS page cache is not a checkpoint: the failure it exists to survive is the
    process dying, and buffered lines do not survive that.
    """

    def __init__(self, directory: pathlib.Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> pathlib.Path:
        return self.dir / name

    def append(self, name: str, row: dict) -> None:
        with self.path(name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def rows(self, name: str) -> list[dict]:
        return load_rows(self.path(name))

    def keys(self, name: str, *fields: str) -> set[tuple]:
        return {tuple(r.get(f) for f in fields) for r in self.rows(name)}


# ------------------------------------------------------------------------- ingest
async def phase_ingest(pool, ck: Checkpoint, corpus: Corpus, arms: list[Arm],
                       pack_spaces: dict, principal: str) -> None:
    """Ingest each (haystack, extractor, pack) triple exactly once.

    Retrieval strategy plays no part here: `search_only` and
    `search_then_traverse` read the same store, so adding a strategy costs
    nothing in ingest. The pack IS an ingest dimension, though -- the same
    conversation extracted against the conversational ontology is a different
    store from the starter one, so the LLM extractor runs once per pack.
    """
    done = {(r["haystack_id"], r["extractor"], r.get("pack"))
            for r in ck.rows("ingest.jsonl") if r.get("done")}
    needed = sorted({(h.haystack_id, a.extractor, a.pack) for a in arms for h in corpus.haystacks})

    for haystack_id, extractor_name, pack_name in needed:
        space_id, pack_dict = pack_spaces[pack_name]
        if (haystack_id, extractor_name, pack_name) in done:
            print(f"  [skip] {haystack_id} / {extractor_name} / {pack_name} — already ingested")
            continue

        arm_space = ArmSpace(space_id, principal, extractor_name)
        scope = arm_space.scope_for(haystack_id)
        # A scope with rows but no completion marker is a half-finished ingest
        # from an interrupted run. Re-ingesting into it would double-write every
        # draft that already landed -- and duplicate rows in the store would
        # then read as a dedup failure rather than as a resume bug.
        cleared = _clear_scope(space_id, scope)
        if cleared:
            print(f"  [reset] {scope} — dropped {cleared} rows from an incomplete ingest")

        extractor = _build_extractor(extractor_name, pack_dict)
        policy = _build_policy(next(a.policy for a in arms
                                    if a.extractor == extractor_name and a.pack == pack_name))
        haystack = next(h for h in corpus.haystacks if h.haystack_id == haystack_id)
        print(f"  [ingest] {haystack_id} / {extractor_name} / {pack_name} → {scope} "
              f"({len(haystack.sessions)} sessions, {haystack.turn_count} turns)", flush=True)

        stats = await ingest_haystack(pool, arm_space, haystack, extractor, confirm_policy=policy)
        row = stats.as_dict()
        row.update({"extractor": extractor_name, "pack": pack_name, "scope_id": scope,
                    "space_id": space_id, "done": True,
                    "sessions": len(haystack.sessions), "turns": haystack.turn_count})
        ck.append("ingest.jsonl", row)
        print(f"           {row['drafts']} drafts → {row['nodes_written']} nodes, "
              f"confirm-band {row['confirm_band_rate']:.1%}, "
              f"edges {row['edges_attached']}/{row['edges_requested']}, "
              f"{row['total_seconds']:.0f}s", flush=True)


def _build_extractor(name: str, pack: dict):
    if name == "verbatim":
        return VerbatimExtractor()
    return LLMExtractor(ClaudeCLIClient(model=ROLE_MODELS["extractor"]["model"]), pack)


def _build_policy(name: str):
    if name == "llm_adjudicate":
        return LLMAdjudicate(ClaudeCLIClient(model=ROLE_MODELS["adjudicator"]["model"]))
    return AlwaysDistinct()


def _build_judge(provider: str) -> Judge:
    """The judge, on the requested provider.

    Both satisfy the same `LLMClient` seam, so the Judge class is unchanged. A
    Claude usage cap surfaces as `QuotaExhausted` (providers.py), which
    `phase_judge` already treats as a clean resumable stop -- so the Max plan's
    headroom becomes the binding constraint in exactly the shape the Gemini
    daily cap had.
    """
    if provider == "claude":
        return Judge(ClaudeCLIClient(model=CLAUDE_JUDGE_MODEL))
    return Judge(GeminiClient(model=ROLE_MODELS["judge"]["model"]))


def _clear_scope(space_id: str, scope_id: str) -> int:
    """Delete every row a previous partial ingest left in one scope.

    Neither `pending_writes` nor `dedup_log` carries a `scope_id` column --
    the pending payload holds it as jsonb, and a dedup_log row is addressed
    through its node. Both are cleared through those handles rather than
    left behind: a stale pending write would be resolved into the store on a
    later `resolve_duplicate`, and a stale dedup_log row would misattribute a
    band decision to the re-ingest.
    """
    scope = json.dumps({"scope_id": scope_id})
    with psycopg.connect(DB, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT id FROM nodes WHERE space_id = %s AND scope_id = %s",
                    (space_id, scope_id))
        node_ids = [r[0] for r in cur.fetchall()]
        cur.execute("DELETE FROM pending_writes WHERE space_id = %s AND payload @> %s::jsonb",
                    (space_id, scope))
        if not node_ids:
            return 0
        cur.execute(
            "DELETE FROM edges WHERE space_id = %s AND (src_id = ANY(%s) OR dst_id = ANY(%s))",
            (space_id, node_ids, node_ids),
        )
        cur.execute(
            "DELETE FROM dedup_log WHERE space_id = %s "
            "AND (node_id = ANY(%s) OR candidate_id = ANY(%s))",
            (space_id, node_ids, node_ids),
        )
        cur.execute("DELETE FROM nodes WHERE space_id = %s AND scope_id = %s",
                    (space_id, scope_id))
    return len(node_ids)


async def _retrieve_with_retry(strategy, pool, arm_space, q, meter, *, tries: int = 6):
    """Retrieve, retrying on a transient Postgres deadlock.

    A backstop beneath the retrieval lock: the lock stops the harness from
    issuing concurrent retrievals, but the compose stack also runs a server that
    could touch the same rows, and a deadlock is transient by definition. Each
    attempt starts from clean stage timings so a retried retrieval is not
    double-counted in the latency numbers.
    """
    from psycopg.errors import DeadlockDetected

    delay = 0.1
    for attempt in range(tries):
        meter.stages.clear()
        try:
            return await strategy.retrieve(pool, arm_space, q, meter)
        except DeadlockDetected:
            if attempt == tries - 1:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)


# ------------------------------------------------------------------------- answer
async def phase_answer(pool, ck: Checkpoint, corpus: Corpus, arms: list[Arm],
                       pack_spaces: dict, principal: str, *, concurrency: int,
                       stance: str = "grounded") -> None:
    """Retrieve and read, checkpointing each answer the moment it returns.

    Retrieval is sequential (it is database work and fast); the reader is a CLI
    subprocess per question and is the wall-clock bottleneck, so a bounded
    number run concurrently. Each result is appended as it completes rather than
    per batch: a kill between two answers costs at most the one in flight.
    """
    reader = Reader(ClaudeCLIClient(model=ROLE_MODELS["reader"]["model"]), stance=stance)
    have = ck.keys("answers.jsonl", "arm", "question_id")

    for arm in arms:
        strategy = STRATEGIES[arm.strategy]()
        space_id, _ = pack_spaces[arm.pack]
        arm_space = ArmSpace(space_id, principal, arm.extractor)
        todo = [q for q in corpus.questions
                if arm.wants(q) and (arm.arm_id, q.question_id) not in have]
        print(f"  [answer] {arm.arm_id}: {len(todo)} questions to do", flush=True)

        sem = asyncio.Semaphore(concurrency)
        # Retrieval is serialized while readers stay parallel. The reader is a
        # ~4s CLI subprocess and the real bottleneck; retrieval is a ~50ms DB
        # call. Running retrievals concurrently bought almost nothing and
        # deadlocked the engine's recall-count UPDATE (concurrent searches
        # racing on the same node rows -- a 3-way deadlock seen live). One
        # retrieval at a time removes the contention at its source; the readers,
        # which is what concurrency is actually for, still overlap.
        retrieval_lock = asyncio.Lock()
        pending: set[asyncio.Task] = set()

        # The loop-dependent values are bound at def time on purpose. Every task
        # created for an arm is drained by the `while pending` barrier at the
        # bottom of this loop before the next arm starts, so these closures never
        # actually observed a stale `arm` -- but that correctness lived in a
        # barrier forty lines away and would evaporate the moment anyone let a
        # task outlive its iteration. Binding here (ruff B023) makes the capture
        # correct by construction instead of by distant convention.
        async def run_one(q: Question, *, arm: Arm = arm, strategy=strategy,
                          arm_space: ArmSpace = arm_space,
                          sem: asyncio.Semaphore = sem,
                          retrieval_lock: asyncio.Lock = retrieval_lock,
                          ) -> tuple[dict, dict]:
            meter = Meter()
            async with retrieval_lock:
                retrieval = await _retrieve_with_retry(strategy, pool, arm_space, q, meter)
            async with sem:
                # to_thread: the reader is a blocking subprocess, and holding
                # the event loop through it would serialize retrieval too.
                answer = await asyncio.to_thread(reader.read, q, retrieval)

            # -- failure observability (diagnostics only; changes no scoring) --
            # gold_in_context is computed here (pure function of the envelope, no
            # DB). The gold_in_STORE probe is NOT done here: it is a second scope
            # search per question, and running it inside the concurrent answer
            # loop deadlocked the engine's recall-count UPDATE against the real
            # retrievals. It is deferred to the sequential `diagnose` phase, which
            # also keeps the diagnostic probe from bumping recall stats during
            # measurement.
            compact = compact_context(retrieval.envelope)
            gold_in_context = None
            if not q.abstain_expected and str(q.gold_answer).strip():
                gold_in_context = gold_support(q.gold_answer, context_text(compact))

            row = {
                "arm": arm.arm_id,
                "extractor": arm.extractor,
                "pack": arm.pack,
                "strategy": arm.strategy,
                "confirm_policy": arm.policy,
                "haystack_id": q.haystack_id,
                "category": q.category,
                "abstain_expected": q.abstain_expected,
                "scope_id": arm_space.scope_for(q.haystack_id),
                # Enough to diagnose the miss without re-running anything: the
                # question, the gold, the reader's answer (in answer.as_dict),
                # what retrieval put in front of the reader, and the two gold-
                # support heuristics that bucket the failure mode.
                "question_text": q.text,
                "gold_answer": q.gold_answer,
                "evidence": list(q.evidence),
                "context": compact,
                "gold_in_context": gold_in_context,
                **answer.as_dict(),
                "retrieval_seconds": round(meter.total_seconds, 4),
                **meter.as_dict(),
            }
            # The raw envelope goes to a gitignored sidecar, not the checkpoint:
            # the compact `context` above is what the durable record carries.
            raw = {"arm": arm.arm_id, "question_id": q.question_id,
                   "envelope": retrieval.envelope}
            return row, raw

        done_n = 0
        errored = 0
        consec_err = 0
        stop_reason = None
        stop_transient = False

        def _consume(t: asyncio.Task, *, todo: list = todo) -> None:
            """Persist a clean answer; NEVER persist a reader error (so a resume
            re-answers it rather than banking an empty answer as a wrong one)."""
            nonlocal done_n, errored, consec_err, stop_reason, stop_transient
            try:
                row, raw = t.result()
            except QuotaExhausted as exc:
                stop_reason = stop_reason or f"usage limit: {exc}"
                return
            if row.get("error"):
                errored += 1
                consec_err += 1
                if consec_err >= 8 and stop_reason is None:
                    stop_reason = f"{consec_err} consecutive reader errors; last: {row['error']}"
                    # A run of TransientCLIErrors is a self-healing CLI-launch
                    # outage, not a wrong-answer streak -- flag it so the run
                    # declares `class=transient` and the supervisor relaunches
                    # after a short backoff rather than sleeping to a usage reset.
                    stop_transient = row["error"].startswith("TransientCLIError")
                return  # not checkpointed -> resume re-answers this question
            consec_err = 0
            ck.append("answers.jsonl", row)
            ck.append("envelopes.jsonl", raw)
            done_n += 1
            if done_n % 25 == 0:
                print(f"           {done_n}/{len(todo)}", flush=True)

        for q in todo:
            if stop_reason is not None:
                break
            pending.add(asyncio.create_task(run_one(q)))
            if len(pending) >= concurrency * 2:
                finished, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for t in finished:
                    _consume(t)
        # Drain the rest as tasks (not gather) so _consume's t.result() catches a
        # QuotaExhausted uniformly rather than gather raising it.
        while pending:
            finished, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for t in finished:
                _consume(t)

        if errored:
            print(f"           {done_n} answered, {errored} reader errors NOT "
                  "checkpointed (resume re-answers them)", flush=True)
        else:
            print(f"           {done_n} answered", flush=True)
        if stop_reason is not None:
            if stop_transient:
                raise TransientRunStop(
                    f"answer phase stopped cleanly ({stop_reason}); resume "
                    "re-answers the ungraded questions once the CLI is back")
            raise QuotaExhausted(
                f"answer phase stopped cleanly ({stop_reason}); "
                "resume re-answers the ungraded questions")


# -------------------------------------------------------------------------- judge
def phase_judge(ck: Checkpoint, corpus: Corpus, judge_factory, *, concurrency: int = 1) -> bool:
    """Grade every answered row not yet graded. Returns False on a clean stop.

    Adversarial questions never reach the judge: their correct answer is a
    refusal, which `score.grade_abstention` decides without an LLM. On LoCoMo
    that is 22% of the suite and therefore 22% of the judge budget not spent
    asking a judge the wrong question. Those are graded inline first (instant),
    so the parallel section deals only with the rows that actually call a model.

    **Parallelism** (claude judge only; the gemini path forces `concurrency=1`
    because its client shares a per-minute rate-limiter that concurrency would
    corrupt). Each worker builds its OWN judge via `judge_factory` -- no shared
    mutable client state, so a concurrent call cannot read another's
    `last_model_usage`. Grading runs off the main thread; **every checkpoint
    append happens on the main thread**, so the JSONL is never written by two
    threads at once. Rows are submitted in `_judge_order` (balanced) order and
    in-flight work is bounded, so an interrupted parallel pass still leaves a
    roughly balanced graded prefix -- the same property the sequential path has.

    Two invariants preserved from the sequential version, because they are the
    ones that matter: a `judge_error` verdict is NEVER checkpointed (it carries
    correct=False; persisting it would make a rate limit permanently
    indistinguishable from a wrong answer), and five failures in a row stop the
    run rather than grading the remainder against a broken judge.
    """
    import concurrent.futures as _cf

    scorer = LLMJudgeScorer()
    by_id = {q.question_id: q for q in corpus.questions}
    have = ck.keys("verdicts.jsonl", "arm", "question_id")
    todo = _judge_order(
        [r for r in ck.rows("answers.jsonl") if (r["arm"], r["question_id"]) not in have]
    )
    abstain = [r for r in todo if r.get("abstain_expected")]
    judge_rows = [r for r in todo if not r.get("abstain_expected")]
    print(f"  [judge] {len(todo)} rows to grade ({len(abstain)} without the judge, "
          f"{len(judge_rows)} judged at concurrency {concurrency})", flush=True)

    # Abstentions: graded by rule, no model, so just do them inline.
    inline_judge = judge_factory()
    for row in abstain:
        v = scorer.grade(by_id[row["question_id"]], row.get("answer") or "", inline_judge)
        ck.append("verdicts.jsonl",
                  {"arm": row["arm"], "question_id": row["question_id"], **v.as_dict()})

    def grade_one(row):
        """Runs on a worker thread. Own judge, so no shared client state."""
        jq = judge_factory()
        try:
            # Best-of-N majority is the default scoring path (see score.py /
            # judge.grade_majority): it suppresses the judge's measured ~16%
            # per-pass instability that a single grade would bake into the run.
            v = jq.grade_majority(by_id[row["question_id"]], row.get("answer") or "")
            return ("ok", row, v)
        except QuotaExhausted as exc:
            return ("quota", row, exc)

    graded = 0
    consecutive_errors = 0
    stop_reason = None

    with _cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        pending: set = set()
        idx = 0

        def submit_next():
            nonlocal idx
            if idx < len(judge_rows) and stop_reason is None:
                pending.add(ex.submit(grade_one, judge_rows[idx]))
                idx += 1

        for _ in range(max(1, concurrency)):
            submit_next()

        while pending:
            done, pending = _cf.wait(pending, return_when=_cf.FIRST_COMPLETED)
            for fut in done:
                kind, row, payload = fut.result()
                if kind == "quota":
                    if stop_reason is None:
                        stop_reason = f"quota: {payload}"
                    continue
                verdict = payload
                if verdict.graded_by == "judge_error":
                    consecutive_errors += 1
                    if consecutive_errors >= 5 and stop_reason is None:
                        stop_reason = f"5 consecutive judge failures; last: {verdict.error}"
                    continue
                consecutive_errors = 0
                ck.append("verdicts.jsonl",
                          {"arm": row["arm"], "question_id": row["question_id"],
                           **verdict.as_dict()})
                graded += 1
                if graded % 50 == 0:
                    print(f"          {graded}/{len(judge_rows)}", flush=True)
            # Refill only while healthy; on stop we let in-flight drain and quit.
            for _ in range(len(done)):
                submit_next()

    if stop_reason is not None:
        print(f"\n  [judge] stopped cleanly ({stop_reason}).")
        print(f"  [judge] {graded} of {len(judge_rows)} judged rows done; ungraded rows "
              "are left ungraded and resume re-grades them.", flush=True)
        # The run declares its own stop class for the supervisor: a transient
        # CLI-launch outage backs off and relaunches; a usage cap sleeps to the
        # reset; a repeating judge bug halts for manual attention.
        print(f"  [stop] class={stop_class(stop_reason)}", flush=True)
        return False
    print(f"          {graded} judged, {len(abstain)} by rule", flush=True)
    return True


def _judge_order(rows: list[dict]) -> list[dict]:
    """Order grading so that **any prefix of it is a balanced sample**.

    This is not cosmetic. The judge's free tier is a hard daily cap, so a run
    will routinely be interrupted partway through grading, and the shape of what
    survives is decided entirely by this order. Grading in file order -- which is
    arm-major, then dataset order -- produced exactly the wrong partial result on
    the first real attempt: the verbatim arm was graded completely, the LLM arm
    got 108 rows all from a single conversation, and the multi-hop traverse arm
    got none at all. Every comparison the run exists to make was either
    unbalanced or absent, despite 606 rows being graded.

    Round-robin across (arm, category, haystack) instead. A prefix then holds
    roughly equal numbers from every arm and every category, so an interrupted
    run yields a smaller version of the intended comparison rather than a
    complete answer to a question nobody asked. Question ids are hashed for the
    within-bucket order so the sequence is deterministic across reruns.
    """
    buckets: dict[tuple, list[dict]] = {}
    for r in rows:
        buckets.setdefault((r["arm"], r["category"], r["haystack_id"]), []).append(r)
    for bucket in buckets.values():
        bucket.sort(key=lambda r: hashlib.sha256(r["question_id"].encode()).digest())

    keys = sorted(buckets)
    out: list[dict] = []
    i = 0
    while any(buckets[k] for k in keys):
        k = keys[i % len(keys)]
        if buckets[k]:
            out.append(buckets[k].pop(0))
        i += 1
    return out


def phase_calibrate(ck: Checkpoint, corpus: Corpus, judge: Judge, sample: int) -> dict:
    """Double-grade a stratified sample to measure judge instability.

    Deterministic sample (hash of the question id, not `random`) so "the
    calibration set" means the same items on a rerun. Costs two judge calls per
    item, taken from the same daily budget as the run itself -- which is why the
    sample is small and why it runs last.
    """
    if sample <= 0:
        return {}
    by_id = {q.question_id: q for q in corpus.questions}
    rows = [r for r in ck.rows("answers.jsonl") if not r.get("abstain_expected")]
    if not rows:
        return {}
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    for cat_rows in by_cat.values():
        cat_rows.sort(key=lambda r: hashlib.sha256(
            f"cal:{r['arm']}:{r['question_id']}".encode()).digest())

    picked: list[dict] = []
    cats = sorted(by_cat)
    i = 0
    while len(picked) < sample and any(by_cat[c] for c in cats):
        cat = cats[i % len(cats)]
        if by_cat[cat]:
            picked.append(by_cat[cat].pop(0))
        i += 1

    # Emit the same items for a human to grade blind. design/09's calibration has
    # two halves: self-consistency (measured here) and agreement with human
    # labels (which needs an actual person). The harness cannot manufacture the
    # human half, but it can hand over exactly the sample to label, with the
    # judge's own verdict withheld so the labeller is not anchored.
    for r in picked:
        q = by_id[r["question_id"]]
        ck.append("calibration_to_label.jsonl", {
            "question_id": r["question_id"], "arm": r["arm"], "category": r["category"],
            "question": q.text, "gold_answer": q.gold_answer,
            "candidate_answer": r.get("answer") or "",
            "human_correct": None,  # <- a person fills this in: true / false
        })
    print(f"  [calibrate] wrote {len(picked)} items to calibration_to_label.jsonl "
          "for human labelling")

    print(f"  [calibrate] double-grading {len(picked)} stratified items "
          f"({2 * len(picked)} judge calls)", flush=True)
    try:
        return self_consistency(judge, [(by_id[r["question_id"]], r.get("answer") or "")
                                        for r in picked])
    except QuotaExhausted as exc:
        print(f"  [quota] calibration incomplete: {exc}")
        return {"instability": None, "note": f"stopped by quota: {exc}"}


# ------------------------------------------------------------- cost extrapolation
# LoCoMo, whole file. The published figure of 1,540 excludes the 446 adversarial
# questions; this harness carries all five categories, so both denominators are
# stated wherever a total appears.
FULL_SUITE = {"conversations": 10, "sessions": 272, "turns": 5882, "questions": 1986,
              "adversarial": 446, "multi_hop": 282}


def _extrapolate(ck: Checkpoint, corpus: Corpus, arms: list[Arm],
                 judge_provider: str = "gemini") -> str:
    """Project the full-suite cost from this run's *measured* rates.

    Measured, not assumed: every rate below is divided out of what this run
    actually recorded, so the projection moves when the run does. It is
    deliberately linear -- ingest is per-session, answering is per-question, and
    neither has a term that grows with corpus size, so linear is the right shape
    rather than a simplification.

    The binding constraint is not time, it is the judge's 1,500/day free tier.
    That is stated as a number of days, because that is the unit the decision
    actually gets made in.
    """
    ingest = ck.rows("ingest.jsonl")
    answers = ck.rows("answers.jsonl")
    if not ingest or not answers:
        return ""

    per_ext: dict[str, dict] = {}
    for row in ingest:
        d = per_ext.setdefault(row["extractor"], {"seconds": 0.0, "sessions": 0, "turns": 0})
        d["seconds"] += row["total_seconds"]
        d["sessions"] += row.get("sessions", 0)
        d["turns"] += row.get("turns", 0)

    reader_s = [r.get("reader_seconds", 0.0) for r in answers if not r.get("error")]
    reader_mean = sum(reader_s) / len(reader_s) if reader_s else 0.0

    L: list[str] = []
    A = L.append
    A("Projected from this run's measured rates, not from an estimate. Ingest is "
      "per-session and answering is per-question; neither has a term that grows "
      "with corpus size, so the projection is linear by construction rather than "
      "by simplification.")
    A("")
    A("| stage | measured here | full suite (×10 conversations) |")
    A("|---|---|---|")
    for ext, d in sorted(per_ext.items()):
        if not d["sessions"]:
            continue
        rate = d["seconds"] / d["sessions"]
        full = rate * FULL_SUITE["sessions"]
        A(f"| ingest, `{ext}` extractor | {d['seconds']/60:.0f} min for "
          f"{d['sessions']} sessions ({rate:.1f} s/session) | "
          f"**{full/3600:.1f} h** for {FULL_SUITE['sessions']} sessions |")

    n_ans = len(answers)
    concurrency_note = ""
    full_answers = 0
    for arm in arms:
        share = (FULL_SUITE["multi_hop"] if arm.categories == ("multi-hop",)
                 else FULL_SUITE["questions"])
        full_answers += share
    wall = reader_mean * full_answers / 3
    A(f"| answering ({len(arms)} arms) | {n_ans} reader calls, "
      f"{reader_mean:.1f} s each | **{full_answers} calls ≈ {wall/3600:.1f} h** "
      "at concurrency 3 |")

    # The judge is the constraint that actually decides the schedule.
    graded = len([r for r in answers if not r.get("abstain_expected")])
    full_judge = 0
    for arm in arms:
        if arm.categories == ("multi-hop",):
            full_judge += FULL_SUITE["multi_hop"]
        else:
            full_judge += FULL_SUITE["questions"] - FULL_SUITE["adversarial"]
    A(f"| judging | {graded} calls (adversarial graded without the judge) | "
      f"**{full_judge} calls** |")
    A("")
    judge_s = [r.get("judge_seconds", 0.0) for r in ck.rows("verdicts.jsonl")
               if r.get("graded_by") == "judge" and r.get("judge_seconds")]
    judge_mean = sum(judge_s) / len(judge_s) if judge_s else 0.0
    if judge_provider == "claude":
        # This run judged on Claude (Max plan). No daily cap was hit -- the whole
        # subset graded in one session -- so the constraint is plan headroom and
        # sequential wall-clock, not a per-day allowance.
        seq_h = judge_mean * full_judge / 3600
        A(f"**The judge ran on Claude (Max plan) and hit no rate limit across the "
          f"entire {graded}-call grading pass.** So unlike the Gemini route (whose "
          f"per-model daily cap of 500/day is documented below as the fallback "
          f"path), the constraint here is not a daily quota — it is plan headroom "
          f"plus sequential wall-clock. Judging is one Claude CLI spawn per "
          f"question, ~{judge_mean:.1f}s each, run sequentially: a full-suite "
          f"{full_judge}-call pass is **≈ {seq_h:.1f} h** of grading wall-clock "
          f"(and parallelises trivially — the calibration pass was cut from ~8 min "
          f"sequential to ~2 min at 4 workers). Whether that many calls stays "
          f"inside plan limits is the open question a full run would answer; this "
          f"subset did, comfortably.")
        A("")
        A("**Fallback, for a published number.** design/09's cross-vendor posture "
          "puts the judge on Gemini, whose free tier is the real ceiling: measured "
          f"at 500 requests/day for `gemini-flash-lite-latest` (and 20/day for "
          f"`gemini-flash-latest`), a {full_judge}-call full suite is "
          f"**{-(-full_judge // 500)} days** of budget. The checkpointing exists "
          "for exactly that route — designed to be stopped by quota and resumed.")
    else:
        rpd = 500
        A(f"**The binding constraint is the judge's daily quota, and it is measured "
          f"rather than quoted.** This run exhausted `gemini-flash-lite-latest` at "
          f"**{rpd} requests/day** — not the ~1,500 the documentation implies, and "
          f"not remotely the 20/day that `gemini-flash-latest` turned out to allow. "
          f"A full-suite run of these same {len(arms)} arms needs {full_judge} "
          f"judge calls plus calibration: **{-(-full_judge // rpd)} days** of "
          f"free-tier budget at {rpd}/day. That is the schedule, and it is why the "
          "checkpointing is load-bearing rather than a nicety.")
    A("")
    A("**Financial cost: nil.** Every role runs on the operator's existing "
      "subscriptions — extraction/reading/adjudication on the Claude CLI, and "
      "judging on either the Claude Max plan (this run) or the Gemini free tier "
      "(the cross-vendor default). No run of this harness in any configuration "
      f"spends API credit.{concurrency_note}")
    A("")
    A("Two caveats on the projection, both pointing the same way. The measured "
      "conversations are the smaller ones (63 of the suite's 272 sessions across "
      "3 of 10 conversations, so ~23% of ingest for 30% of the conversations), so "
      "a full run is somewhat worse than ×10 of what is shown. And reader latency "
      "here is dominated by CLI subprocess spawn, which a deployed agent would not "
      "pay — it inflates the wall-clock projection but not the cost.")
    return "\n".join(L)


# ------------------------------------------------------------------------- report
def phase_report(ck: Checkpoint, manifest: dict) -> dict:
    """Merge answers with verdicts into the committed per-question rows, then
    aggregate and render. Ungraded answers are excluded from the accuracy
    aggregate and **counted in the manifest**, so a partial run cannot look like
    a complete one with a smaller denominator."""
    verdicts = {(r["arm"], r["question_id"]): r for r in ck.rows("verdicts.jsonl")}
    answers = ck.rows("answers.jsonl")

    from bench.core.diagnostics import attribute_failure

    # gold_in_store comes from the sequential diagnose phase, keyed the same way.
    attribution = {(r["arm"], r["question_id"]): r.get("gold_in_store")
                   for r in ck.rows("attribution.jsonl")}

    rows: list[dict] = []
    ungraded = 0
    for a in answers:
        v = verdicts.get((a["arm"], a["question_id"]))
        if v is None:
            ungraded += 1
            continue
        merged = dict(a)
        merged.update({k: val for k, val in v.items() if k not in ("arm", "question_id")})
        merged["gold_in_store"] = attribution.get((a["arm"], a["question_id"]))
        # Failure attribution (heuristic; diagnostics only, no effect on scoring).
        merged["failure_mode"] = attribute_failure(
            correct=bool(merged.get("correct")),
            abstain_expected=bool(merged.get("abstain_expected")),
            gold_in_context=merged.get("gold_in_context"),
            gold_in_store=merged.get("gold_in_store"),
        )
        rows.append(merged)

    results = ck.path("results.jsonl")
    with results.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")

    agg = aggregate(rows)
    manifest["rows_graded"] = len(rows)
    manifest["rows_answered_but_ungraded"] = ungraded
    manifest["aggregate"] = agg
    manifest["failure_modes"] = _failure_mode_counts(rows)
    ck.path("manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    ck.path("report.md").write_text(
        render_report(manifest, rows, agg), encoding="utf-8")
    ck.path("failures.md").write_text(render_failures(rows), encoding="utf-8")
    return agg


def _failure_mode_counts(rows: list[dict]) -> dict:
    """Failure-mode tally per arm, for the manifest -- a quick machine-readable
    view of where each arm's misses fall before anyone opens failures.md."""
    from collections import defaultdict
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        out[r["arm"]][r.get("failure_mode", "unknown")] += 1
    return {arm: dict(sorted(modes.items())) for arm, modes in sorted(out.items())}


async def phase_diagnose(pool, ck: Checkpoint, corpus: Corpus, arms: list[Arm],
                         pack_spaces: dict, principal: str) -> None:
    """Compute `gold_in_store` for the wrong answers that need it, SEQUENTIALLY.

    Only the rows where attribution is otherwise ambiguous get a probe: an
    incorrect, non-adversarial answer whose gold text was NOT already in the
    retrieved context. For those, one scope search decides retrieval-miss (the
    fact is in the store) vs extraction-miss (it is not). Correct answers,
    adversarial questions, and reader-misses (gold already in context) need no
    probe.

    Run here rather than in the concurrent answer loop for two reasons: a second
    concurrent search per question deadlocked the engine's recall-count UPDATE
    against the real retrievals, and a diagnostic probe should not bump recall
    stats while measurement retrievals are still happening. Sequential and
    post-hoc fixes both. Checkpointed to `attribution.jsonl` so a resume skips
    what is done.
    """
    by_arm = {a.arm_id: a for a in arms}
    by_id = {q.question_id: q for q in corpus.questions}
    verdicts = {(r["arm"], r["question_id"]): r for r in ck.rows("verdicts.jsonl")}
    have = ck.keys("attribution.jsonl", "arm", "question_id")

    todo = []
    for a in ck.rows("answers.jsonl"):
        key = (a["arm"], a["question_id"])
        if key in have or key not in verdicts:
            continue
        if a.get("abstain_expected") or not str(a.get("gold_answer") or "").strip():
            continue
        if verdicts[key].get("correct"):
            continue
        if (a.get("gold_in_context") or {}).get("supported") is True:
            continue  # reader-miss: no probe needed
        todo.append(a)

    print(f"  [diagnose] {len(todo)} wrong-and-not-in-context answers to probe", flush=True)
    for i, a in enumerate(todo, 1):
        arm = by_arm.get(a["arm"])
        q = by_id.get(a["question_id"])
        space_id, _ = pack_spaces[arm.pack]
        arm_space = ArmSpace(space_id, principal, arm.extractor)
        try:
            probe = await probe_search(pool, arm_space, arm_space.scope_for(q.haystack_id),
                                       q.gold_answer, limit=10)
            gis = gold_support(q.gold_answer, context_text(compact_context(probe)))
        except Exception as exc:  # noqa: BLE001 -- diagnostics must never abort the run
            gis = {"supported": None, "error": f"{type(exc).__name__}: {exc}"}
        ck.append("attribution.jsonl",
                  {"arm": a["arm"], "question_id": a["question_id"], "gold_in_store": gis})
        if i % 50 == 0:
            print(f"           {i}/{len(todo)}", flush=True)
    print(f"           {len(todo)} probed", flush=True)


def _parse_space_config(items: list[str]) -> dict:
    """`--space-config KEY=JSON` list → dict. Value parsed as JSON, exactly like
    `engraphy-admin config set --value`. Empty list → {} (defaults run)."""
    out: dict = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--space-config must be KEY=JSON, got {item!r}")
        key, _, raw = item.partition("=")
        try:
            out[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            raise SystemExit(f"--space-config {key!r}: value must be JSON, got {raw!r}")
    return out


def provision_or_attach(run_id: str, scope_keys: list[str], pack: dict | None = None,
                        space_config: dict | None = None) -> RunSpace:
    """Create the run's space, or attach to the one a previous attempt created.

    `provision_run_space` is not idempotent -- `packs.apply` inserts `node_types`
    without an ON CONFLICT clause, so calling it twice against the same space
    dies on `node_types_pkey`. That is fine for its own purpose and fatal for
    ours: **resume is the whole point of the checkpointing**, and a run stopped
    by the judge's daily quota must be restartable with the identical command
    tomorrow. Found by killing a live run and restarting it, which is the only
    way this class of bug ever shows up -- a resume path that has never resumed
    is not a resume path.

    Fixed here rather than in `engraphy/admin/packs.py` on purpose: design/09 puts
    the harness outside the engine and forbids it from modifying engine
    behaviour to suit a benchmark. Adding ON CONFLICT to the shipped pack
    applier would change what a real `engraphy-admin pack apply` does, which is
    not a change a benchmark gets to make.
    """
    space_id = space_id_for(run_id)
    with psycopg.connect(DB, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("SELECT 1 FROM spaces WHERE id = %s", (space_id,))
        exists = cur.fetchone() is not None

    if not exists:
        with psycopg.connect(DB, autocommit=True) as c:
            return provision_run_space(c, run_id=run_id, haystack_ids=scope_keys, pack=pack,
                                       space_config=space_config)

    # Attach. The pack is already applied; only the scope set may have grown
    # (a resumed run can name an arm the first attempt did not).
    pack = pack or load_bench_pack()
    print(f"  [attach] space {space_id} already exists — reusing it "
          "(pack already applied; scopes ensured)")
    seen: dict[str, str] = {}
    for key in scope_keys:
        sid = scope_id_for(key)
        if sid in seen and seen[sid] != key:
            raise SystemExit(
                f"scope id collision: {seen[sid]!r} and {key!r} both map to {sid!r}")
        seen[sid] = key
    with psycopg.connect(DB, autocommit=True) as c:
        cur = c.cursor()
        for sid, key in seen.items():
            cur.execute(
                "INSERT INTO scopes (space_id, id, display_name, owner_principal, "
                "visibility) VALUES (%s, %s, %s, %s, 'private') ON CONFLICT DO NOTHING",
                (space_id, sid, f"Haystack {key}", "bench"),
            )
        # Re-ensure per-space config on resume (idempotent upsert), so an attach
        # to an earlier attempt's space still carries the run's --space-config.
        _apply_space_config(cur, space_id, space_config)
    return RunSpace(space_id=space_id, principal="bench", pack_name=pack["pack"],
                    pack_version=int(pack["version"]))


# ----------------------------------------------------------------------- manifest
def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO,
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _git_dirty() -> bool:
    try:
        return bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO,
                                            text=True, stderr=subprocess.DEVNULL).strip())
    except Exception:  # noqa: BLE001
        return False


def _file_hash(path: pathlib.Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}"


def _read_back_thresholds(space_id: str) -> dict:
    """The bands actually in effect, read from `config` rather than asserted.

    design/09 §Neutrality item 1 names this specifically. Asserting 0.95/0.80
    from the source would prove nothing: the engine resolves per-space config
    rows ahead of its own defaults, so a stray config row would silently move
    the bands and a hardcoded manifest entry would not notice.
    """
    from engraphy.core.dedup import BandThresholds

    defaults = BandThresholds()
    out = {
        "source": "config table, per-space; code defaults where unset",
        "code_defaults": {"t_high": defaults.t_high, "t_low": defaults.t_low,
                          "resonance.floor": 0.75},
        "config_rows": {},
    }
    try:
        with psycopg.connect(DB, autocommit=True) as c:
            cur = c.cursor()
            cur.execute(
                "SELECT key, value FROM config WHERE space_id = %s AND key = ANY(%s)",
                (space_id, ["dedup.t_high", "dedup.t_low", "resonance.floor"]),
            )
            out["config_rows"] = {k: v for k, v in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["effective"] = {
        "t_high": out["config_rows"].get("dedup.t_high", defaults.t_high),
        "t_low": out["config_rows"].get("dedup.t_low", defaults.t_low),
        "resonance.floor": out["config_rows"].get("resonance.floor", 0.75),
    }
    return out


def build_manifest(args, corpus: Corpus, arms: list[Arm], pack_meta: dict,
                   representative_space_id: str, reader_manifest: dict,
                   dataset: pathlib.Path, questions_in_file: int) -> dict:
    return {
        "run_id": args.run_id,
        "benchmark": args.benchmark,
        # Per-space config rows applied identically to every arm's space (empty on
        # a defaults run). A run with non-default governance (e.g. dedup.t_high)
        # must be distinguishable from a defaults run forever -- Phase A.
        "space_config": dict(getattr(args, "space_config_parsed", {}) or {}),
        "generated_at": datetime.now(UTC).isoformat(),
        "engine_git_sha": _git_sha(),
        "harness_git_sha": _git_sha(),
        "git_tree_dirty": _git_dirty(),
        "git_branch": _branch(),
        "transport": "in-process (engraphy.core), per design/09 §in-process decision",
        "principal": "bench",
        # One space PER PACK (node_types are space-scoped). Each pack's shipped
        # file hash is recorded so "we ran the shipped pack" is verifiable; the
        # ambient_scopes strip (required for haystack isolation, symmetric across
        # packs) is recorded beside it.
        "packs": pack_meta,
        "band_thresholds": _read_back_thresholds(representative_space_id),
        "band_thresholds_note": "read back from one representative pack space; every "
                                "pack space is provisioned identically with no override.",
        "reader": reader_manifest,
        "dataset": {
            "path": dataset.name,
            "digest": dataset_digest(dataset),
            "questions_in_file": questions_in_file,
        },
        "haystacks": [h.haystack_id for h in corpus.haystacks],
        "corpus": corpus.stats(),
        "arms": [a.as_dict() for a in arms],
        "role_models": {
            **ROLE_MODELS,
            "judge": ({"provider": "claude-cli", "model": CLAUDE_JUDGE_MODEL,
                       "same_vendor": True}
                      if args.judge == "claude" else ROLE_MODELS["judge"]),
        },
        "judge_provider": args.judge,
        "judge_neutrality": (CLAUDE_JUDGE_NEUTRALITY if args.judge == "claude"
                             else "Cross-vendor (Gemini) — the design/09 recommended "
                                  "posture; no deviation to note."),
        "resolved_models": {},
        "prompt_hashes": {
            "extract.md": prompt_hash("extract.md"),
            "judge.md": prompt_hash("judge.md"),
            "adjudicate.md": prompt_hash("adjudicate.md"),
            # read.md is retired: the reader's instruction is now the shipped
            # skill + output contract, recorded under `reader` above.
        },
        "embedding_model": embedding.MODEL_STAMP,
        "embedding_revision": embedding.MODEL_REVISION,
        "llm_extractor": {
            "retain_source_text": _LLM_RETAIN_SOURCE_TEXT,
            "note": (
                "When true, the LLM extractor appends the verbatim text of the "
                "turns each memory cites (source_turn_ids) to the node body, so a "
                "typed node stays word-searchable. A general recall property, not "
                "a single-hop trick; the source is the cited turns copied exactly, "
                "never re-summarised. design/09 single-hop finding, 2026-07-22."
            ),
        },
        "retrieval_note": (
            "search_then_traverse seeds with the SAME search(limit, detail) as the "
            "search_only baseline and walks every seed, so the two arms differ only "
            "in the graph walk -- the multi-hop comparison isolates the graph "
            "contribution rather than confounding it with search width."
        ),
        "token_counter": None,
        "token_counter_note": (
            "Absent by construction, not by oversight. Counting Claude tokens needs "
            "the count_tokens API and therefore an API key; this harness routes "
            "Claude through the CLI on a subscription so that no API credit is spent, "
            "and the CLI reports usage for its own injected system prompt rather than "
            "for the payload. Gemini's tokenizer would be a different vendor's "
            "tokenizer measuring a Claude payload -- the same objection that ruled "
            "out tiktoken. Payload is reported in bytes of the serialized envelope; "
            "byte ratios between arms are tokenizer-independent, absolute token "
            "claims are not made."
        ),
        "seeds": {"question_order": "dataset order (no sampling within a conversation)"},
        "dataset_licence": "CC BY-NC 4.0 — internal measurement only; commercial "
                           "publication is the project owner's open question",
    }


def _branch() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO,
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------- main
async def main() -> int:
    ap = argparse.ArgumentParser(prog="bench.core.run")
    ap.add_argument("--dataset", default="datasets/locomo10.json")
    ap.add_argument("--benchmark", default="locomo", choices=sorted(LOADERS))
    ap.add_argument("--haystacks", default="",
                    help="comma-separated conversation ids; empty = all (expensive)")
    ap.add_argument("--arm", action="append", default=[],
                    help="<extractor>[-<pack>]:strategy[:categories[:policy]] — repeatable")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--phases", default="ingest,answer,judge,calibrate,diagnose,report")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="concurrent reader calls; retrieval stays sequential")
    ap.add_argument("--judge-concurrency", type=int, default=4,
                    help="concurrent judge calls (claude only; gemini is forced to 1 "
                         "because its client shares a per-minute rate limiter)")
    ap.add_argument("--reader-stance", default="grounded", choices=sorted(READER_STANCES),
                    help="inference stance the answer-discipline skill is applied under "
                         "(skill §The inference stance). 'grounded' (default) allows a "
                         "declared, sourced inference; 'strict' declines when memory "
                         "lacks the answer. Recorded in the manifest.")
    ap.add_argument("--space-config", action="append", default=[], metavar="KEY=JSON",
                    help="Per-space config row applied to EVERY arm's space identically, "
                         "e.g. --space-config dedup.t_high=0.98 (value parsed as JSON). "
                         "Repeatable. Recorded in the manifest.")
    ap.add_argument("--calibration-sample", type=int, default=40)
    ap.add_argument("--judge", default="gemini", choices=sorted(JUDGE_PROVIDERS),
                    help="which provider grades. 'gemini' is cross-vendor (the "
                         "recommended posture for a published number); 'claude' is "
                         "same-vendor, escaping the Gemini per-model daily cap at a "
                         "stated neutrality cost -- see the manifest's judge_neutrality.")
    ap.add_argument("--teardown", action="store_true",
                    help="drop the run space afterwards; never implicit")
    args = ap.parse_args()

    args.run_id = args.run_id or f"lcm{uuid.uuid4().hex[:10]}"
    args.space_config_parsed = _parse_space_config(args.space_config)
    arms = [parse_arm(s) for s in (args.arm or ["llm:search_only"])]
    phases = {p.strip() for p in args.phases.split(",") if p.strip()}
    dataset = (REPO / args.dataset) if not pathlib.Path(args.dataset).is_absolute() \
        else pathlib.Path(args.dataset)

    corpus = LOADERS[args.benchmark]().load(dataset)
    # Recorded before restricting: the manifest must state the denominator the
    # whole file carries (1,986 for LoCoMo, not the 1,540 usually quoted), or a
    # comparison against a published number is meaningless.
    questions_in_file = len(corpus.questions)
    if args.haystacks:
        wanted = {h.strip() for h in args.haystacks.split(",") if h.strip()}
        missing = wanted - {h.haystack_id for h in corpus.haystacks}
        if missing:
            raise SystemExit(f"--haystacks: unknown conversation(s) {sorted(missing)}")
        corpus = Corpus(
            name=f"{corpus.name}[{','.join(sorted(wanted))}]",
            haystacks=tuple(h for h in corpus.haystacks if h.haystack_id in wanted),
            questions=tuple(q for q in corpus.questions if q.haystack_id in wanted),
            notes={**corpus.notes, "restricted_to_haystacks": sorted(wanted)},
        ).validate()

    ck = Checkpoint(RUNS / args.run_id)
    print(f"run {args.run_id} → {ck.dir}")
    print(f"  corpus: {corpus.stats()['questions']} questions over "
          f"{len(corpus.haystacks)} conversations "
          f"({corpus.stats()['sessions']} sessions, {corpus.stats()['turns']} turns)")
    for a in arms:
        print(f"  arm: {a.arm_id}" + (f" [{','.join(a.categories)}]" if a.categories else ""))

    # One space PER PACK: node_types are space-scoped, so two packs cannot share
    # a space. Each pack's space holds the (haystack, extractor) scopes of the
    # arms that use it. Provision each up front -- collision checking happens
    # there, before any write.
    pack_names = sorted({a.pack for a in arms})
    pack_spaces: dict[str, tuple[str, dict]] = {}
    pack_meta: dict[str, dict] = {}
    for pn in pack_names:
        pack_dict, ambient_stripped = load_named_pack(pn)
        scope_keys = sorted({f"{h.haystack_id}:{a.extractor}"
                             for a in arms if a.pack == pn for h in corpus.haystacks})
        rs = provision_or_attach(f"{args.run_id}-{pn}", scope_keys, pack_dict,
                                 space_config=args.space_config_parsed)
        pack_spaces[pn] = (rs.space_id, pack_dict)
        pack_meta[pn] = {
            "space_id": rs.space_id,
            "version": rs.pack_version,
            "file_hash": pack_file_hash(pn),
            "ambient_scopes_stripped": ambient_stripped,
            "node_types": sorted((pack_dict.get("node_types") or {}).keys()),
            "note": ("ambient_scopes removed for haystack isolation (symmetric across "
                     "packs; recorded beside the untouched file hash)"
                     if ambient_stripped else "no ambient scopes to strip"),
        }
        print(f"  pack {pn}: space {rs.space_id} "
              f"({'ambient stripped' if ambient_stripped else 'clean'}, "
              f"{len(pack_meta[pn]['node_types'])} node types)")

    representative_space = pack_spaces[pack_names[0]][0]
    _reader_system, reader_manifest = build_reader_system(args.reader_stance)
    reader_manifest = {**reader_manifest, "effort": "medium",
                       "model": ROLE_MODELS["reader"]["model"]}

    manifest_path = ck.path("manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() \
        else build_manifest(args, corpus, arms, pack_meta, representative_space,
                            reader_manifest, dataset, questions_in_file)
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    if "ingest" in phases or "answer" in phases:
        embedding.load_model()  # warm once, outside anything timed

    pool = AsyncConnectionPool(APP_DB, open=False, min_size=2, max_size=8)
    await pool.open()
    quota_stop = False
    try:
        if "ingest" in phases:
            print("\n== ingest ==")
            await phase_ingest(pool, ck, corpus, arms, pack_spaces, "bench")
        if "answer" in phases:
            print("\n== answer ==")
            await phase_answer(pool, ck, corpus, arms, pack_spaces, "bench",
                               concurrency=args.concurrency, stance=args.reader_stance)
    except QuotaExhausted as exc:
        # A usage limit during answering is a clean, resumable stop -- the errored
        # questions were NOT checkpointed, so a resume re-answers them. Skip the
        # downstream phases rather than judging a half-answered set.
        quota_stop = True
        print(f"\n  [quota] {exc}")
        print("  [stop] class=usage")
    except TransientRunStop as exc:
        # A self-healing CLI-launch outage (a Claude Code self-update swapping
        # claude.exe). Clean and resumable exactly like a usage stop -- no failed
        # answer was checkpointed -- but declared distinctly so the supervisor
        # backs off briefly and relaunches rather than sleeping to a usage reset.
        quota_stop = True
        print(f"\n  [transient] {exc}")
        print("  [stop] class=transient")
    finally:
        await pool.close()

    # Parallel judging is safe only for the claude route; the gemini client
    # shares a per-minute rate limiter that concurrency would corrupt.
    judge_conc = args.judge_concurrency if args.judge == "claude" else 1
    if ("judge" in phases or "calibrate" in phases) and not quota_stop:
        print(f"  judge: {args.judge}"
              + (f" (SAME-VENDOR — see manifest judge_neutrality; concurrency {judge_conc})"
                 if args.judge == "claude" else " (cross-vendor, sequential)"))
    if "judge" in phases and not quota_stop:
        print("\n== judge ==")
        quota_stop = not phase_judge(ck, corpus, lambda: _build_judge(args.judge),
                                     concurrency=judge_conc)
    if "calibrate" in phases and not quota_stop:
        print("\n== calibrate ==")
        manifest["judge_calibration"] = phase_calibrate(
            ck, corpus, _build_judge(args.judge), args.calibration_sample)

    # Failure attribution needs the verdicts (from judge) and the store (a probe),
    # so it runs after judging with its own short-lived pool -- sequentially, to
    # avoid the recall-UPDATE deadlock the concurrent answer loop hit.
    if "diagnose" in phases and not quota_stop:
        print("\n== diagnose ==")
        dpool = AsyncConnectionPool(APP_DB, open=False, min_size=2, max_size=4)
        await dpool.open()
        try:
            await phase_diagnose(dpool, ck, corpus, arms, pack_spaces, "bench")
        finally:
            await dpool.close()

    manifest["ingest"] = ck.rows("ingest.jsonl")
    manifest["extrapolation"] = _extrapolate(ck, corpus, arms, args.judge)
    manifest["resolved_models"] = _resolved_models(ck)
    # Sticky. A later `--phases report` invocation must not erase the fact that
    # an earlier pass was cut short by quota -- re-rendering a report is not
    # evidence that the grading it renders ever finished, and a manifest saying
    # `quota_stop: false` over a half-graded run is exactly the kind of quiet
    # overclaim the manifest exists to prevent.
    if quota_stop or "judge" not in phases:
        manifest["quota_stop"] = manifest.get("quota_stop") or quota_stop
    else:
        manifest["quota_stop"] = quota_stop
    if "calibrate" not in phases and "judge_calibration" not in manifest:
        manifest["judge_calibration"] = {
            "instability": None,
            "note": "not measured on this run — the calibration phase did not run.",
        }

    if "report" in phases:
        print("\n== report ==")
        agg = phase_report(ck, manifest)
        for arm, v in sorted(agg.items()):
            o, x = v["overall"], v["overall_excl_adversarial"]
            print(f"  {arm:<44} n={o['n']:<4} acc={o['accuracy']:.3f} "
                  f"(excl-adv {x['accuracy']:.3f} on n={x['n']})")
        print(f"\n  report: {ck.path('report.md')}")
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    space_ids = [sid for sid, _ in pack_spaces.values()]
    if args.teardown and not quota_stop:
        for sid in space_ids:
            _teardown(sid)
        print(f"  spaces dropped: {space_ids}")
    elif quota_stop:
        print(f"\n  spaces KEPT ({space_ids}) — rerun the same command to resume.")

    return 0


def _resolved_models(ck: Checkpoint) -> dict:
    """The model ids that actually did the work, taken from the recorded rows.

    `--model sonnet` is an alias; the manifest must record what ran, because the
    whole point of pinning is that a cheaper model cannot silently move the
    headline.
    """
    readers = {r.get("reader_model") for r in ck.rows("answers.jsonl") if r.get("reader_model")}
    judges = {r.get("judge_model") for r in ck.rows("verdicts.jsonl") if r.get("judge_model")}
    return {
        "reader": sorted(readers),
        "judge": sorted(judges),
        "reader_note": (
            "A SET, not a single id, and it legitimately contains a Haiku model "
            "alongside the requested Sonnet. The CLI runs a second model for its own "
            "internal work, and ClaudeCLIClient._resolved_model reports whichever "
            "model emitted the most output tokens in that call -- for a one-line "
            "reader answer that is sometimes the internal model rather than the one "
            "that answered. So this field bounds which models were involved; it does "
            "not prove which produced a given answer. The requested alias is recorded "
            "verbatim under role_models. Recorded as a known imprecision rather than "
            "resolved by guessing."
        ),
    }


def _teardown(space_id: str) -> None:
    with psycopg.connect(DB, autocommit=True) as c:
        cur = c.cursor()
        for t in ("edges", "dedup_log", "pending_writes", "inbox", "audit_log", "nodes",
                  "scope_grants", "scopes", "edge_rules", "edge_types", "node_types",
                  "config", "api_tokens", "principals"):
            cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
        cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
