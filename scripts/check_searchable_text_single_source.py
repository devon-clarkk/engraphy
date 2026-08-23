"""CI guard (fact-searchability-phase-c.md §2.4): no `embed_document(...)` call
site outside engraphy/core/embedding.py may build its argument by concatenating
`title + "\\n" + body` on its own. Stored-node embeddings must go through
`embedding.searchable_text(title, body, extra)` so the embedding and the tsvector
weight-C leg read ONE render (nodes.extra_search). A site that re-concatenates
title+body inline is embedding out of the searchable surface -- the exact Phase-C
regression.

AST-based (like check_no_embed_in_transaction.py): the banned shape is an
`embed_document(<expr>)` whose argument expression contains a string constant with
a newline joined by `+` -- i.e. `a + "\\n" + b`. `embedding.searchable_text(...)`
builds that join INSIDE embedding.py (exempt), and every legitimate caller either
passes a `searchable_text(...)` call or a variable holding its output (both have
no inline "\\n"-BinOp), so they pass. embed_query is not checked: it embeds a
search query, not a stored node.
"""
import ast
import pathlib
import sys

EXEMPT = {pathlib.Path("engraphy/core/embedding.py")}


def _is_embed_document(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id == "embed_document") or (
        isinstance(func, ast.Attribute) and func.attr == "embed_document"
    )


def _has_newline_concat(node: ast.expr) -> bool:
    """True if `node` is (or contains) a `+` BinOp whose operands include a string
    constant containing a newline -- the `x + "\\n" + y` embedding shape."""
    for n in ast.walk(node):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            for side in (n.left, n.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, str) and "\n" in side.value:
                    return True
    return False


def main() -> int:
    violations: list[tuple[str, int]] = []
    for path in pathlib.Path("engraphy").rglob("*.py"):
        if "tests" in path.parts or path in EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and _is_embed_document(n.func) and n.args
                    and _has_newline_concat(n.args[0])):
                violations.append((str(path), n.lineno))

    if violations:
        for filename, lineno in violations:
            print(f"{filename}:{lineno}: embed_document() concatenates title+body inline; "
                  f"pass embedding.searchable_text(title, body, extra) (Phase C §2.4)",
                  file=sys.stderr)
        return 1
    print("OK: every embed_document() call site builds through searchable_text()")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
