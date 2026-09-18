# Contributing

Thanks for taking an interest in Engraphy. Issues and pull requests are welcome.

Engraphy is maintained by one person, so the most useful contribution is a small,
self-contained change with a test that fails without it. If you are planning
something larger, open an issue first and we can agree the shape before you write
it.

## Getting set up

You need **Postgres 16 with pgvector** and **Python 3.12**. The quickest database
is the image CI uses:

```bash
docker run -d --name engraphy-dev -p 5432:5432 \
  -e POSTGRES_PASSWORD=engraphy -e POSTGRES_DB=engraphy_test \
  pgvector/pgvector:pg16
```

Then install the package with its development extra, apply the migrations, and
run the suite:

```bash
pip install -e '.[dev]'
export ENGRAPHY_TEST_DATABASE_URL='postgres://postgres:engraphy@localhost:5432/engraphy_test?sslmode=disable'
dbmate --migrations-dir engraphy/db/migrations --url "$ENGRAPHY_TEST_DATABASE_URL" up
pytest -q
```

The tests run against a live database on purpose. Row-Level Security, the schema
enforcement triggers, and the dedup write path are all enforced in Postgres, so a
mocked database would test the mock rather than the thing that ships.

The first run downloads the embedding model (`nomic-ai/nomic-embed-text-v1.5`,
about 523 MB) and caches it.

To run the LoCoMo benchmark rather than the test suite, follow
[`bench/RUN-LOCOMO.md`](bench/RUN-LOCOMO.md). It needs an OpenAI-compatible base
URL and key on top of the database above, and `python -m bench.smoke_openai`
checks the endpoint before you commit to a full run.

## What CI checks

Every push to `main` and every pull request runs [`ci.yml`](.github/workflows/ci.yml).
Three jobs gate the merge and all three must pass:

- **`test`** runs `ruff check .`, the migrations from an empty database, the
  pytest suite with coverage, a 100% branch-coverage floor on
  `engraphy/core/attr_spec.py`, and the repository's grep guards.
- **`deploy-smoke`** builds the shipped images and walks
  [`deploy/checklist.md`](deploy/checklist.md) end to end: compose up, migrate and
  provision through the admin sidecar, mint a token, drive a real MCP client over
  HTTP, and run a backup and restore drill.
- **`windows-cli`** proves the async admin verbs select an event loop psycopg can
  use on Windows.

A fourth job, **`perf-budgets`**, measures the performance budgets in
`engraphy/tests/bench.py` at 10k seeded nodes and writes the table into the run
summary. It is advisory: it runs on every push and pull request, and it never
gates. GitHub's shared runners contend hard enough to move every measured
operation at once, so a breach there reports the runner rather than the code.
Read it for the trend and treat a real regression as something to reproduce
locally.

Run `ruff check .` and `pytest -q` before you push and most surprises disappear.
The linter version is pinned in `pyproject.toml`, so use the pinned one.

## House style

- Match the surrounding code. The codebase comments the *reasoning* behind a
  decision, not the mechanics of the line below it. If a choice took thought,
  record why.
- Australian English, and no em dashes.
- Keep commits focused, and write a message that explains the reasoning rather
  than restating the diff.
- New behaviour needs a test. Changed behaviour needs the test that proves the
  change.
- If you touch the tool surface, update
  [`docs/04-tools-reference.md`](docs/04-tools-reference.md) in the same commit.

## Design documentation

[`design/`](design/README.md) holds the data model, retrieval and dedup, auth and
tenancy, operations, the pack system, and the benchmark harness. It is the
reasoning behind the implementation and the best place to start on anything
structural.

## Licensing of contributions

Engraphy is licensed under the [Business Source License 1.1](LICENSE), converting
to Apache-2.0 on the Change Date. By opening a pull request you agree that your
contribution is licensed on those same terms.

## Security

Please do not open a public issue for a security problem. Report it privately
through [GitHub's security advisory form](https://github.com/devon-clarkk/engraphy/security/advisories/new).
