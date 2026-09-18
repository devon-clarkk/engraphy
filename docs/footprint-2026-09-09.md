# Engraphy resident footprint, measured 2026-09-09

For deciding what to hand a coworker on an 8GB laptop. Every figure below was
measured on one Linux x86-64 host (Docker Desktop on Windows 11, WSL2 backend,
Intel i5-11600K) against `main` at `72b6052`, unless it names another source.

**The short answer.** A 250MB budget for the Engraphy stack is achievable, and
the configuration that gets there is the `micro` embedder plus a Postgres tuned
for a laptop rather than for a database server. Measured working footprint:
**187MB**, against a stock-configuration **983MB**.

The budget is achievable for the *stack*. It is not achievable for *Engraphy on
a Windows laptop through Docker Desktop*, because Docker Desktop's own Windows
processes measured 730MB on this host before any container started. Section 6
covers what to do about that.

## 1. How these numbers were taken

Container figures are read from the kernel cgroup, not from `docker stats`:

    anon    private and unreclaimable
    shmem   shared memory, where Postgres keeps shared_buffers
    file    page cache, reclaimable, and NOT counted in any headline here

The headline is `anon + shmem`: what the kernel cannot reclaim without
swapping, which is what a laptop with a browser open actually loses.
`scripts/footprint.py` does the reading, through a throwaway privileged
container, so measuring a stack never starts a process inside it.

`docker stats` subtracts only `inactive_file`, so it reports a larger number
that includes some page cache. Both views appear below where they differ,
because the ~1.05GB figure from the rollout review is the `docker stats` one and
it should stay reconcilable.

Every stack figure is taken **after a workload**, not at idle. An idle Postgres
has not faulted in `shared_buffers`, has not touched the HNSW index, and has not
opened the connections the pool will hold. Those three arrive the moment
somebody searches, and the difference is not small: Postgres measured 29MB idle
and 111MB after 20,000 nodes and 700 searches. The second number is the one a
coworker lives with.

## 2. Where the current ~1GB goes

Devon's running instance, measured read-only:

| component | anon | shmem | resident |
|---|---:|---:|---:|
| server process | 905.6 MB | 0.0 | **905.6 MB** |
| Postgres | 28.8 MB | 21.7 MB | **50.5 MB** |
| **total** | | | **956.1 MB** |

`docker stats` puts the same two containers at 1.031GiB and 64.8MiB, which is
the ~1.05GB the rollout review recorded. The difference is page cache.

**The server is 95% of it, and Postgres is not the problem.** That is the single
most useful fact in this report, because it says tuning the database is a
second-order move and the embedder is the whole game.

One thing to know before comparing against anything else here: **that instance
predates the 0.2.0 embedder work.** Its image was built 2026-08-31, installs
`requirements-cpu.txt`, and therefore carries torch 2.13.0. Current `main` has
no torch on any default path. The comparison that matters is not "live against
tuned" but "stock `main` against tuned `main`", and stock `main` is section 3.

## 3. Stack configurations, measured

20,000-node store, 700 searches, then measured. 20,000 is a deliberate
overestimate: the design's own performance budgets are set at 10,000, so this
keeps every Postgres figure a ceiling.

| # | configuration | server | Postgres | total |
|---|---|---:|---:|---:|
| A | live instance today (torch-era image) | 905.6 | 50.5 | 956.1 |
| B | `main`, default profile (`onnx-fp32`) | 871.9 | 110.8 | **982.7** |
| C | `main`, `micro` profile, stock Postgres | 131.9 | 110.9 | 242.7 |
| D | `micro` + tuned Postgres | 132.2 | 46.0 | **178.2** |
| E | D + lazy model load, idle, never searched | 48.8 | 45.8 | 94.6 |
| F | D + lazy, after real MCP traffic | 133.0 | 54.4 | **187.4** |

Row F is the number to quote. It is the only row driven through the shipped
Streamable HTTP path with real writes and searches rather than SQL standing in
for them, so every allocation the MCP layer, the auth path and the pool make per
request is inside it.

Row B is higher than row A because the default ONNX profile loads a
full-precision graph, and because row B's Postgres has done work that row A's
had not. Neither is a regression: the two rows differ in three variables at once
and are listed together only to reconcile against the review's figure.

