# Benchmark datasets — acquisition

[`bench/RUN-LOCOMO.md`](../RUN-LOCOMO.md) is the end-to-end walkthrough; this page
is the dataset half of it, in more detail.

Datasets are **not committed**. `datasets/` is gitignored: LoCoMo is CC BY-NC 4.0
(not ours to redistribute) and the files are large.

## LoCoMo

```sh
mkdir -p datasets
curl -sL https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json \
  -o datasets/locomo10.json
curl -sL https://raw.githubusercontent.com/snap-research/locomo/main/LICENSE.txt \
  -o datasets/locomo10.LICENSE.txt
```

- **Source:** `snap-research/locomo`, `data/locomo10.json`
- **Size:** 2,805,274 bytes
- **Licence: CC BY-NC 4.0 (Attribution-NonCommercial).** GitHub reports the repo
  licence as "Other / NOASSERTION"; the actual `LICENSE.txt` is Creative Commons
  Attribution-NonCommercial 4.0. **The NonCommercial term is a live question for
  any use of these numbers in commercial marketing material** — internal
  measurement and research are squarely fine, publication as product marketing is
  a decision for the project owner, not an engineering call.

### What the file actually contains (verified 2026-07-22)

`LoCoMoLoader` parses it without modification. Measured, not assumed:

| | |
|---|---|
| haystacks (conversations) | 10 |
| sessions | 272 |
| turns | 5,882 |
| **questions** | **1,986** |
| adversarial (abstention) | 446 |
| single-hop | 841 |
| temporal-reasoning | 321 |
| multi-hop | 282 |
| open-domain-knowledge | 96 |

**The published figure is 1,540; this file holds 1,986.** The difference is
exactly the 446 adversarial questions (1,986 − 446 = 1,540), so the headline
number quoted in the literature evidently excludes the adversarial category.
Anyone comparing an Engraphy figure against a published LoCoMo figure must state
which denominator they used, or the comparison is meaningless. The harness
carries all five categories and reports per-category, so either denominator can
be reconstructed from a run.

Loader liberties on this file: 6 non-string answers coerced to text, 0
image-only turns dropped.

## LongMemEval

Not acquired. Several hundred MB (`longmemeval_s` alone is ~115k tokens per
question across 500 questions), distributed via a Google Drive link from its
repo. Deferred deliberately — see design/09.
