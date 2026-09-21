# Moving memories between engines

This runbook moves a space's memories from one Engraphy engine to another: for
example, from a local engine on a Windows machine to a server engine on a Mac.
Two `engraphy-admin` verbs do the work:

- `space export` writes a space, or some of its scopes, to a JSONL bundle. It is
  read-only: its database connection refuses every write.
- `space import` replays a bundle into a space on the destination engine, under
  a principal of that engine. Every node goes through the engine's write
  pipeline and every edge through `link`, so the destination's space, principal,
  scope permissions, attr specs, embedding model and deduplication all apply.

Both verbs run on the operator connection (`ENGRAPHY_DATABASE_URL`, the
superuser URL in each stack's `.env`). Neither uses an account token: bulk
import is an operator verb and is never reachable through an agent's token. The
bundle holds your memory text in plain JSON. It goes from your machine to your
own server, and nothing leaves your control.

## What moves

| Moves | Detail |
|---|---|
| Scopes | Display name, visibility, ambient flag, hints, description and archived flag. |
| Nodes | Type, scope, title, body, attrs (including `addenda` and `dropped`), status (`active`, `superseded`, `archived`), and session id. |
| Edges | Every edge whose two endpoints are exported, remapped to the destination's node ids. |

A node with status `merged` is not written. Its content already lives on its
canonical node, and its edges move to that canonical.

These do not move: tokens, principals' credentials, parked duplicate checks,
inbox items, the audit log, usage metrics, recall counts and `created_at`
timestamps. Imported nodes carry the time of the import. Node ids change; the
import maps them.

## Before you start

On the destination engine:

1. The destination space exists, and your principal is a member of it. To create
   both: `engraphy-admin space create --id devon --display-name "Devon" --principal devon`.
2. The destination space has the same pack as the source space, so every node
   type and edge rule the bundle needs is registered:
   `engraphy-admin pack apply packs/conversational/pack.yaml --space devon`.
   The import refuses a bundle that uses a node type the destination does not
   register, before it writes anything. An edge whose type the destination does
   not permit between its two node types is skipped and counted.
3. The import runs with the same `ENGRAPHY_EMBEDDING_PROFILE` as the server. The
   import embeds each node with the admin container's model, and the server
   searches with its own. `/healthz` reports the server's `embedding_model`. Run
   the `admin` service with the same compose files as the server (for example
   `-f compose.yaml -f compose.micro.yaml`, which moves both onto `micro`), or
   pass `-e ENGRAPHY_EMBEDDING_PROFILE=<profile>`.

Both engines run an Engraphy version that has `space export` and `space import`.

## 1. Export on the source machine

The export needs the source database. With the Postgres port published on the
host, run it from any environment that has this Engraphy version installed:

```powershell
$env:ENGRAPHY_DATABASE_URL = "postgres://postgres:<POSTGRES_PASSWORD>@127.0.0.1:<port>/<database>?sslmode=disable"
engraphy-admin space export --space devon --out devon.jsonl
```

When the port is not published, run the admin image on the stack's own network.
`docker network ls` lists it; for the compose project `engram` it is
`engram_default`, and the database service is `postgres`. Run these from a
checkout of this Engraphy version:

```powershell
docker build --target admin -t engraphy-admin:local .
docker run --rm --network engram_default -v "${PWD}:/out" -e ENGRAPHY_DATABASE_URL="postgres://postgres:<POSTGRES_PASSWORD>@postgres:5432/<database>?sslmode=disable" engraphy-admin:local engraphy-admin space export --space devon --out /out/devon.jsonl
```

Use the database name your stack sets in `POSTGRES_DB`.

To export some scopes only, repeat `--scope`:

```powershell
engraphy-admin space export --space devon --scope proj-iris --scope proj-engraphy --out projects.jsonl
```

The export prints its counts: scopes, node types, nodes and edges. Keep them for
step 5. The export changes nothing in the source engine, so it is safe to run
while the engine is in use.

## 2. Move the bundle

Copy the file to the Mac by a channel you control: `scp`, AirDrop or a USB
drive. Put it in a directory of its own. Delete the copies you do not need after
the import.

## 3. Import on the destination machine

Run the import through the stack's `admin` service, which holds the operator
connection and the embedding model. Run the commands from the stack's directory,
with `BUNDLE_DIR` set to the directory that holds the bundle. The import writes
its review CSV beside the bundle. Add the same `-f` files the server uses.

First a dry run, which checks everything and writes nothing:

```bash
docker compose --profile admin run --rm -v "$BUNDLE_DIR:/in" admin engraphy-admin space import /in/devon.jsonl --space devon --principal devon --dry-run
```

The dry run prints the scope map and the scopes it would create. The mapping
rules:

- A source scope maps to the destination scope with the same id.
- `personal-<source owner>` maps to `personal-<principal>`.
- `--scope-map SRC=DST` overrides either rule. Repeat it for several scopes.
- A destination scope that does not exist is created with the source scope's
  display name, visibility, ambient flag, hints and description, and is owned
  by the importing principal. `--no-create-scopes` refuses instead.

Then run the import:

```bash
docker compose --profile admin run --rm -v "$BUNDLE_DIR:/in" admin engraphy-admin space import /in/devon.jsonl --space devon --principal devon
```

The summary reports each node's outcome and each edge's:

- **inserted**: a new node.
- **already present**: the destination held a node with the same type, scope,
  title and body, and the import used it.
- **merged** or **merge-linked**: the write pipeline matched the node to
  destination content, the same way it matches an agent's write.
- **resolved distinct**: the node was close to another node in the same bundle.
  The source held both, so both are kept.
- **left for review**: the node was close to content the destination held
  before the import. It is parked as a duplicate check, and the review CSV
  beside the bundle lists it.
- **skipped in an archived scope**: the destination scope is archived, so it
  takes no new writes. A node already present there is matched.

## 4. Resolve parked nodes and re-run

A parked node has no id yet, so its edges wait. Resolve each parked node with
your agent (`pending_list`, then `resolve_duplicate` with `distinct` or `merge`),
then run the same import command again. The second run writes nothing it wrote
before: every node matches the node it created, every edge is already present,
and the edges of the nodes you resolved are attached.

## 5. Check the result

- Compare the summary's node and edge counts with the export's counts. The
  difference is the merged-status nodes and the skipped edges, which the
  summary itemises.
- Run `engraphy-admin doctor --space devon` on the destination.
- Search for a few memories you know well, through your agent.

Your local engine keeps its memories unchanged. When you have confirmed the
server holds them, point your agents at the server.
