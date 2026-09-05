# Adopting the `micro` embedder

For the operator of a running Engraphy instance who wants the memory back.
Nothing here has been run against any live server; this is the procedure for when
you choose to.

`micro` runs gte-small on ONNX Runtime instead of nomic-embed-text-v1.5. It is
the only profile that changes the model rather than the executor, so read section
1 before you decide, and section 2 before you start.

## 1. What you get and what it costs

Measured on one Linux x86-64 host, one profile per fresh process
(`scripts/embedding_memprobe.py`), and over 498 evidence-bearing LoCoMo
questions against 1,297 turn-nodes (`python -m bench.retrieval_recall`):

| | `onnx-fp32` (default) | `onnx-int8` | `micro` |
|---|---:|---:|---:|
| steady resident memory | 882 MB | 262 MB | **143 MB** |
| ms per embed | 58.6 | 23.1 | **5.1** |
| fused recall@10 | not measured here | 0.685 to 0.691 | **0.643 to 0.645** |
| vector width | 384 | 384 | 384 |
| schema migration | n/a | none | **none** |

Against the int8 profile that is the closest comparison: **45% less resident
memory and about 4x faster per embed, for about 6% less retrieval recall.**
Against the shipped fp32 default the memory saving is 84%.

The recall figure is the one to weigh. That gap is about 23 questions out of 498
whose evidence no longer surfaces in the top ten. Both figures are the spread of
repeated runs, and the spread is roughly three questions either way: the
approximate HNSW index moves it, not the embedder. If your store is a personal
memory of a few thousand facts on a machine where 882 MB is the problem, losing
6% of recall is a good trade. If retrieval quality is the product, it is not.

**Take it for the memory, not for the speed.** The speed is real and it is a side
effect; nothing in Engraphy's serving path is embed-bound at these sizes.

## 2. What you are taking on

`micro` is a different **vector space**, and unlike the int8 move it is a
different **model**. Three consequences, in the order they will bite you.

**A full re-embed is mandatory, not recommended.** Every stored vector has to be
rewritten. Until it is, the write path bands an incoming gte-small vector against
stored nomic vectors, and those two numbers have no relationship at all. This is
worse than the int8 case in a way worth being precise about: moving to int8 was
the same model quantized, so the mixed state was a small systematic contraction
with a safe error direction (a near-duplicate opened a confirm round trip rather
than merging silently). Across two different models there is no such guarantee.
A mixed store can merge unrelated facts as easily as it can split identical ones.

**Four thresholds change, and they are all calibrated.** `micro` ships its own
values and you do not need to set them, but you should know what moved:

| | fp32 default | `micro` | derived from |
|---|---:|---:|---|
| `dedup.t_high` | 0.95 | 0.955 | 17 committed dedup fixtures |
| `dedup.t_low` | 0.80 | 0.902 | the same 17 |
| `resonance.floor` | 0.75 | 0.90 | behaviour parity on those 17 |
| `briefing.semantic_floor` | 0.50 | 0.81 | the labelled briefing fixture |

They move because gte-small scores every pair higher and packs them into a
narrower range. A clearly-unrelated pair sits around 0.71 on gte-small where it
sits around 0.45 on nomic, so a floor carried over unchanged would sit under every
node in the store: a briefing whose "relevant" section is everything, a resonance
report that resonates with anything. Nothing errors when that happens, which is
why it is called out here rather than left to be discovered.

**One of those windows is narrow.** `dedup.t_low` is admitted anywhere in
(0.9005, 0.9034] on this fixture set, which is 0.0029 wide, against 0.0221 for
the fp32 space. Measured on four hosts across two instruction sets and three CPU
vendors, all four windows contained the shipped 0.902, which is why a single
default ships at all. It is still a quarter of int8's room and an eighth of
fp32's. If your store matters, re-derive it on your own hardware in step 3.

**Rolling back is another full re-embed.** Not a restart. Plan the window
accordingly, or plan to restore the backup instead.

## 3. Calibrate on the target host

Optional if you are running on x86-64 or aarch64 Linux and are content with the
shipped defaults. Worth ten minutes if the store matters.

```
docker compose -f compose.yaml -f compose.micro.yaml --profile admin \
  run --rm admin python scripts/baseline_dedup_fixtures_profile.py \
  --profile micro --t-high 0.955 --t-low 0.902

docker compose -f compose.yaml -f compose.micro.yaml --profile admin \
  run --rm admin python scripts/baseline_similarity_floors_profile.py \
  --profile micro
```

