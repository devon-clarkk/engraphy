#!/usr/bin/env bash
# Extraction validation on the SEEN conversations only (conv-26, conv-30, conv-49).
# The held-out seven are deliberately untouched: they are the benchmark session's
# final number.
#
# One run, two arms, so both stores come from the same engine, the same models and
# the same dedup policy, and differ only in the extraction prompt:
#   llm       -> bench/prompts/extract.md       (shipped, the control)
#   llm_wide  -> bench/prompts/extract-wide.md  (the change)
# Each arm gets its own scope, so the two stores never mix.
#
# Resumable: rerun after a usage cap and it continues from the checkpoint.
set -u
cd "$(dirname "$0")/.."
export ENGRAPHY_TEST_DATABASE_URL="${ENGRAPHY_TEST_DATABASE_URL:-postgres://postgres:engraphy@127.0.0.1:5441/engraphy_bench?sslmode=disable}"
RUN=extract-ab
SPACE=bench-${RUN}-conversational

python -m bench.core.run --haystacks conv-26,conv-30,conv-49 \
  --arm llm-conversational:search_only \
  --arm llm_wide-conversational:search_only \
  --run-id "$RUN" --phases ingest
rc=$?
if [ $rc -ne 0 ]; then
  echo "INGEST INCOMPLETE (exit $rc) $(date -u +%FT%TZ): rerun this script to resume"
  exit $rc
fi

python -m bench.extraction_coverage \
  --space "$SPACE" --extractor llm \
  --space "$SPACE" --extractor llm_wide \
  --haystacks conv-26,conv-30,conv-49 \
  --gap-labels ../engraphy-benchmarks-completeness/analysis/2026-09-30-extraction-gap-labels.json \
  --out runs/coverage-ab.json
echo "DONE $(date -u +%FT%TZ)"
