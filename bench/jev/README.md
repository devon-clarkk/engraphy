# Jev rerank experiment

Tests one idea: reorder `search()`'s top 25 by Jev's relevance score and see whether recall@5 and recall@10 go up. The code is bench-only and nothing under `engraphy/` imports it.

## Before the first real run

1. Get API access from TypeSafe (it is waitlisted).
2. Read their API reference and replace the three parts of `client.py` marked `STUB`:
   - `_request_body`: the request JSON.
   - `_parse_score`: where the score is in the response.
   - The auth header in `_post`.
3. Set these environment variables. Keep them in your shell or an ignored `.env`, never in a commit:
   ```
   export JEV_API_KEY=...
   export JEV_API_URL=...        # the scoring endpoint from their docs
   export JEV_MODEL=...          # only if the API takes one
   ```

## Running

Start with the offline control arm. It needs no key and uses the same pipeline:

```
python -m bench.jev_rerank_recall --dataset datasets/locomo10.json \
    --haystacks conv-26,conv-30,conv-49 --scorer lexical
```

Then preview Jev's cost, do a small smoke run, and finally the full run. `--skip-load` reuses the store from the previous run:

```
python -m bench.jev_rerank_recall ... --scorer jev --skip-load --estimate-only
python -m bench.jev_rerank_recall ... --scorer jev --skip-load --max-questions 20
python -m bench.jev_rerank_recall ... --scorer jev --skip-load --out runs/jev.jsonl
```

Scores are cached in `runs/jev-cache.jsonl`, so a re-run with the same prompt costs nothing.

## What counts as a win

- `ceiling_at_depth` is the most any reorder can reach, so the gap between it and `baseline` is all a reranker can recover. If that gap is small, stop there.
- Jev has to beat both `baseline` and the `lexical` arm, with `gained` clearly larger than `lost`.
- `rrf_with_base` is the shape an engine integration would take, through the reranking hook in `core/rerank.py`. It is the number that decides whether to wire Jev in.
