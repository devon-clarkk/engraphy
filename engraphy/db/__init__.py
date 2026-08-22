"""Schema/migration assets for the engine.

This file exists so `engram.db` is a real package and setuptools can therefore
ship `migrations/*.sql` as package data (see pyproject.toml's
[tool.setuptools.package-data]). Without it, `pip install .` installed no
migrations at all, and both

    engram/admin/migrate.py::DEFAULT_MIGRATIONS_DIR   (parents[1]/db/migrations)
    engram/server/app.py::_MIGRATIONS_DIR             (parents[1]/db/migrations)

resolved into a site-packages path that did not exist -- `engram-admin migrate`
failed with dbmate's "could not find migrations directory", and the server's
boot-time schema gate had no files to derive its expected version from. The
server only escaped this in practice because `python -m engram.server.app` puts
the working directory on sys.path; the `engram-admin` console script does not.
"""
