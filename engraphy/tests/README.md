# Engraphy test suite

Fixtures are **spec** (design/07 §Golden fixtures): written before code, never weakened
to pass. Starter sets are committed; files state their required final counts.
`dedup_cases.yaml` similarities are baselined by `scripts/baseline_dedup_fixtures.py`
once the pinned embedding model lands (E1) — expected *bands* are design intent and fixed now.

Layout: fixtures/ (golden data) · test modules named per design/01–07 test tables.
Every test module's docstring cites the design table rows it implements.
