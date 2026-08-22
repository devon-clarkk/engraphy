"""Run the duplicate-stream benchmark and score it (design/09 §dupstream).

Two arms, and the ratio between them is the claim:

* **measured** — the shipped dedup pipeline, real bands, a declared confirm-band
  policy.
* **control** — the identical stream with every write forced to insert. This is
  the one legitimate use of a threshold override in the harness: it is not the
  measured arm, it is the counterfactual the measured arm is compared against,
  and it is labelled as such in the manifest.

Scoring is against the generator's labels — no LLM judge, so the figures are
exactly reproducible from a seed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from engraphy.core import dedup, embedding

from bench.core.ingest import AlwaysDistinct, SOURCE_CLIENT
from bench.core.meter import Meter, percentile
from bench.core.retrieve import probe_search
from bench.dupstream.generate import FactClass, Variant, stream

__all__ = ["ArmResult", "run_arm", "score", "DupstreamResult"]

# Forced-insert control: t_high/t_low above 1.0 make every similarity fall below
# the low band, so nothing can merge or park. Test surface, used deliberately
# and only for the control arm.
_CONTROL_THRESHOLDS = dedup.BandThresholds(t_high=1.01, t_low=1.005)


@dataclass
class ArmResult:
    arm: str
    scope_id: str
    inserted: int = 0
    merged: int = 0
    needs_confirmation: int = 0
    resolved_to_insert: int = 0
    resolved_to_merge: int = 0
    errors: dict = field(default_factory=dict)
    # variant_id -> the node id it ended up as (canonical if merged)
    landing: dict = field(default_factory=dict)
    # (variant_id, class_id_of_variant, node_id) for merge analysis
    merges: list = field(default_factory=list)
    write_seconds: list = field(default_factory=list)

    @property
    def active_nodes(self) -> int:
        return self.inserted + self.resolved_to_insert


async def run_arm(
    pool, run_space, classes: list[FactClass], *, arm: str, scope_id: str, seed: int,
    policy=None,
) -> ArmResult:
    """Ingest the whole stream into one scope under one banding regime."""
    policy = policy or AlwaysDistinct()
    thresholds = _CONTROL_THRESHOLDS if arm == "control" else None
    result = ArmResult(arm=arm, scope_id=scope_id)

    for variant in stream(classes, seed=seed):
        vec = embedding.embed_document(f"{variant.title}\n{variant.body}")
        t0 = time.perf_counter()
        try:
            env = await dedup.write(
                pool, run_space.space_id, run_space.principal, "note", scope_id,
                variant.title, variant.body, {}, vec, SOURCE_CLIENT,
                thresholds=thresholds, source_session=f"dupstream-{arm}",
            )
        except Exception as exc:  # noqa: BLE001
            result.errors[type(exc).__name__] = result.errors.get(type(exc).__name__, 0) + 1
            continue
        finally:
            result.write_seconds.append(time.perf_counter() - t0)

        outcome = env.get("outcome")
        if outcome == "inserted":
            result.inserted += 1
            result.landing[variant.variant_id] = env["node"]["id"]
        elif outcome == "merged":
            result.merged += 1
            nid = env["canonical"]["id"]
            result.landing[variant.variant_id] = nid
            result.merges.append((variant.variant_id, variant.class_id, nid))
        elif outcome == "needs_confirmation":
            result.needs_confirmation += 1
            decision = policy.decide(env, _as_draft(variant))
            try:
                inner = await dedup.resolve_duplicate(
                    pool, run_space.space_id, run_space.principal, env["pending_id"],
                    decision.resolution, merge_into=decision.merge_into,
                )
            except Exception as exc:  # noqa: BLE001
                key = f"resolve:{type(exc).__name__}"
                result.errors[key] = result.errors.get(key, 0) + 1
                continue
            if inner.get("outcome") == "inserted":
                result.resolved_to_insert += 1
                result.landing[variant.variant_id] = inner["node"]["id"]
            elif inner.get("outcome") == "merged":
                result.resolved_to_merge += 1
                nid = inner["canonical"]["id"]
                result.landing[variant.variant_id] = nid
                result.merges.append((variant.variant_id, variant.class_id, nid))
        else:
            result.errors[f"outcome:{outcome}"] = result.errors.get(f"outcome:{outcome}", 0) + 1

    return result


def _as_draft(variant: Variant):
    from bench.core.extract import NodeDraft

    return NodeDraft(variant.variant_id, "note", variant.title, variant.body)


@dataclass
class DupstreamResult:
    seed: int
    policy: str
    precision: float
    precision_incl_contradictions: float
    recall: float
    growth: float
    control_nodes: int
    measured_nodes: int
    contradictions_merged: int
    contradictions_total: int
    confirm_band_rate: float
    probe_hit_rate_measured: float
    probe_hit_rate_control: float
    payload_bytes_measured: int
    payload_bytes_control: int
    write_p50_ms: float
    write_p95_ms: float
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        for k in ("precision", "precision_incl_contradictions", "recall", "growth",
                  "confirm_band_rate",
                  "probe_hit_rate_measured", "probe_hit_rate_control"):
            d[k] = round(d[k], 4)
        return d


def score(
    classes: list[FactClass], measured: ArmResult, control: ArmResult, *, seed: int, policy: str,
) -> DupstreamResult:
    """Precision, recall and growth against the generator's labels.

    **Precision** counts merges that crossed an equivalence class. A cross-class
    merge destroys a fact and is the expensive error, so it is the numerator
    that matters most.

    **Recall** counts within-class merges against the opportunities to make
    them. An opportunity is a variant that is not the first member of its class
    to be written — the first one has nothing to merge into.
    """
    by_variant = {v.variant_id: v for c in classes for v in c.variants}

    # Which class does each landing node "belong" to? The first variant to land
    # on a node defines it; anything of a different class landing there later is
    # a cross-class merge.
    node_class: dict[str, str] = {}
    for vid, nid in measured.landing.items():
        cls = by_variant[vid].class_id
        node_class.setdefault(nid, cls)

    # A contradiction shares its class_id with the fact it contradicts (it is
    # *about* the same subject), but merging one is never correct -- it destroys
    # one of two opposed facts. Scoring it as a within-class success would make
    # precision blind to the single worst error this benchmark exists to catch,
    # and would inflate recall with merges we do not want. So contradictions are
    # excluded from the merge-quality numerators entirely and counted on their
    # own, and a second precision figure charges them as the failures they are.
    cross_class = 0
    within_class = 0
    contradiction_merges = 0
    unattributed = 0
    for vid, cls, nid in measured.merges:
        if by_variant[vid].kind == "contradiction":
            contradiction_merges += 1
            continue
        owner = node_class.get(nid)
        if owner is None:
            # The merge target was never claimed by a variant in `landing` --
            # e.g. it resolved through a canonical chain. Previously this fell
            # into the `else` and was credited as a within-class success, which
            # is how recall reached an impossible 1.012. Counted separately now:
            # an unattributable merge is evidence of neither precision nor
            # recall, and inventing a class for it would be guessing.
            unattributed += 1
        elif owner != cls:
            cross_class += 1
        else:
            within_class += 1

    total_merges = cross_class + within_class
    precision = 1.0 - (cross_class / total_merges) if total_merges else 1.0

    # The honest headline: every merge that destroyed a fact, over every merge.
    destructive = cross_class + contradiction_merges
    all_merges = total_merges + contradiction_merges
    precision_incl_contradictions = 1.0 - (destructive / all_merges) if all_merges else 1.0

    # Merge opportunities, defined by STREAM ORDER rather than by class size.
    #
    # "variants - 1 per class" was wrong and produced an impossible recall of
    # 1.012: a contradiction variant can be written first and own the node, and
    # then all of that class's remaining variants have something to merge into,
    # giving more real opportunities than the class-size formula allows.
    #
    # An opportunity is therefore: a non-contradiction variant written at a
    # moment when its class already has at least one node in the store. That is
    # exactly the set of writes that *could* correctly merge, it is computable
    # from the order actually ingested, and it cannot exceed itself.
    seen_classes: set[str] = set()
    opportunities = 0
    for v in stream(classes, seed=seed):
        if v.class_id in seen_classes and v.kind != "contradiction":
            opportunities += 1
        seen_classes.add(v.class_id)
    recall = within_class / opportunities if opportunities else 0.0
    if recall > 1.0:  # must be impossible; surface it rather than round it away
        raise AssertionError(
            f"recall {recall:.3f} exceeds 1.0 ({within_class}/{opportunities}) -- "
            "the merge-opportunity denominator is wrong"
        )

    unique_classes = len({v.class_id for v in by_variant.values()})
    growth = measured.active_nodes / unique_classes if unique_classes else 0.0

    contradictions = [v for v in by_variant.values() if v.kind == "contradiction"]
    contra_merged = sum(
        1 for v in contradictions
        if any(vid == v.variant_id for vid, _, _ in measured.merges)
    )

    drafts = len(by_variant)
    confirm_rate = measured.needs_confirmation / drafts if drafts else 0.0

    return DupstreamResult(
        seed=seed,
        policy=policy,
        precision=precision,
        precision_incl_contradictions=precision_incl_contradictions,
        recall=recall,
        growth=growth,
        control_nodes=control.active_nodes,
        measured_nodes=measured.active_nodes,
        contradictions_merged=contra_merged,
        contradictions_total=len(contradictions),
        confirm_band_rate=confirm_rate,
        probe_hit_rate_measured=0.0,
        probe_hit_rate_control=0.0,
        payload_bytes_measured=0,
        payload_bytes_control=0,
        write_p50_ms=round(percentile([s * 1000 for s in measured.write_seconds], 50), 2),
        write_p95_ms=round(percentile([s * 1000 for s in measured.write_seconds], 95), 2),
        detail={
            "cross_class_merges": cross_class,
            "within_class_merges": within_class,
            "contradiction_merges": contradiction_merges,
            "unattributed_merges": unattributed,
            "facts_destroyed": destructive,
            "merge_opportunities": opportunities,
            "unique_classes": unique_classes,
            "measured_outcomes": {
                "inserted": measured.inserted, "merged": measured.merged,
                "needs_confirmation": measured.needs_confirmation,
                "resolved_to_insert": measured.resolved_to_insert,
                "resolved_to_merge": measured.resolved_to_merge,
            },
            "control_outcomes": {
                "inserted": control.inserted, "merged": control.merged,
                "needs_confirmation": control.needs_confirmation,
            },
            "errors": {"measured": measured.errors, "control": control.errors},
        },
    )


async def probe(pool, run_space, classes: list[FactClass], arm: ArmResult) -> tuple[float, int]:
    """Post-dedup retrieval: can each class's fact still be found?

    The number that makes dedup mean something. Store growth alone is easy to
    win by merging everything; the probe is what shows whether the merging cost
    us the ability to answer.
    """
    hits = 0
    total_bytes = 0
    for c in classes:
        meter = Meter()
        env = await probe_search(pool, run_space, arm.scope_id, c.probe, limit=5)
        total_bytes += meter.measure_payload(env)["bytes"]
        blob = " ".join(
            (r["node"].get("title", "") + " " + r["node"].get("body", "")).lower()
            for r in env.get("results", [])
        )
        # Keyword containment, not a judge: the probe answers are short factual
        # phrases the generator itself produced, so a substring check is exact
        # here and keeps the whole benchmark LLM-free.
        if c.probe_answer.lower() in blob:
            hits += 1
    return (hits / len(classes) if classes else 0.0), total_bytes
