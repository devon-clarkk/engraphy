# Adopting 0.2.0 on a live server

For the operator of a running Engraphy instance. Nothing here was run against any
live server; this is the procedure for when you choose to.

Read the first section before anything else. On the shipped default this upgrade
needs no data work at all, and the long procedure further down applies only if you
opt into `onnx-int8`.

## 1. The short version

The 0.2.0 default profile (`onnx-fp32`) emits the same vectors as 0.1.0 and
carries the same `nodes.embedding_model` stamp. An existing store is already
correct.

```
docker compose pull
docker compose up -d
```

That is the whole upgrade. `engraphy-admin reembed` reports no work if you run it,
which is a useful way to confirm the store is where you expect.

**Expected duration:** the time to pull two images and restart, under a minute on
a warm host.

**Rollback:** repin to the previous image tag and restart. No data changed, so
there is nothing to undo.

```
# compose.yaml: image: ghcr.io/devon-clarkk/engraphy:0.1.0
docker compose up -d
```

## 2. Keeping the previous backend

The `legacy-torch` profile runs the same pinned weights through
sentence-transformers. It exists for anyone who wants byte-identical continuity
with what they were running.

```yaml
# compose.yaml, under the engraphy service
environment:
  ENGRAPHY_EMBEDDING_PROFILE: legacy-torch
```

The image does not carry torch, so this needs an image built with the extra:

```
docker build --target server \
  --build-arg ENGRAPHY_EMBEDDING_PROFILE=legacy-torch \
  -t engraphy:0.2.0-legacy-torch .
```

`onnx-fp32` and `legacy-torch` share a vector space and a stamp, so you can move
between them freely with a restart.

## 3. Opting into `onnx-int8`

Only do this if the memory or the speed matters to you. It is a real change to the
store and to write-path behaviour, and it has a calibration step that cannot be
skipped.

### 3.1 What you are taking on

int8 vectors are not interchangeable with the default profile's. Two consequences:

1. Every row has to be rewritten, or the write path compares new int8 vectors
   against old ones, which is a comparison across two vector spaces.
2. The dedup bands move, and **their correct values depend on your CPU**. On two
   Linux x86-64 hosts the same fixtures produced viable `dedup.t_low` windows of
   (0.8072, 0.8197] and (0.7809, 0.8017]. Those do not overlap. There is no
   universal value, which is why this is a per-host step rather than a default.

### 3.2 Calibrate on the target host

Run the baseline inside the image that will serve, on the machine that will serve:

```
docker compose run --rm --entrypoint python engraphy \
  scripts/baseline_dedup_fixtures_profile.py --profile onnx-int8
```

It prints every fixture's measured similarity and the band it selects. Read the
window from its output and pick a `t_low` inside it. If it reports a band
disagreement it exits non-zero and names the case; resolve that before continuing
rather than adjusting until it passes.

Set the value for each space:

```
engraphy-admin config set --space <space> --key dedup.t_low --value <your value>
engraphy-admin config set --space <space> --key dedup.t_high --value 0.94
```

Config beats the code default and is read per write with no cache, so this takes
effect on the next write.

### 3.3 Back up, and prove the backup

Not optional. The backfill rewrites every vector in the store.

```
engraphy-admin verify-restore --database-url "$SUPERUSER_URL"
```

### 3.4 Switch the profile and re-embed

```yaml
environment:
  ENGRAPHY_EMBEDDING_PROFILE: onnx-int8
```

```
docker compose up -d
engraphy-admin reembed --space <space> --dry-run    # count first
engraphy-admin reembed --space <space>
```

Repeat per space. `--scope` limits it further if you want to stage the work.

**Expected duration:** roughly 8ms per node on the int8 backend, so about two
minutes per 15,000 nodes, plus database round trips. A store of a few thousand
nodes finishes in well under a minute. The command prints progress as it goes.

**While it is running,** the store is part converted. The error direction is the
safe one: a near-duplicate opens a confirm round trip rather than merging
silently. Even so, run it to completion rather than leaving it half done, and
prefer a quiet window.

**If it is interrupted,** run it again. Selection is on the stamp and each row's
vector and stamp are written in one statement, so it resumes at exactly the
remainder. A completed run finds nothing.

### 3.5 Confirm

```
curl -s localhost:8000/healthz | jq .embedding_model
# "nomic-ai/nomic-embed-text-v1.5+onnx-int8"

engraphy-admin reembed --space <space> --dry-run
# nothing to do: every row is already in this vector space.
```

### 3.6 Rolling back from int8

Rolling back is a re-embed in the other direction, not just a restart, because the
stored vectors are in int8's space.

```yaml
environment:
  ENGRAPHY_EMBEDDING_PROFILE: onnx-fp32
```

```
docker compose up -d
engraphy-admin reembed --space <space>
```

Then remove the `dedup.t_high` and `dedup.t_low` config rows you set in 3.2, so
the space returns to the code defaults:

```
engraphy-admin config set --space <space> --key dedup.t_high --value 0.95
engraphy-admin config set --space <space> --key dedup.t_low  --value 0.80
```

Restoring the pre-upgrade backup is the faster route if the store is large and the
backup is recent.

## 4. Quick reference

| Move | Data work | Calibration | Rollback |
|---|---|---|---|
| 0.1.0 to 0.2.0 default | none | none | repin the image tag |
| default to `legacy-torch` | none | none | restart |
| default to `onnx-int8` | `reembed`, every space | per host, required | `reembed` back |
