"""CI guard: the engine must never import the benchmark harness.

design/09 §Decision record: the harness lives outside the engine package. The
harness needs LLM clients and pulls large volumes of content out of the store --
both forbidden in the engine by IMPLEMENTER.md rule 4. That separation is only
worth anything if it is mechanically enforced, because the failure mode is
gradual: one `from bench.core...` import for a "shared" helper, and the engine
has an `anthropic` dependency in its transitive closure with nobody noticing.

AST-based rather than grep, following the precedent of
`check_no_embed_in_transaction.py`: a grep for "bench" matches
`engraphy/tests/bench.py` (the unrelated latency-budget script), every docstring
that says "benchmark", and nothing useful. The AST sees imports only.

Exits non-zero, naming file and line, on the first violation.
"""

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE = REPO_ROOT / "engraphy"
FORBIDDEN_ROOT = "bench"


def violations_in(path: pathlib.Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # a syntax error is someone else's test to fail
        print(f"warning: could not parse {path}: {exc}", file=sys.stderr)
        return []

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_bench(alias.name):
                    found.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has module None; a relative import can never
            # reach `bench` from inside `engraphy`, so only absolute ones matter.
            if node.level == 0 and node.module and _is_bench(node.module):
                found.append((node.lineno, f"from {node.module} import ..."))
    return found


def _is_bench(dotted: str) -> bool:
    return dotted == FORBIDDEN_ROOT or dotted.startswith(FORBIDDEN_ROOT + ".")


def main() -> int:
    if not ENGINE.is_dir():
        print(f"guard error: {ENGINE} does not exist", file=sys.stderr)
        return 2

    failures = 0
    for path in sorted(ENGINE.rglob("*.py")):
        for lineno, what in violations_in(path):
            rel = path.relative_to(REPO_ROOT).as_posix()
            print(
                f"{rel}:{lineno}: engine code imports the benchmark harness ({what}). "
                "The dependency arrow points one way: bench imports engraphy, never the "
                "reverse. See design/09.",
                file=sys.stderr,
            )
            failures += 1

    if failures:
        print(f"\n{failures} forbidden import(s) found.", file=sys.stderr)
        return 1
    print(f"ok: no engraphy/** module imports {FORBIDDEN_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
