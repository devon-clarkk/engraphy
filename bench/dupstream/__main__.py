"""Driver: provision, run both arms across seeds, score, emit a manifest.

    python -m bench.dupstream                       # AlwaysDistinct, 3 seeds
    python -m bench.dupstream --seeds 0             # one seed, faster

Needs a Postgres with the Engraphy schema (ENGRAPHY_TEST_DATABASE_URL). Needs no
API credential: dupstream is scored against generated labels, so the whole
benchmark is LLM-free unless --policy llm_adjudicate is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import uuid
from datetime import datetime, UTC

import psycopg
from psycopg_pool import AsyncConnectionPool

from engraphy.core import embedding

from bench.core.space import provision_run_space, space_id_for
from bench.dupstream.generate import FIXTURE, generate, read_jsonl, write_jsonl
from bench.dupstream.run import probe, run_arm, score

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DB = os.environ.get(
    "ENGRAPHY_TEST_DATABASE_URL",
    "postgres://postgres:engram@localhost:5432/engram_dev?sslmode=disable",
)
APP_DB = DB.replace("postgres:engram@", "engram_app:engram_app_test_only@")
RUNS = pathlib.Path(__file__).resolve().parents[2] / "runs"


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _teardown(space_id: str) -> None:
    with psycopg.connect(DB, autocommit=True) as c:
        cur = c.cursor()
        for t in ("edges", "dedup_log", "pending_writes", "inbox", "audit_log", "nodes",
                  "scope_grants", "scopes", "edge_rules", "edge_types", "node_types",
                  "config", "api_tokens", "principals"):
            cur.execute(f"DELETE FROM {t} WHERE space_id = %s", (space_id,))
        cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))


async def main() -> int:
    ap = argparse.ArgumentParser(prog="bench.dupstream")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--policy", default="always_distinct",
                    choices=["always_distinct", "llm_adjudicate"])
    ap.add_argument("--keep", action="store_true", help="do not drop the run space")
    args = ap.parse_args()

    classes = read_jsonl() if FIXTURE.exists() else generate()
    if not FIXTURE.exists():
        write_jsonl(classes)
        print(f"generated and committed {FIXTURE}")

    policy = None
    if args.policy == "llm_adjudicate":
        from bench.core.ingest import LLMAdjudicate
        from bench.core.llm import ROLE_MODELS
        from bench.core.providers import ClaudeCLIClient

        # The adjudicator runs on the Claude CLI (subscription), not the judge's
        # Gemini route -- so this comparison needs no API credit and no
        # GEMINI_API_KEY.
        policy = LLMAdjudicate(ClaudeCLIClient(model=ROLE_MODELS["adjudicator"]["model"]))

    embedding.load_model()  # warm once, outside the timed writes
    run_id = f"dup{uuid.uuid4().hex[:10]}"
    space_id = space_id_for(run_id)

    haystacks = ["measured", "control"]
    with psycopg.connect(DB, autocommit=True) as c:
        run_space = provision_run_space(c, run_id=run_id, haystack_ids=haystacks)

    results = []
    pool = AsyncConnectionPool(APP_DB, open=False, min_size=2, max_size=4)
    await pool.open()
    try:
        for seed in args.seeds:
            # Each seed gets its own scopes so orders cannot contaminate each
            # other; the space is torn down whole at the end.
            m_scope = run_space.scope_for("measured")
            c_scope = run_space.scope_for("control")
            if len(args.seeds) > 1:
                m_scope, c_scope = f"{m_scope}-s{seed}", f"{c_scope}-s{seed}"
                with psycopg.connect(DB, autocommit=True) as c:
                    cur = c.cursor()
                    for sid in (m_scope, c_scope):
                        cur.execute(
                            "INSERT INTO scopes (space_id, id, display_name, owner_principal, "
                            "visibility) VALUES (%s, %s, %s, %s, 'private') "
                            "ON CONFLICT DO NOTHING",
                            (run_space.space_id, sid, sid, run_space.principal),
                        )

            print(f"\n--- seed {seed} ---")
            measured = await run_arm(pool, run_space, classes, arm="measured",
                                     scope_id=m_scope, seed=seed, policy=policy)
            control = await run_arm(pool, run_space, classes, arm="control",
                                    scope_id=c_scope, seed=seed)
            r = score(classes, measured, control, seed=seed, policy=args.policy)
            r.probe_hit_rate_measured, r.payload_bytes_measured = await probe(
                pool, run_space, classes, measured)
            r.probe_hit_rate_control, r.payload_bytes_control = await probe(
                pool, run_space, classes, control)
            results.append(r)
            print(json.dumps(r.as_dict(), indent=2))
    finally:
        await pool.close()
        if not args.keep:
            _teardown(space_id)

    manifest = {
        "run_id": run_id,
        "benchmark": "dupstream",
        "generated_at": datetime.now(UTC).isoformat(),
        "engine_git_sha": _git_sha(),
        "pack": {"name": run_space.pack_name, "version": run_space.pack_version},
        "embedding_model": embedding.MODEL_ID,
        "embedding_revision": embedding.MODEL_REVISION,
        "confirm_policy": args.policy,
        "role_models": {"adjudicator": args.policy},
        "seeds": args.seeds,
        "classes": len(classes),
        "variants": sum(len(c.variants) for c in classes),
        "control_arm": "forced-insert via thresholds t_high=1.01/t_low=1.005 "
                       "(labelled; never the measured arm)",
        "token_counter": None,
        "token_counter_note": "no API credential available; payload measured in bytes, "
                              "not tokens. Token figures are deliberately absent rather "
                              "than locally estimated.",
        "results": [r.as_dict() for r in results],
    }
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"dupstream-{run_id}.json"
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nmanifest: {out}")

    _summarise(results)
    return 0


def _summarise(results) -> None:
    if not results:
        return
    print("\n" + "=" * 68)
    print(f"{'seed':<6}{'prec(x)':>9}{'prec(all)':>11}{'recall':>9}{'growth':>8}"
          f"{'probe(m)':>10}{'probe(c)':>10}{'confirm':>9}")
    for r in results:
        print(f"{r.seed:<6}{r.precision:>9.3f}{r.precision_incl_contradictions:>11.3f}"
              f"{r.recall:>9.3f}{r.growth:>8.2f}"
              f"{r.probe_hit_rate_measured:>10.2f}{r.probe_hit_rate_control:>10.2f}"
              f"{r.confirm_band_rate:>9.2f}")
    n = len(results)
    print("-" * 68)
    print(f"{'mean':<6}{sum(r.precision for r in results)/n:>9.3f}"
          f"{sum(r.precision_incl_contradictions for r in results)/n:>11.3f}"
          f"{sum(r.recall for r in results)/n:>9.3f}"
          f"{sum(r.growth for r in results)/n:>8.2f}"
          f"{sum(r.probe_hit_rate_measured for r in results)/n:>10.2f}"
          f"{sum(r.probe_hit_rate_control for r in results)/n:>10.2f}"
          f"{sum(r.confirm_band_rate for r in results)/n:>9.2f}")
    spread = max(r.precision for r in results) - min(r.precision for r in results)
    print(f"\nprecision spread across seeds: {spread:.3f}")
    nodes_m = sum(r.measured_nodes for r in results) / n
    nodes_c = sum(r.control_nodes for r in results) / n
    print(f"store size: measured {nodes_m:.1f} vs control {nodes_c:.1f} nodes "
          f"({nodes_c / nodes_m:.2f}x reduction)" if nodes_m else "")
    contra = sum(r.contradictions_merged for r in results)
    total_contra = sum(r.contradictions_total for r in results)
    print(f"contradictions merged away: {contra}/{total_contra}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
