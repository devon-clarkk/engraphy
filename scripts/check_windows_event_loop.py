"""CI guard: the Windows admin-CLI event-loop regression (walkthrough finding 2).

psycopg's async mode refuses Windows' default ProactorEventLoop:

    InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode.

Every async verb in engraphy-admin (`token create` -> auth.mint_token, `import` ->
import_.run_import) goes through asyncio.run() and therefore hits it. That bug
SHIPPED undetected: `token create` was completely dead on Windows, which meant no
bearer token could be minted, which meant no MCP client could be onboarded at
all. engraphy/admin/cli.py fixes it by installing WindowsSelectorEventLoopPolicy at
import, mirroring conftest.py and bench.py.

This is a standalone script rather than a pytest test on purpose: conftest.py's
autouse session fixture provisions a role against a live Postgres, and the point
of this guard is to run on a bare Windows runner with NO database (GitHub's
Windows runners cannot use service containers, and pgvector is not available
there). It needs no DB because the two outcomes are cleanly distinguishable
against an unreachable address:

    ProactorEventLoop (regressed) -> InterfaceError, before any socket work
    SelectorEventLoop  (correct)  -> ConnectionTimeout / connection refused

i.e. reaching a *connection* error proves the async machinery ran correctly.

Exits 0 if the guard passes, 1 with a diagnosis if it regresses. No-ops off
Windows (the bug is Windows-only).
"""
import asyncio
import json
import pathlib
import subprocess
import sys
import tempfile

# Deliberately unreachable: a closed port on loopback, short timeout.
DEAD_DSN = "postgres://nobody:nobody@127.0.0.1:59999/nodb?sslmode=disable&connect_timeout=3"
PROACTOR_MARKER = "ProactorEventLoop"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' -- ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def check_policy_installed() -> None:
    """Importing the CLI must install the selector policy process-wide -- this is
    what protects EVERY async verb, including ones this script does not drive
    end-to-end."""
    import engraphy.admin.cli  # noqa: F401  (import is the thing under test)

    policy = asyncio.get_event_loop_policy()
    check(
        "importing engraphy.admin.cli installs WindowsSelectorEventLoopPolicy",
        isinstance(policy, asyncio.WindowsSelectorEventLoopPolicy),
        type(policy).__name__,
    )


def check_token_create_verb() -> None:
    """Drive the REAL `token create` verb in a subprocess (the way an operator
    runs it). Against a dead DSN it must fail with a connection error; the
    regression signature is the Proactor InterfaceError instead."""
    proc = subprocess.run(
        [sys.executable, "-m", "engraphy.admin.cli", "token", "create",
         "--space", "x", "--principal", "y", "--client-name", "z", "--role", "readwrite"],
        capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "ENGRAPHY_DATABASE_URL": DEAD_DSN},
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    check(
        "`token create` reaches the database layer (not the Proactor error)",
        PROACTOR_MARKER not in out,
        "saw the ProactorEventLoop error" if PROACTOR_MARKER in out
        else "failed on connection, as expected off-DB",
    )


def check_import_async_path() -> None:
    """`import` is the other async verb. Driving the CLI verb end-to-end would
    download the ~523MB embedding model, so instead call run_import's async path
    directly with a stub embedder -- same asyncio.run + psycopg async machinery,
    no model.

    CORROBORATING ONLY, not the detector: AsyncConnectionPool retries connections
    in background tasks and surfaces the failure as PoolTimeout, swallowing the
    InterfaceError a regression would raise -- verified by disabling the fix, at
    which point this check still passed while the other two failed. Detection of
    the regression rests on the policy assertion (which governs `import` too,
    since the policy is process-wide) and on the `token create` verb, which calls
    psycopg.AsyncConnection.connect directly and so surfaces the error verbatim.
    This check is kept because it exercises import's real async path end-to-end
    and would catch a future refactor that moved import off asyncio.run entirely.
    """
    from psycopg_pool import AsyncConnectionPool

    from engraphy.admin.import_ import run_import

    with tempfile.TemporaryDirectory() as td:
        batch = pathlib.Path(td) / "batch.jsonl"
        batch.write_text(
            json.dumps({"type": "note", "title": "guard", "body": "event loop guard"}) + "\n",
            encoding="utf-8",
        )

        async def drive():
            # Short timeout: we only need to reach the connection attempt, and a
            # regression surfaces as InterfaceError immediately either way.
            pool = AsyncConnectionPool(DEAD_DSN, open=False, min_size=1, timeout=5)
            await pool.open()
            try:
                await run_import(
                    pool, "x", "z", "y", batch,
                    embed_document=lambda _t: [0.0] * 384,  # no model download
                )
            finally:
                await pool.close()

        try:
            asyncio.run(drive())
            detail = "unexpectedly succeeded against a dead DSN"
            ok = False
        except Exception as exc:  # noqa: BLE001 -- any failure is fine EXCEPT the Proactor one
            text = f"{type(exc).__name__}: {exc}"
            ok = PROACTOR_MARKER not in text
            detail = text.splitlines()[0][:90]
        check("`import`'s async path reaches the database layer", ok, detail)


def main() -> int:
    if sys.platform != "win32":
        print("not Windows -- the ProactorEventLoop bug is Windows-only; nothing to check.")
        return 0

    print("Windows admin-CLI event-loop guard (walkthrough finding 2):")
    check_policy_installed()
    check_token_create_verb()
    check_import_async_path()

    if failures:
        print(
            "\nREGRESSED: engraphy-admin's async verbs are broken on Windows.\n"
            "engraphy/admin/cli.py must install WindowsSelectorEventLoopPolicy on win32\n"
            "before any asyncio.run() -- see conftest.py / bench.py for the same guard.\n"
            f"Failed: {', '.join(failures)}"
        )
        return 1
    print("\nOK -- async admin verbs run on a compatible event loop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