**The move from B to F is 983MB to 187MB, an 81% reduction.**

## 4. Where the server's memory goes

Staged imports in one fresh process, cumulative RSS, `micro` profile:

| layer | added |
|---|---:|
| bare Python interpreter | 11.5 MB |
| psycopg + pool | 22.8 MB |
| uvicorn + starlette | 8.7 MB |
| MCP SDK | 22.5 MB |
| Engraphy's own modules | 0.4 MB |
| **runtime floor before any embedder** | **65.9 MB** |
| onnxruntime + the gte-small graph | 111.6 MB |
| warm-up embed and steady serving | 1.3 MB |

Taken a second way, isolating the runtime from the model:

| | resident |
|---|---:|
| Python + numpy + onnxruntime, no graph loaded | 43.5 MB |
| the gte-small int8 session on top of that | +98.7 MB |

**A 34MB int8 graph costs 99MB resident.** ONNX Runtime expands it roughly
threefold in graph structures, activation buffers and allocator pools. That
ratio, not the file size, is what governs how much a smaller model would save.

## 5. The encoder, and what was rejected

`micro` (gte-small int8, `Xenova/gte-small`) is where the encoder work lands:
**112MB of the server process**, against 872MB for the shipped `onnx-fp32`
default. Its calibration and its 6% recall cost are covered in
[micro-reembed.md](micro-reembed.md).

Two ways further down were measured and not taken.

**ONNX Runtime's CPU arena allocator, rejected on the measurement.** Disabling
it moves the embedder from 146.4MB to 145.7MB and costs 9% on latency
(5.44 to 5.93 ms/embed). Mapping the model bytes directly rather than copying
them changes nothing measurable. The arena is not where the memory is, so the
knob that looks like it should help does not.

| variant | steady | ms/embed |
|---|---:|---:|
| default | 146.4 MB | 5.44 |
| `enable_cpu_mem_arena = False` | 145.7 MB | 5.93 |
| the above plus direct model bytes | 146.1 MB | 4.75 |

**A model smaller than gte-small, not pursued, and the arithmetic says why.**
The earlier tier investigation measured all-MiniLM-L6-v2 int8 at a 23MB graph
against gte-small's 34MB, for 19.9% less recall. Scaling by the 3x expansion
above, that buys roughly 30MB of a 187MB total and costs a fifth of retrieval
accuracy. There is a 44MB runtime floor underneath any of them that no model
choice touches. With the target already met at 187MB, spending 20% of recall to
reach ~157MB is not a trade worth offering.

## 6. Docker Desktop is the larger term on Windows

Measured on this host, outside every container:

| | working set |
|---|---:|
| Docker Desktop's Windows-side processes (7 of them) | **730 MB** |
| WSL helper processes (18 of them) | 5 MB |
| `vmmemWSL`, the WSL2 VM | 3,068 MB |

**Docker Desktop's own processes cost about four times the entire tuned
Engraphy stack.** That figure does not scale with host RAM, so a coworker's
laptop would see something close to it.

The 3,068MB VM figure is **not** attributable to Engraphy and must not be quoted
as though it were. This host was running six containers and had `.wslconfig` set
to `memory=6GB`; the VM sizes itself against the host and holds page cache for
everything inside it. A coworker's figure is unmeasured here. WSL2 with no
`.wslconfig` ceilings at the lower of half of RAM or 8GB, so an 8GB laptop
allows up to ~4GB, and `autoMemoryReclaim` returns some of it when idle.

The honest reading: **on Windows, the container runtime costs more than the
thing it is running.** No amount of embedder work changes that, because it is
not Engraphy's memory.

## 7. Verdict against the 250MB target

**Achievable, for the stack.** 187.4MB measured through the shipped path, 63MB
inside budget, on a store twice the size the performance budgets assume.

**The realistic floor is about 110MB**, and it is not worth chasing:

| term | floor | why it does not go lower |
|---|---:|---|
| Python + numpy + onnxruntime | 43.5 MB | the runtime, before any model |
| psycopg + uvicorn + starlette + MCP SDK | 22.4 MB | the server has to speak the protocol and reach the database |
| the smallest graph with acceptable recall | ~99 MB | a 34MB int8 graph expands ~3x in ORT |
| Postgres with `shared_buffers=32MB` | ~46 MB | lower values were not measured; 32MB is a small share of the total |

