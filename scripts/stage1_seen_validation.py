"""Stage 1: validate the candidate extraction lever on the SEEN conversations.

    python scripts/stage1_seen_validation.py

The held-out seven stay untouched; they carry the final number. This stage runs
on conv-26, conv-30 and conv-49, and follows the rule fixed in engraphy-benchmarks
`analysis/2026-09-30-settling-run-preregistration.md`, addendum 2026-10-01.

1. **Three stores.** `llm` (shipped, the control) and `llm_wide` (the candidate)
   in one run, so they share the engine, the models and the dedup policy and
   differ only in the extraction prompt. Then a second `llm` ingest under its own
   run id: extraction is a model call, so two ingests of the same prompt differ,
   and that difference is the floor this lever has to clear. Measured, not
   assumed. Retrieval is held at 25 on every arm, the width a promotion would
   ship.
2. **Store coverage, no model in the loop.** Is a question's cited evidence
   present in any stored memory of that scope? That is the ceiling on what any
   read path could surface, and it is reported beside store size, because
   coverage bought by storing everything is not a win.
3. **The gate.** The answer and judge pass is paid for only if the coverage gain
   clears the replicate floor and an exact McNemar test over the paired
   per-question outcomes. `scripts/extraction_gate.py` applies it.
4. **Accuracy, paired per question**, on both arms under the same reader and the
   same strict judge, if the gate opens.

Each model-calling step runs under the supervisor, so a usage cap pauses and the
same command resumes. An expired session halts instead, because waiting cannot
fix one.

Promotion into the combined engine is still a judgement against the full rule,
including the adversarial cost, and this script does not make it.
"""
import datetime
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
PY = sys.executable
LOG = REPO / "runs" / "stage1.log"
RUN = "extract-ab-seen"
RUN_REP = "extract-ab-seen-rep"
SEEN = "conv-26,conv-30,conv-49"
SPACE = f"bench-{RUN}-conversational"
SPACE_REP = f"bench-{RUN_REP}-conversational"
# The settle database, not the levers one: this run's stores stay beside the
# held-out run's, and arms are kept apart by scope, not by database.
DSN = "postgres://postgres:engraphy@127.0.0.1:5442/engraphy_bench?sslmode=disable"
GAP_LABELS = ("../engraphy-benchmarks-completeness/analysis/"
              "2026-09-30-extraction-gap-labels.json")
COMMON = ["--dataset", "datasets/locomo10.json", "--haystacks", SEEN,
          "--judge", "claude", "--reader-stance", "grounded",
          "--reader-contract", "verify",
          "--concurrency", "3", "--judge-concurrency", "4"]


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}"
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def supervised(cmd: list[str], run_dir: str, log_name: str) -> int:
    full = [PY, "-m", "bench.supervise", "--run-dir", run_dir, "--log", log_name, "--", *cmd]
    out = (REPO / "runs" / f"{pathlib.Path(log_name).stem}.stdout").open("a", encoding="utf-8")
    rc = subprocess.run(full, cwd=REPO, stdout=out, stderr=subprocess.STDOUT,
                        check=False).returncode
    log(f"exit {rc}")
    return rc


os.environ.setdefault("ENGRAPHY_TEST_DATABASE_URL", DSN)
bad = [v for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") if os.environ.get(v)]
if bad:
    log(f"refusing to start: {bad} set; the CLI route must use the subscription")
    sys.exit(2)

head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                      text=True, check=False).stdout.strip()
log(f"stage 1 start, engine {head}, seen split {SEEN}")

log("step 1a: both extraction arms, one run, ingest only")
rc = supervised([PY, "-m", "bench.core.run", *COMMON,
                 "--arm", "llm-conversational:search_only:k=25",
                 "--arm", "llm_wide-conversational:search_only:k=25",
                 "--phases", "ingest", "--run-id", RUN],
                f"runs/{RUN}", f"runs/{RUN}.supervise.log")
if rc != 0:
    log("ingest did not finish; rerun this script to resume")
    sys.exit(1)

log("step 1b: a second llm ingest, the noise floor this lever has to clear")
rc = supervised([PY, "-m", "bench.core.run", *COMMON,
                 "--arm", "llm-conversational:search_only:k=25",
                 "--phases", "ingest", "--run-id", RUN_REP],
                f"runs/{RUN_REP}", f"runs/{RUN_REP}.supervise.log")
if rc != 0:
    log("the replicate ingest did not finish; rerun this script to resume")
    sys.exit(1)
log("ALL THREE STORES INGESTED")

log("step 2: store coverage across the three stores, no model in the loop")
coverage = [PY, "-m", "bench.extraction_coverage", "--dsn", DSN,
            "--space", SPACE, "--extractor", "llm",
            "--space", SPACE, "--extractor", "llm_wide",
            "--space", SPACE_REP, "--extractor", "llm",
            "--haystacks", SEEN,
            *(("--gap-labels", GAP_LABELS) if (REPO / GAP_LABELS).exists() else ()),
            "--out", f"runs/{RUN}/coverage.json"]
rc = subprocess.run(coverage, cwd=REPO, check=False).returncode
if rc != 0:
    log(f"coverage failed with exit {rc}")
    sys.exit(1)

log("step 3: the pre-registered gate")
gate = subprocess.run([PY, str(REPO / "scripts" / "extraction_gate.py"),
                       f"runs/{RUN}/coverage.json"], cwd=REPO, capture_output=True,
                      text=True, check=False)
for line in gate.stdout.splitlines():
    log(f"  {line}")
(REPO / "runs" / RUN / "gate.txt").write_text(gate.stdout, encoding="utf-8", newline="\n")

if gate.returncode == 3:
    log("GATE CLOSED: the wider prompt does not move what is stored beyond the "
        "replicate floor, so the answer pass is not paid for and the lever is dropped")
    log("STAGE 1 COMPLETE")
    sys.exit(0)
if gate.returncode != 0:
    log(f"the gate could not be decided (exit {gate.returncode}); stopping for a look")
    sys.exit(2)

log("step 4: answers and judging on both arms, paired per question")
rc = supervised([PY, "-m", "bench.core.run", *COMMON,
                 "--arm", "llm-conversational:search_only:k=25",
                 "--arm", "llm_wide-conversational:search_only:k=25",
                 "--phases", "answer,judge,report", "--run-id", RUN],
                f"runs/{RUN}", f"runs/{RUN}.supervise.log")
if rc != 0:
    log("the answer and judge pass did not finish; rerun this script to resume")
    sys.exit(1)
log("STAGE 1 COMPLETE")
