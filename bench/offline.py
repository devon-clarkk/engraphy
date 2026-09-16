"""Offline passes over a completed run: re-read its saved retrieval results, then grade.

A run saves every retrieval envelope it put in front of the reader
(`envelopes.jsonl`). An offline pass reads those envelopes again, under a
different reader or different conventions, and grades the new answers. It never
touches the store and never retrieves, so two passes over one run differ in
exactly the reader and the grading, and one expensive run yields every figure
that depends only on them.

Two instruments share this engine:

- `bench.reference_pass`, the matched-convention figure (reference reader and judge);
- `bench.replay`, reader validation (a candidate reader against a baseline reader).

The output directory has the shape the supervisor reads: `answers.jsonl` grows as
answers are checkpointed, and a finished pass writes `report.md` and a
`manifest.json` with `quota_stop: false`. A usage limit stops the pass cleanly
with `[stop] class=usage`, and the same command resumes it, so an offline pass
can run under `bench.supervise` exactly as a run does.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime
import hashlib
import json
import pathlib
from collections.abc import Callable

from bench.core import run as harness
from bench.core.judge import Verdict
from bench.core.providers import QuotaExhausted
from bench.core.report import aggregate

__all__ = ["Source", "finish", "judge_pass", "load_source", "read_pass", "resolve_run_dir",
           "stop"]


def resolve_run_dir(value: str | pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(value)
    if path.is_dir():
        return path.resolve()
    if (harness.RUNS / path).is_dir():
        return (harness.RUNS / path).resolve()
    raise SystemExit(f"no run directory at {value}")


class Source:
    """A completed run, as an offline pass sees it."""

    def __init__(self, run_dir: pathlib.Path, envelopes: pathlib.Path | None = None) -> None:
        self.dir = run_dir
        self.manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        dataset = harness.REPO / "datasets" / self.manifest["dataset"]["path"]
        corpus = harness.LOADERS[self.manifest.get("benchmark", "locomo")]().load(dataset)
        wanted = set(self.manifest["haystacks"])
        self.haystacks = {h.haystack_id: h for h in corpus.haystacks if h.haystack_id in wanted}
        self.questions = {q.question_id: q for q in corpus.questions if q.haystack_id in wanted}
        self.results = {(r["arm"], r["question_id"]): r
                        for r in harness.load_rows(run_dir / "results.jsonl")}
        env_path = envelopes or run_dir / "envelopes.jsonl"
        self.envelope_source = str(env_path)
        self.envelopes: dict[str, dict] = {}
        self.arms: set[str] = set()
        for row in harness.load_rows(env_path):
            self.envelopes[row["question_id"]] = row["envelope"]
            self.arms.add(row["arm"])
        if len(self.arms) != 1:
            raise SystemExit(f"{env_path}: expected envelopes for one arm, found {sorted(self.arms)}")
        self.arm = next(iter(self.arms))

    def source_row(self, question_id: str) -> dict | None:
        """The run's own result for a question, under the run's arm."""
        for (arm, qid), row in self.results.items():
            if qid == question_id and not arm.endswith(("/reference-conventions",)):
                return row
        return None


def load_source(run_dir, envelopes=None) -> Source:
    return Source(resolve_run_dir(run_dir), pathlib.Path(envelopes) if envelopes else None)


def envelope_digest(envelope: dict) -> tuple[int, str]:
    blob = json.dumps(envelope).encode("utf-8")
    return len(blob), hashlib.sha256(blob).hexdigest()


def read_pass(ck: harness.Checkpoint, todo: list[dict], read_one: Callable[[dict], dict], *,
              concurrency: int) -> str | None:
    """Read every item not yet answered. Returns a stop reason, or None when done.

    `read_one` runs on a worker thread and returns a result row; it raises
    `QuotaExhausted` on a usage limit. A row carrying `error` is not checkpointed,
    so a resume reads it again. Checkpoint appends happen on this thread only.
    """
    have = ck.keys("answers.jsonl", "arm", "question_id")
    todo = [t for t in todo if (t["arm"], t["question_id"]) not in have]
    print(f"  [read] {len(todo)} to read at concurrency {concurrency}", flush=True)
    done = consecutive = 0
    last_error = ""
    stop_reason = None
    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        pending: set = set()
        it = iter(todo)

        def submit() -> None:
            item = next(it, None)
            if item is not None and stop_reason is None:
                pending.add(ex.submit(_call, read_one, item))

        for _ in range(max(1, concurrency)):
            submit()
        while pending:
            finished, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for fut in finished:
                kind, payload = fut.result()
                if kind == "quota":
                    stop_reason = stop_reason or f"usage limit: {payload}"
                    continue
                if payload.get("error"):
                    last_error = payload["error"]
                    consecutive += 1
                    if consecutive >= 8 and stop_reason is None:
                        stop_reason = f"8 consecutive reader errors; last: {payload['error']}"
                    continue
                consecutive = 0
                ck.append("answers.jsonl", payload)
                done += 1
                if done % 25 == 0:
                    print(f"         {done}/{len(todo)}", flush=True)
            for _ in finished:
                submit()
    print(f"         {done} read", flush=True)
    if stop_reason is None and done < len(todo):
        stop_reason = f"{RESIDUAL} {len(todo) - done} not read; last reader error: {last_error}"
    return stop_reason


def judge_pass(ck: harness.Checkpoint, grade_one: Callable[[dict], Verdict], *,
               concurrency: int) -> str | None:
    """Grade every answered row not yet graded. A `judge_error` is never checkpointed."""
    have = ck.keys("verdicts.jsonl", "arm", "question_id")
    todo = [r for r in ck.rows("answers.jsonl") if (r["arm"], r["question_id"]) not in have]
    print(f"  [grade] {len(todo)} to grade at concurrency {concurrency}", flush=True)
    done = consecutive = 0
    last_error = ""
    stop_reason = None
    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        pending: set = set()
        it = iter(todo)

        def submit() -> None:
            row = next(it, None)
            if row is not None and stop_reason is None:
                pending.add(ex.submit(_call, lambda r: (r, grade_one(r)), row))

        for _ in range(max(1, concurrency)):
            submit()
        while pending:
            finished, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for fut in finished:
                kind, payload = fut.result()
                if kind == "quota":
                    stop_reason = stop_reason or f"usage limit: {payload}"
                    continue
                row, verdict = payload
                if verdict.graded_by == "judge_error":
                    last_error = verdict.error
                    consecutive += 1
                    if consecutive >= 5 and stop_reason is None:
                        stop_reason = f"5 consecutive judge failures; last: {verdict.error}"
                    continue
                consecutive = 0
                ck.append("verdicts.jsonl", {"arm": row["arm"], "question_id": row["question_id"],
                                             **verdict.as_dict()})
                done += 1
                if done % 50 == 0:
                    print(f"         {done}/{len(todo)}", flush=True)
            for _ in finished:
                submit()
    print(f"         {done} graded", flush=True)
    if stop_reason is None and done < len(todo):
        stop_reason = f"{RESIDUAL} {len(todo) - done} not graded; last judge error: {last_error}"
    return stop_reason


def _call(fn, item):
    try:
        return "ok", fn(item)
    except QuotaExhausted as exc:
        return "quota", exc


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


# A pass that finished its loop with some rows still failing. The failures were
# not checkpointed, so a relaunch retries exactly those rows.
RESIDUAL = "residual errors:"


def stop_class(reason: str) -> str:
    """How the supervisor should treat a stop: sleep to a usage reset, relaunch
    after a short backoff, or halt for attention (a breaker trip is a defect)."""
    if reason.startswith("usage limit"):
        return "usage"
    if reason.startswith(RESIDUAL):
        return "transient"
    return "hard"


def stop(ck: harness.Checkpoint, manifest: dict, reason: str) -> int:
    """A clean, resumable stop the supervisor recognises."""
    manifest = {**manifest, "quota_stop": True, "stop_reason": reason, "stopped_at": _now()}
    ck.path("manifest.json").write_text(json.dumps(manifest, indent=2, default=str),
                                        encoding="utf-8")
    print(f"\n  [quota] {reason}")
    print(f"  [stop] class={stop_class(reason)}", flush=True)
    return 0


def finish(ck: harness.Checkpoint, manifest: dict, title: str) -> dict:
    """Join answers and verdicts, aggregate, and write the finished pass."""
    verdicts = {(v["arm"], v["question_id"]): v for v in ck.rows("verdicts.jsonl")}
    results = []
    for row in ck.rows("answers.jsonl"):
        v = verdicts.get((row["arm"], row["question_id"]))
        if v is not None:
            results.append({**row, **v})
    path = ck.path("results.jsonl")
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in results),
                    encoding="utf-8")
    agg = aggregate(results)
    ungraded = len(ck.rows("answers.jsonl")) - len(results)
    manifest = {**manifest, "aggregate": agg, "quota_stop": False,
                "rows_answered_but_ungraded": ungraded, "finished_at": _now()}
    ck.path("manifest.json").write_text(json.dumps(manifest, indent=2, default=str),
                                        encoding="utf-8")
    lines = [f"# {title}", ""]
    for arm, a in agg.items():
        lines += [f"## `{arm}`", "", "| | accuracy |", "|---|---|"]
        rows = [("excluding adversarial", a["overall_excl_adversarial"]),
                ("all rows", a["overall"]), *sorted(a["categories"].items())]
        for name, b in rows:
            if b.get("n"):
                lo, hi = b["wilson_95"]
                lines.append(f"| {name} | {100 * b['accuracy']:.1f}% "
                             f"[{100 * lo:.0f} to {100 * hi:.0f}] ({b['correct']}/{b['n']}) |")
        lines.append("")
    ck.path("report.md").write_text("\n".join(lines), encoding="utf-8")
    return agg