That sums past 187MB because the layers share transitive dependencies; the
measured total is the authority and the column is for seeing which term to
attack. The only term with real room left is the ONNX session, and the way to
shrink it is a smaller model, which costs recall.

**Not achievable as "total running memory on a Windows laptop", if that laptop
runs Docker Desktop.** 187MB of containers plus ~730MB of Docker Desktop is
~917MB before the WSL2 VM, and the VM is the larger and less predictable term.

## 8. Recommended profile for an 8GB laptop

**Run the `micro` profile with the tuned Postgres, and do not ship Docker
Desktop to get there.**

    docker compose -f compose.yaml -f compose.micro.yaml -f compose.small.yaml up -d

That is rows D and F: 178MB after a SQL workload, 187MB with a live MCP client
connected and the pool's backends open. **Quote 187MB**, because a deployment
nobody is connected to is not the case worth budgeting for. Row F additionally
carries the lazy overlay, which by then has already loaded the model and so
costs nothing either way. It is the right configuration whichever way the stack
is delivered. Adopting `micro` on an existing store is a mandatory full
re-embed, covered in [micro-reembed.md](micro-reembed.md).

**On delivery, the no-Docker path is the recommendation for these machines.**
`docs/02-setup.md` already documents it: the Python server installed directly
and a Postgres installed as a service. The container figures in this report are
process figures, so they carry over unchanged, and the ~730MB of Docker Desktop
plus the WSL2 VM simply is not there. That is a larger saving than every
embedder change in this report combined.

**Lazy model load is available and is not the default.** `compose.lazy.yaml`
sets `ENGRAPHY_EMBEDDING_LAZY_LOAD=1`, which takes an instance nobody has
searched from 132MB to 49MB. Two things to understand before using it:

- **The saving ends at the first search** and does not return within the
  process. It lowers what an idle instance holds, not what a working one holds.
  Row E is 94.6MB; the same instance after one search is row F at 187.4MB.
- **`/healthz` starts answering 200 before the server can serve a search**, so
  the health signal degrades from readiness to liveness. The first search of the
  process pays about 400ms to load the model, against 1.8ms afterwards.

Worth it for an instance installed and searched a few times a day. Not worth it
on a server, and not a way to fit a working instance into a budget it does not
otherwise fit.

## 9. Reproducing this

    # bring a stack up, carrying the SAME -f set on every compose command
    docker compose -f compose.yaml -f compose.micro.yaml -f compose.small.yaml up -d

    # give it a store and some searches, then measure
    docker compose -f compose.yaml -f compose.micro.yaml -f compose.small.yaml       --profile admin run --rm -T admin psql "$DSN" -q -f - < scripts/footprint_workload.sql
    python scripts/footprint.py --project <compose-project> --label "..."

    # attribution and latency
    python scripts/embedding_layers.py --profile micro
    docker compose ... run --rm -T admin psql "$DSN" -q -f - < scripts/footprint_latency.sql

Every compose command has to carry the same `-f` set, including `run`. Compose
resolves a service from the files it is given, so a `run` that omits an overlay
recreates the dependency it starts from the base file: that is how a tuned
Postgres reverts to stock in the middle of a measurement, which happened once
while taking these numbers and is why the settings are asserted from
`pg_settings` in the workload script rather than assumed.

## 10. What is not measured here

- **A coworker's actual laptop.** Everything is from one 15.8GB Windows host
  with other containers running. The container figures should carry; the WSL2
  figure will not.
- **The no-Docker path's Postgres.** Section 8 recommends it on the strength of
  removing Docker Desktop, which is measured, and assumes a native Postgres
  behaves like the containerised one, which is not.
- **Recall under the tuned Postgres.** Tuning `shared_buffers` changes cache
  behaviour, not results, and the vector leg measured 0.15 ms/query tuned
  against 0.21 ms/query stock. The lexical leg measured between 76 and 171
  ms/query across runs on this contended host, which is too noisy to read
  anything into, and its absolute value is an artifact of a synthetic corpus
  where every row matches the query.
