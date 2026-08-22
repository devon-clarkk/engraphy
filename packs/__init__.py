"""Shipped pack assets: the pack-file JSON Schema and the starter pack.

This file exists so `packs` is a real package and setuptools can ship
`schema.json` and `starter/pack.yaml` as package data (see pyproject.toml's
[tool.setuptools.package-data]). Without it, `pip install .` installed neither,
and `engraphy/admin/packs.py::SCHEMA_PATH` (parents[2]/packs/schema.json)
resolved to a site-packages path that did not exist -- `engraphy-admin pack
validate|apply` failed with
`FileNotFoundError: .../site-packages/packs/schema.json`, and an operator who
installed from a wheel had no starter pack to apply either.

Nothing imports this package; it is a data container. It is deliberately kept
at the repo root rather than moved under `engraphy/` so that the existing
`parents[2]` resolution keeps working unchanged and the source layout is
undisturbed -- note that design/01 describes this content as living at
`engraphy/packs/...`, so consolidating it under the `engraphy` package later would
both match that doc and avoid shipping a top-level `packs` name. That is a
layout decision for the design fold-back, not a packaging bug.
"""
