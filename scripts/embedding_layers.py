"""Where the Engraphy server process's resident memory actually goes.

    python server_breakdown.py --profile micro

Imports the server's dependency stack one layer at a time in a single fresh
process, reporting RSS after each, so the total is attributed rather than
guessed. Order matches the real boot: interpreter, then the web and database
stack the server needs before it can serve anything, then the embedder, which
is the layer under scrutiny.

Each figure is CUMULATIVE RSS; the delta column is what that layer added on top
of everything before it. Attribution by import order is approximate where two
layers share a transitive dependency (numpy arrives with onnxruntime here, and
would be charged to whichever imported it first), so the deltas are read as
"what adding this layer costs given the ones already loaded", which is the
question an operator deciding what to drop actually has.
"""
import argparse
import pathlib

STATUS = pathlib.Path("/proc/self/status")


def rss_mb() -> float:
    for line in STATUS.read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return -1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="micro")
    args = ap.parse_args()

    stages: list[tuple[str, float]] = []
    prev = rss_mb()
    stages.append(("bare python interpreter", prev))

    def step(label: str) -> None:
        nonlocal prev
        now = rss_mb()
        stages.append((label, now))
        prev = now

    import psycopg  # noqa: F401
    import psycopg_pool  # noqa: F401
    step("psycopg + pool (database client)")

    import starlette.applications  # noqa: F401
    import uvicorn  # noqa: F401
    step("uvicorn + starlette (http server)")

    import mcp.server  # noqa: F401
    step("mcp sdk (the protocol layer)")

    from engraphy.core import embedding
    from engraphy.server import app  # noqa: F401
    step("engraphy's own modules")

    backend = embedding._backend_for(args.profile)
    step("onnxruntime + the graph (embedder load)")

    backend.encode(embedding.document_prefix(args.profile) + "warm the inference session")
    step("warm-up embed (arenas, graph prep)")

    for i in range(50):
        embedding.embed_with(args.profile, f"a node body about deployment {i}")
    step("50 embeds (steady serving state)")

    print(f"profile: {args.profile}   model: {embedding.spec(args.profile).model_id}")
    print(f"{'layer':<40}{'cumulative':>12}{'added':>10}")
    base = stages[0][1]
    for i, (label, value) in enumerate(stages):
        delta = value - (stages[i - 1][1] if i else 0.0)
        print(f"{label:<40}{value:>11.1f}M{delta:>9.1f}M")
    print(f"{'':<40}{'':>12}{'':>10}")
    print(f"embedder share of total: "
          f"{(stages[-1][1] - stages[4][1]) / stages[-1][1] * 100:.0f}%")
    print(f"runtime floor before the embedder: {stages[4][1]:.1f}M "
          f"(of which bare python {base:.1f}M)")


if __name__ == "__main__":
    main()
