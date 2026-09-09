# PyInstaller spec for engraphy-win.exe, the shipped Windows server binary.
#
# ONEDIR, NOT ONEFILE, and the reason is not size. A onefile build unpacks its
# whole payload to a temporary directory on every start, and this payload
# includes ONNX Runtime and a 34MB model graph. Inside an installer a single
# file buys nothing (the user sees an installer either way) and costs a
# multi-second delay every time the logon task starts the process, plus a
# %TEMP% directory that antivirus gets to inspect on each run.
#
# Run from the repository root:
#
#   pyinstaller --clean --noconfirm deploy/windows/engraphy-win.spec
#
# The build has to happen on Windows with Python 3.12, which is why it lives in
# .github/workflows/windows-dist.yml rather than in a developer's instructions:
# the artifact anyone installs is built by CI, from a tagged commit, once.

import pathlib

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

ROOT = pathlib.Path(SPECPATH).parent.parent

datas = []

# Package data that engraphy resolves relative to its own modules. pyproject
# ships these as package-data for exactly the same reason; a frozen build does
# not go through setuptools, so the list is repeated here rather than inferred.
#   engraphy/admin/migrate.py -> parents[1]/db/migrations
#   engraphy/admin/packs.py   -> parents[2]/packs/schema.json
datas += [(str(ROOT / "engraphy" / "db" / "migrations" / "*.sql"), "engraphy/db/migrations")]
datas += [(str(ROOT / "packs" / "schema.json"), "packs")]
datas += [(str(ROOT / "packs" / "starter" / "pack.yaml"), "packs/starter")]

# The app-role SQL is run through the bundled psql by engraphy_win._provision_app_role.
datas += [(str(ROOT / "deploy" / "provision-app-role.sql"), "deploy")]

# jsonschema resolves format checkers and version classes through entry points,
# which means it reads its own distribution metadata at import time. Without the
# metadata a frozen build imports and then fails on the first pack validation.
datas += copy_metadata("jsonschema")
datas += copy_metadata("jsonschema_specifications")
datas += collect_data_files("jsonschema_specifications")

hiddenimports = [
    # uvicorn picks its event loop, HTTP protocol and logging config by string
    # at runtime, so nothing statically imports them and the analysis cannot see
    # them. A build missing these starts and then fails to serve.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # psycopg selects its libpq binding at import time. The binary wheel's
    # `psycopg_binary` is what carries libpq itself.
    "psycopg_binary",
    "psycopg_pool",
    # anyio's backend is likewise chosen by name.
    "anyio._backends._asyncio",
]

a = Analysis(
    [str(ROOT / "deploy" / "windows" / "engraphy_win.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    # The two heaviest things a frozen build picks up by accident. torch is the
    # legacy-torch profile's dependency and is never imported by the shipped
    # profile; tkinter comes in through anything that touches matplotlib-adjacent
    # code. Excluding them is worth roughly a gigabyte.
    excludes=["torch", "sentence_transformers", "transformers", "tkinter",
              "matplotlib", "IPython", "pytest", "hypothesis"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="engraphy-win",
    debug=False,
    strip=False,
    upx=False,
    # A CONSOLE build, and the Scheduled Task runs it hidden. The console is not
    # for the user to look at: it is what makes Windows deliver CTRL_LOGOFF and
    # CTRL_SHUTDOWN to the process, which is how the cluster gets a clean
    # shutdown at the end of a session (see engraphy_win._install_console_handler).
    # A windowed build would silently lose that.
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="engraphy-win",
)
