"""Resident memory and warm latency for one embedding profile, in one process.

Run it once per profile, each in a FRESH subprocess, because the thing being
measured is partly the import graph and a process that has already imported a
different backend cannot measure the next one honestly:

    for p in onnx-fp32 onnx-int8 micro; do
      python scripts/embedding_memprobe.py --profile $p
    done

Four stages are printed, and the last is the one to quote. `imports` is the
framework floor before any weights are read. `loaded` is after the session is
constructed. `warm` is after `load_model()`'s warm-up embed, which is where an
ONNX session actually allocates its arenas. `steady` is after a short embed loop,
which is what a serving process looks like once it has done some work.

Two rules this exists to enforce, both learned by getting them wrong:

* **One probe for every arm.** A figure taken with one harness and compared
  against a figure taken with another measures the harnesses. The earlier
  investigation carried a 277MB number for one profile and a 143MB number for
  another that had been measured through different import stacks, and the
  difference between the stacks was roughly 28MB of the gap.
* **Go through the real seam.** `embedding.embed_with` is the production path.
  A probe that constructs its own session with a convenience wrapper measures the
  wrapper, and the wrapper is not what ships.

Reads RSS from /proc, so it reports real numbers on Linux, which is the
deployment target. Elsewhere it falls back to psutil if it is installed.
"""
import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_STATUS = pathlib.Path("/proc/self/status")


def rss_mb() -> float:
    if _STATUS.exists():
        for line in _STATUS.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except ImportError:
        sys.exit("no /proc and no psutil: run this on Linux, or pip install psutil")


def peak_mb() -> float | None:
    if _STATUS.exists():
        for line in _STATUS.read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    return None


TEXT = ("Deploy failed: migration not run\n"
        "The deploy failed because the backfill migration was never executed "
        "before switching the mapper.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--iters", type=int, default=32)
    args = ap.parse_args()

    bare = rss_mb()
    from engraphy.core import embedding
    spec = embedding.spec(args.profile)
    imports = rss_mb()

    backend = embedding._backend_for(args.profile)
    loaded = rss_mb()

    # The warm-up the serving path performs at boot, through the same prefix the
    # profile's model expects.
    backend.encode(embedding.document_prefix(args.profile) + "warm the inference session")
    warm = rss_mb()

    t0 = time.perf_counter()
    for i in range(args.iters):
        embedding.embed_with(args.profile, f"{TEXT} run {i}")
    ms = (time.perf_counter() - t0) / args.iters * 1000
    steady = rss_mb()

    peak = peak_mb()
    print(f"profile      {args.profile}")
    print(f"model        {spec.model_id} @ {spec.revision[:12]}")
    print(f"graph        {spec.graph or 'sentence-transformers'}")
    print(f"bare         {bare:8.1f} MB")
    print(f"imports      {imports:8.1f} MB")
    print(f"loaded       {loaded:8.1f} MB")
    print(f"warm         {warm:8.1f} MB")
    print(f"steady       {steady:8.1f} MB   <- quote this one")
    if peak is not None:
        print(f"peak         {peak:8.1f} MB")
    print(f"ms/embed     {ms:8.1f}")


if __name__ == "__main__":
    main()