The first prints every fixture's similarity, the band it selects, and the two
windows this host admits. The second prints the same for the resonance and
semantic floors. Read the **width** of each window, not only whether the shipped
value falls inside it: a value sitting inside a window 0.003 wide is a value that
may not survive your next CPU.

If a shipped default falls outside a window on your host, set the space's own:

```
engraphy-admin config set --space <space> --key dedup.t_low --value <yours>
```

## 4. Back up, and prove the backup

Not optional. The backfill rewrites every vector in the store.

```
engraphy-admin verify-restore --database-url "$SUPERUSER_URL"
```

## 5. Switch the profile and re-embed

Both the server and the admin sidecar have to move together. A sidecar that
re-embeds with one model while the server reads with another writes a store
neither can search, which is why the overlay sets both.

```
docker compose -f compose.yaml -f compose.micro.yaml up -d --build
docker compose -f compose.yaml -f compose.micro.yaml --profile admin \
  run --rm admin engraphy-admin reembed --space <space> --dry-run
docker compose -f compose.yaml -f compose.micro.yaml --profile admin \
  run --rm admin engraphy-admin reembed --space <space>
```

Repeat per space. `--scope` limits it further if you want to stage the work.

On a prebuilt image, pull the `-micro` tag instead of building:

```yaml
# compose.yaml
services:
  engraphy:
    image: ghcr.io/devon-clarkk/engraphy:<version>-micro
  admin:
    image: ghcr.io/devon-clarkk/engraphy-admin:<version>-micro
```

**Expected duration:** about 5ms per node on this backend, so roughly 75 seconds
per 15,000 nodes plus database round trips. A store of a few thousand nodes
finishes in seconds. The command prints progress as it goes.

**While it is running, the store is part converted and the write path is
comparing across two models.** Unlike the int8 backfill there is no safe error
direction here, so prefer a quiet window and run it to completion. If writes
cannot be paused, accept that some near-duplicate decisions taken during the run
may be wrong and review the confirm queue afterwards.

**If it is interrupted, run it again.** Selection is on `nodes.embedding_model`
and each row's vector and stamp are written in one statement, so it resumes at
exactly the remainder and a completed run finds nothing.

## 6. Confirm

```
curl -s localhost:8000/healthz | jq .embedding_model
# "Xenova/gte-small+micro"

engraphy-admin reembed --space <space> --dry-run
# nothing to do: every row is already in this vector space.
```

Then check the two things the fixtures cannot check for you, because no fixture
in this repo labels either decision on your data:

- **Resonance.** Write a node that has nothing to do with anything already
  stored. Its resonance report should be empty. If unrelated facts are resonating,
  `resonance.floor` is too low for your corpus; raise it per space.
- **A semantic briefing section.** Run a briefing whose hint is narrow, and read
  what came back. If the section returns everything rather than the relevant
  things, `briefing.semantic_floor` is too low; raise it per space.

```
engraphy-admin config set --space <space> --key resonance.floor --value 0.92
engraphy-admin config set --space <space> --key briefing.semantic_floor --value 0.85
```

Read-time near-duplicate collapse has no per-space config key today. It is a code
default (`core/search.py`, `_PROFILE_READ_DEDUP_SIM`), 0.97 on `micro`. If search
results look folded together, that is the value to look at, and changing it means
a build.

## 7. Rolling back

A re-embed in the other direction, not a restart, because the stored vectors are
in gte-small's space.

```
docker compose up -d           # the overlay dropped: back to the default profile
docker compose --profile admin run --rm admin engraphy-admin reembed --space <space>
```

Then remove any threshold config rows you set in steps 3 or 6, so the space
returns to the default profile's code defaults.

Restoring the step-4 backup is the faster route if the store is large and the
backup is recent.

## 8. Quick reference

| Move | Data work | Calibration | Rollback |
|---|---|---|---|
| default to `onnx-int8` | `reembed`, every space | per host, required | `reembed` back |
| default to `micro` | `reembed`, every space, **mandatory** | shipped; re-derive if the store matters | `reembed` back |
| `onnx-int8` to `micro` | `reembed`, every space, **mandatory** | as above | `reembed` back |
| `micro` to `micro` on new hardware | none | re-derive `dedup.t_low`, its window is 0.003 wide | n/a |
