# Running the LoCoMo benchmark against Engraphy

Everything needed to take a clean checkout of this repository to a LoCoMo score:
the dataset pin, the arm, the models, the judge, the exact commands, and what the
run writes out. It needs an OpenAI-compatible API base URL and key, and nothing
else. No vendor CLI, no subscription, no free-tier daily cap to schedule around.

Read [What this reproduces](#what-this-reproduces) before comparing your number
to a published one.

---

## What this reproduces

This document reproduces **the published method**: the same dataset file, the
same arm, the same prompts, the same reader skill, the same best-of-3 judging,
the same per-category reporting, and the same manifest disclosure. Those are the
parts a reviewer has to be able to check, and they are checkable here.

It does not yet reproduce **the published number**, and the reason is worth
stating exactly rather than leaving to be discovered.

Engraphy's published figure is **67.1% excluding the adversarial category**
(261/389), from a 500-question run on 2026-08-09. That run was produced on the
`bench/full-run` branch of the private engine repository, recorded in its
manifest as `engine_git_sha 631e7be`, against schema 0023. Comparing that branch
against this repository's `main`:

| layer | state |
|---|---|
| `bench/prompts/extract.md`, `judge.md`, `adjudicate.md` | identical, hashes match (`b7fdf557f7f60a9c`, `947bebcd5375e93e`, `740151c30787a630`) |
| `skills/answer-discipline.md` (the reader's governing instruction) | identical, `c2afc35f…31328a89` |
| `packs/conversational/pack.yaml` (the published arm's ontology) | present |
| dataset pin and loader | identical |
| harness observability (`bench/core/ingest.py`, `retrieve.py`, `report.py`) | the run branch counts extraction failures, per-reason draft drops and traverse errors that `main` does not. Reporting only: it changes what a run tells you, not what it scores. |
| **engine write path** | **the run branch keeps a node whose typed attribute fails validation, quarantining the bad attribute rather than refusing the write; accepts partial dates (`2026`, `2026-05`); and downgrades a cross-type supersede to a plain write. `main` does none of the three.** |

The last row is the one that moves the score. Those three fixes took the run's
write-yield from 78.7% to 99.4% of extracted nodes actually stored, and LoCoMo
supplies partial dates constantly, so a run on `main` stores fewer facts and
answers fewer questions from them. Landing them here is separate, larger work
than this document covers, and it is tracked as its own item.

So: a run from this checkout is a real, self-contained LoCoMo measurement of
Engraphy on the models you point it at, and it is directly comparable to another
run from this checkout. Treat it as comparable to 67.1% only once the engine
write path above is on `main` and your manifest's `engine_git_sha` says so.

---

## What you need

- **Postgres 16 with pgvector.** The benchmark writes to a real database because
  the dedup write path, the schema triggers and Row-Level Security are all
  enforced in Postgres.
- **Python 3.12.**
- **An OpenAI-compatible endpoint**, meaning anything serving
  `POST /chat/completions`: OpenAI, Anthropic's OpenAI-compatibility layer,
  Google's, a gateway such as OpenRouter or LiteLLM, or a local vLLM.
- **Disk and patience.** The full ten-conversation suite is 1,986 questions and
  tens of thousands of model calls. Start with the three conversations the
  published run used.

Cost and wall-clock depend entirely on the models you choose, and this repository
makes no estimate it cannot stand behind. Measure it: run one conversation first
and read `manifest.json`, which records every call the run made.

---

## Step 1: the code

```bash
git clone https://github.com/devon-clarkk/engraphy
cd engraphy
pip install -e '.[dev]'
```

The first run downloads the embedding model (`nomic-ai/nomic-embed-text-v1.5`,
about 523 MB) and caches it. Embedding runs in-process, so nothing about a
memory write leaves the machine.

## Step 2: the database

```bash
docker run -d --name engraphy-bench -p 5433:5432 \
  -e POSTGRES_PASSWORD=engraphy -e POSTGRES_DB=engraphy_dev \
  pgvector/pgvector:pg16

export ENGRAPHY_TEST_DATABASE_URL='postgres://postgres:engraphy@localhost:5433/engraphy_dev?sslmode=disable'
dbmate --migrations-dir engraphy/db/migrations --url "$ENGRAPHY_TEST_DATABASE_URL" up
```

Port 5433 keeps this clear of a Postgres you may already be running on 5432.
`ENGRAPHY_TEST_DATABASE_URL` is what the harness reads; a run also connects as
the RLS-live `engraphy_app` role, which the migrations create.

A run **never** tears its space down on its own. `--teardown` is the only thing
that drops one, so an interrupted run resumes into exactly the store it left.

## Step 3: the dataset

LoCoMo is not committed here. It is CC BY-NC 4.0 and not ours to redistribute.

```bash
mkdir -p datasets
curl -sL https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json \
  -o datasets/locomo10.json
curl -sL https://raw.githubusercontent.com/snap-research/locomo/main/LICENSE.txt \
  -o datasets/locomo10.LICENSE.txt
```

Verify you have the pinned file. Every published Engraphy figure comes from this
one, and LoCoMo circulates in more than one revision:

```bash
sha256sum datasets/locomo10.json
# 79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4
# 2,805,274 bytes
```

The run records this digest in its manifest, so a mismatch is visible after the
fact as well as before.

**The denominator matters.** This file holds 1,986 questions, of which 446 are
adversarial. The figure usually quoted in the literature is over 1,540, which is
exactly the file minus the adversarial category. The harness carries all five
categories and reports per-category, so either denominator can be reconstructed
from a run, but a comparison that does not say which one it used is meaningless.
[`bench/datasets/README.md`](datasets/README.md) has the full verified counts.

## Step 4: the endpoint and credentials

Two independent routes, because they should be able to be different vendors. A
harness whose judge comes from the same vendor as the system under test invites
the obvious objection that it graded its own homework, and separating the
variables is what keeps the neutral posture reachable.

| variable | what it configures |
|---|---|
| `ENGRAPHY_BENCH_OPENAI_BASE_URL` | extractor, reader, adjudicator |
| `ENGRAPHY_BENCH_OPENAI_API_KEY` | |
| `ENGRAPHY_BENCH_OPENAI_STRUCTURED` | how JSON is asked for on that endpoint, see below |
| `ENGRAPHY_BENCH_JUDGE_BASE_URL` | the judge. Unset falls back to the three above, and the manifest says so |
| `ENGRAPHY_BENCH_JUDGE_API_KEY` | |
| `ENGRAPHY_BENCH_JUDGE_STRUCTURED` | |
| `ENGRAPHY_BENCH_OPENAI_RPM` | optional client-side requests-per-minute guard, off by default |
| `ENGRAPHY_BENCH_OPENAI_TEMPERATURE` | optional. Unset sends no temperature at all, which is what reasoning-tier models require |

Each is read from the environment first and from the repository's gitignored
`.env` second. Nothing here is ever written to the manifest except the hostname.

### Which structured-output mode your endpoint needs

Three roles, the extractor, the adjudicator and the judge, need JSON conforming
to a schema. Endpoints implement that differently, and picking wrong is silent:
an endpoint that ignores the field returns prose, and the role that needed JSON
records a failure.

| endpoint | `ENGRAPHY_BENCH_OPENAI_STRUCTURED` |
|---|---|
| OpenAI (`https://api.openai.com/v1`) | `json_schema` |
| Google (`https://generativelanguage.googleapis.com/v1beta/openai`) | `json_schema` |
| Anthropic (`https://api.anthropic.com/v1/`) | `tool_call` |
| OpenRouter, LiteLLM, vLLM, other gateways | `json_schema`, falling back to `tool_call` |
| anything implementing neither | `json_object` |

`json_schema` sends `response_format: {"type": "json_schema", ...}`. It is the
default.

`tool_call` sends the schema as a single forced function call and reads the
payload back from the call's arguments. **Anthropic's OpenAI-compatibility layer
requires it**: that layer's published support table lists `response_format` as
ignored, while a function's `parameters` and the returned `tool_calls` are fully
supported.

`json_object` asks for JSON and puts the schema in the system prompt. The shape
is requested rather than enforced, so it is the last resort, and the manifest
records that a run used it.

Note that `strict` schema conformance is not requested in any mode. OpenAI's
strict mode requires every key in `properties` to appear in `required`, and the
extraction schema deliberately leaves `attrs`, `supersedes_title` and
`source_turn_ids` optional, because a memory carrying no typed attributes is a
legitimate extraction. Forcing all three onto every memory would change what the
extractor produces and therefore what the run measures. Conformance is checked
where it always was, by the engine's own attr-spec validator, and a draft that
fails it is counted rather than hidden.

### The pinned models

| role | model | override with |
|---|---|---|
| extractor | `claude-opus-4-8` | `ENGRAPHY_BENCH_EXTRACTOR_MODEL` |
| reader | `claude-opus-4-8` | `ENGRAPHY_BENCH_READER_MODEL` |
| adjudicator | `claude-opus-4-8` | `ENGRAPHY_BENCH_ADJUDICATOR_MODEL` |
| judge | `claude-sonnet-5`, best-of-3 majority | `ENGRAPHY_BENCH_JUDGE_MODEL` |

These are the ids the published figure was produced with, pinned in
[`bench/core/llm.py`](core/llm.py) rather than only in this document, so a run
that used something else shows up in the manifest as a difference from the pin
rather than as an unexplained score.

**Every one of them is overridable, and every override changes the number.** A
run on different reader and judge models is measuring a different system. That is
a legitimate thing to want, which is why the overrides exist and why each is
recorded, but its result is not comparable to a figure produced on the models
above, in either direction. Say which models you ran when you quote a score.

Best-of-3 majority judging is part of the pin, not a tuning knob. The judge's
measured per-pass instability is 16.4% two-pass disagreement; a majority of three
flips only when two of three flip, which is much rarer. It applies identically on
every route.

## Step 5: preflight

One small call per role against the endpoint you just configured, using the same
client and the same schemas the run uses. It costs a few cents and it is worth
it: the alternative is discovering on hour six that the judge model is not served
there.

```bash
python -m bench.smoke_openai
```

It prints the resolved configuration first, with no credentials in it, then
exercises the extractor against the live pack-derived schema, the reader on prose
and on abstention, the adjudicator on a paraphrase and a contradiction, and the
real `Judge` through the real best-of-3 path. Exits non-zero if any role fails,
and names the fix.

## Step 6: the run

The published arm, on the three conversations the published run used:

```bash
python -m bench.core.run \
  --provider openai \
  --judge openai \
  --dataset datasets/locomo10.json \
  --haystacks conv-26,conv-30,conv-49 \
  --arm llm-conversational:search_only \
  --reader-stance grounded \
  --run-id my-locomo-run
```

For the whole suite, drop `--haystacks`. It is ten conversations and 1,986
questions, and it is expensive.

Reading the flags:

- `--provider openai` puts the extractor, reader and adjudicator on the
  OpenAI-compatible route. The alternative, `claude-cli`, is the maintainer's own
  free route and needs a Claude subscription.
- `--judge openai` puts grading there too. `gemini` and `claude` also exist.
- `--arm llm-conversational:search_only` is
  `<extractor>-<pack>:<strategy>`, defaulting the confirm policy to
  `always_distinct`. This is the published arm exactly.
- `--reader-stance grounded` allows a declared, sourced inference. `strict`
  declines whenever memory does not state the answer outright. Recorded in the
  manifest either way.
- `--concurrency` (default 3) bounds concurrent reader calls;
  `--judge-concurrency` (default 4) bounds concurrent grading. Raise them for a
  high-tier key, lower them for a low-tier one.

### Stopping and resuming

Run the identical command again. Every phase (`ingest`, `answer`, `judge`,
`calibrate`, `diagnose`, `report`) is resumable and skips work already on disk,
and answers are checkpointed the moment each one returns.

An exhausted API balance or daily cap is a **clean stop, not a crash**: the run
prints what it completed, exits 0 with every artifact intact, and the same
command later picks up from the checkpoint. A rate limit that will clear by
waiting is retried in place instead, honouring the endpoint's own `Retry-After`.

## What the run writes

Everything lands in `runs/<run-id>/`.

| file | what it holds |
|---|---|
| `manifest.json` | the whole configuration: dataset digest, arm, resolved model ids, endpoint hosts, structured mode, prompt hashes, pack file hashes, embedding model and revision, band thresholds read back from the live space, judge calibration, git SHA and whether the tree was dirty |
| `results.jsonl` | one row per question: the answer, the retrieval envelope, the verdict, the category |
| `verdicts.jsonl` | the grades, with the best-of-3 tally in each reason |
| `answers.jsonl` | reader output before grading |
| `ingest.jsonl` | per-conversation write statistics, including how many drafts became nodes |
| `report.md` | the rendered per-category table |
| `failures.md` | worked examples of what went wrong |

**Publish `manifest.json` and `results.jsonl` alongside any number you quote.**
The aggregate is the least checkable thing a run produces; the per-question rows
let a reader audit a sample instead of trusting it.

### Reading the manifest

Four fields carry most of the provenance:

- `dataset.digest` and `dataset.questions_in_file`: which file, and the
  denominator it carries.
- `role_models`: what performed each role. On this route each entry also carries
  `pinned_model`, and an `overridden_by` naming the variable when the two differ.
- `provider_config`: the endpoint host and structured-output mode per role.
  Hostnames only.
- `judge_neutrality` and `judge_endpoint_is_separate`: whether the judge ran on
  its own endpoint, and what that means for the cross-vendor posture. If the
  judge fell back to the reader's endpoint, the manifest says so in words rather
  than leaving you to compare two hostnames.

One gotcha worth knowing: `prompt_hashes` are hashes of the prompt files as they
sit in your working tree, so a Windows checkout with `core.autocrlf` on produces
different hashes for byte-identical prompts. Compare LF-normalised, or compare on
Linux.

## Caveats to carry with any number

**The score depends on the reader and judge models.** This is the largest single
factor after the system under test, and it is why published LoCoMo figures across
vendors are not directly comparable. Name your models.

**Judge neutrality is a property of your configuration, not of this harness.**
Pointing `ENGRAPHY_BENCH_JUDGE_BASE_URL` at a different vendor from
`ENGRAPHY_BENCH_OPENAI_BASE_URL` is the recommended posture for anything
published. The manifest states which you did.

**One run is one sample.** The harness measures judge instability on every run
and reports it as `judge_calibration`. It does not average across runs. Read the
confidence interval in `report.md` rather than the point estimate.

**LoCoMo is CC BY-NC 4.0.** Internal measurement and research are squarely fine.
Whether a number derived from it may appear in commercial marketing material is a
licensing question for whoever publishes it, not an engineering one, and this
repository does not answer it. See
[`bench/datasets/README.md`](datasets/README.md).

**Adversarial questions.** The harness scores the adversarial category as an
abstention case with its own gold key, rather than folding it silently into the
headline. It also does not flatten speaker names to `user` and `assistant`, which
is the single most effective way to inflate a LoCoMo score. Both choices lower
the number and both are deliberate; see
[`bench/adapters/locomo.py`](adapters/locomo.py).

## Reference: the published run's configuration

For anyone reconstructing it. From the 2026-08-09 run's manifest:

| | |
|---|---|
| questions | 500, from `conv-26`, `conv-30`, `conv-49` |
| overall | 71.6% [67 to 75] (358/500) |
| excluding adversarial | **67.1% [62 to 72] (261/389)** |
| arm | `llm-conversational / search_only / always_distinct` |
| reader stance | `grounded`, `effort=medium`, envelope rendered as `rendered_prose_v2` |
| extractor, reader, adjudicator | `claude-opus-4-8` |
| judge | `claude-sonnet-5`, best-of-3, same-vendor, drift measured at +0.0 pp over 100 regrades |
| embedding | `nomic-ai/nomic-embed-text-v1.5`, 384d |
| dataset | `locomo10.json`, `sha256:79fa87e9…ea698ff4` |
| database | pgvector pg16, schema 0023 |
| code | branch `bench/full-run`, `engine_git_sha 631e7be` |
| space config | none, shipped defaults throughout |

The judge on that run was same-vendor with the system under test, which is a
deviation from the cross-vendor posture, and the run's own manifest states it in
full under `judge_neutrality` rather than leaving it to be inferred from a model
id. Judge drift between that judge and the previous run's was measured at +0.0 pp
with answers held fixed.
