"""CI guard: no `embed(`/`embed_document(`/`embed_query(` call anywhere inside
an `async with transaction(...)` block (design/implementation/dedup-write-path-
plan.md trap 3 -- embedding inside the lock would serialize every write on the
~20-80ms model call).

AST-based rather than grep: a raw grep for the literal text "embed(" would
false-positive on this very docstring, and on any comment mentioning the
trap. Walking the parsed tree for real Call nodes avoids that entirely.

Matches any callable whose name starts with "embed" (`embed`, `embed_document`,
`embed_query`, and any future embed wrapper), not just the bare `embed` name --
the asymmetric-prefix decision (QUESTIONS.md embedding-task-prefix) split the
single `embed()` primitive into `embed_document()`/`embed_query()` wrappers,
and a literal `== "embed"` check silently stopped catching real trap-3
violations once callers switched to the wrappers.
"""
import ast
import pathlib
import sys


def _is_transaction_call(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id == "transaction") or (
        isinstance(func, ast.Attribute) and func.attr == "transaction"
    )


def _is_embed_call(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id.startswith("embed")) or (
        isinstance(func, ast.Attribute) and func.attr.startswith("embed")
    )


class Visitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.violations: list[tuple[str, int]] = []

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            call = item.context_expr
            if isinstance(call, ast.Call) and _is_transaction_call(call.func):
                for stmt in node.body:
                    for n in ast.walk(stmt):
                        if isinstance(n, ast.Call) and _is_embed_call(n.func):
                            self.violations.append((self.filename, n.lineno))
        self.generic_visit(node)


def main() -> int:
    violations: list[tuple[str, int]] = []
    for path in pathlib.Path("engraphy").rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = Visitor(str(path))
        visitor.visit(tree)
        violations.extend(visitor.violations)

    if violations:
        for filename, lineno in violations:
            print(f"{filename}:{lineno}: embed() called inside a transaction() block", file=sys.stderr)
        return 1
    print("OK: no embed() call inside any transaction() block")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
