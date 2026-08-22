"""Duplicate-stream generator (design/09 §The duplicate-stream benchmark).

Emits labeled **equivalence classes**: one underlying fact in several surface
forms, each variant tagged with the band it should land in. Because the labels
are the ground truth, scoring needs no LLM judge — precision and recall are
computed against the labels directly.

**Deterministic templates, not LLM paraphrase** *(deviation from design/09,
recorded 2026-07-21)*. The design said classes would be generated with an LLM
offline and committed. Templates are better here and the change is an
improvement rather than a concession: the ground truth becomes exactly
reproducible from a seed, with no model version silently altering what
"paraphrase" means between runs, and a reader can audit every class by reading
the templates rather than trusting a generation run they cannot repeat. The cost
is narrower linguistic variety than an LLM would produce, which understates
paraphrase difficulty — so the resulting recall figure is, if anything,
conservative in the flattering direction and must be reported as such.

Variant kinds and what each is for:

| kind            | expected band  | what a wrong answer costs                   |
|-----------------|----------------|---------------------------------------------|
| `exact`         | merge          | trivially detectable; a floor check         |
| `near`          | merge          | punctuation/filler only                     |
| `paraphrase`    | confirm band   | the interesting case — the policy decides   |
| `elaboration`   | confirm band   | merging loses the added detail              |
| `contradiction` | NOT merge      | merging destroys one of two opposed facts   |
| `distinct`      | insert         | merging here is a **precision failure**     |
"""

from __future__ import annotations

import json
import pathlib
import random
from dataclasses import asdict, dataclass

__all__ = ["Variant", "FactClass", "generate", "write_jsonl", "read_jsonl", "FIXTURE"]

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "classes.jsonl"


@dataclass(frozen=True, slots=True)
class Variant:
    variant_id: str
    class_id: str
    kind: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class FactClass:
    """One underlying fact, its variants, and a probe that should retrieve it."""

    class_id: str
    subject: str
    fact: str
    probe: str
    probe_answer: str
    variants: tuple[Variant, ...]


# Subject / predicate seeds. Deliberately mundane and mutually unrelated: the
# classes must not be semantically near each other, or a cross-class merge would
# be a reasonable judgement rather than a precision failure.
_SEEDS = [
    ("Priya", "works as a paediatric nurse at St Mary's", "paediatric nurse at St Mary's",
     "a marine biologist at the aquarium", "What is Priya's job?"),
    ("Tomas", "keeps bees in three hives behind the barn", "keeps three beehives",
     "raises chickens in a coop by the gate", "What does Tomas keep behind the barn?"),
    ("Ingrid", "cycles to the office every morning", "cycles to work daily",
     "takes the tram to the office each morning", "How does Ingrid get to the office?"),
    ("Marcus", "is allergic to shellfish", "has a shellfish allergy",
     "is allergic to walnuts", "What is Marcus allergic to?"),
    ("Yuki", "plays cello in a string quartet", "plays cello in a quartet",
     "plays trumpet in a brass band", "What instrument does Yuki play?"),
    ("Dara", "grew up in Cork before moving to Leeds", "grew up in Cork",
     "grew up in Galway before moving to Leeds", "Where did Dara grow up?"),
    ("Nadia", "runs a bakery on Mill Street", "runs a Mill Street bakery",
     "runs a bookshop on Mill Street", "What does Nadia run on Mill Street?"),
    ("Olu", "is training for the Berlin marathon in September", "is training for a marathon",
     "is training for a triathlon in September", "What is Olu training for?"),
    ("Freya", "adopted a retired greyhound called Pepper", "adopted a greyhound named Pepper",
     "adopted a tabby cat called Pepper", "What animal did Freya adopt?"),
    ("Sam", "speaks fluent Portuguese", "is fluent in Portuguese",
     "speaks fluent Polish", "Which language does Sam speak fluently?"),
    ("Hana", "drives a battered green estate car", "drives an old green estate",
     "drives a new red hatchback", "What car does Hana drive?"),
    ("Ravi", "teaches evening classes in ceramics", "teaches ceramics in the evenings",
     "teaches evening classes in woodwork", "What does Ravi teach?"),
]

# Filler that changes the surface without changing the fact.
_NEAR_FILLERS = ["Actually, ", "Just so you know, ", "By the way, ", "For the record, "]
_PARAPHRASE_FRAMES = [
    "It is the case that {subject} {fact}.",
    "{subject}, as it happens, {fact}.",
    "One thing about {subject}: {subject_pronoun} {fact}.",
    "Something worth remembering — {subject} {fact}.",
]
_ELABORATIONS = [
    "This has been true for about three years now.",
    "It came up again during the conversation last week.",
    "It is the first thing people tend to mention about them.",
    "They mentioned it without being asked.",
]


def generate(*, classes: int = 12, variants_per_class: int = 8, seed: int = 0) -> list[FactClass]:
    """Build the class set. Fully determined by `seed` — same seed, same bytes."""
    rng = random.Random(seed)
    if classes > len(_SEEDS):
        raise ValueError(f"only {len(_SEEDS)} seeds available; asked for {classes}")

    out: list[FactClass] = []
    for i in range(classes):
        subject, fact, short, contra_fact, probe = _SEEDS[i]
        cid = f"c{i:02d}"
        base_title = f"{subject} {short}"
        base_body = f"{subject} {fact}."
        variants: list[Variant] = [
            Variant(f"{cid}-v0", cid, "exact", base_title, base_body)
        ]

        # Fill the remaining slots, cycling the kinds so every class carries the
        # full spread rather than a random draw that might omit the case that
        # matters most.
        kinds = ["near", "paraphrase", "elaboration", "exact", "paraphrase", "near", "elaboration"]
        for j in range(1, variants_per_class):
            kind = kinds[(j - 1) % len(kinds)]
            if kind == "exact":
                title, body = base_title, base_body
            elif kind == "near":
                title = base_title
                body = rng.choice(_NEAR_FILLERS) + f"{subject} {fact}."
            elif kind == "paraphrase":
                frame = rng.choice(_PARAPHRASE_FRAMES)
                title = f"{subject}: {short}"
                body = frame.format(subject=subject, fact=fact, subject_pronoun="they")
            else:  # elaboration
                title = f"{subject} {short} (detail)"
                body = f"{subject} {fact}. {rng.choice(_ELABORATIONS)}"
            variants.append(Variant(f"{cid}-v{j}", cid, kind, title[:200], body[:8000]))

        # One contradiction and one lexically-similar distinct fact per class.
        # The distinct variant is the precision trap: same subject, similar
        # words, different fact. A merge here destroys information.
        variants.append(
            Variant(f"{cid}-contra", cid, "contradiction",
                    f"{subject} {short} — changed",
                    f"{subject} no longer {fact}; {subject} now {contra_fact}.")
        )
        variants.append(
            Variant(f"{cid}-dist", f"{cid}-other", "distinct",
                    f"{subject} {contra_fact[:60]}",
                    f"{subject} {contra_fact}.")
        )

        out.append(
            FactClass(
                class_id=cid,
                subject=subject,
                fact=fact,
                probe=probe,
                probe_answer=short,
                variants=tuple(variants),
            )
        )
    return out


def write_jsonl(classes: list[FactClass], path: pathlib.Path = FIXTURE) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for c in classes:
            fh.write(json.dumps(asdict(c), ensure_ascii=False, sort_keys=True) + "\n")
    return path


def read_jsonl(path: pathlib.Path = FIXTURE) -> list[FactClass]:
    out: list[FactClass] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            raw = json.loads(line)
            out.append(
                FactClass(
                    class_id=raw["class_id"],
                    subject=raw["subject"],
                    fact=raw["fact"],
                    probe=raw["probe"],
                    probe_answer=raw["probe_answer"],
                    variants=tuple(Variant(**v) for v in raw["variants"]),
                )
            )
    return out


def stream(classes: list[FactClass], *, seed: int = 0) -> list[Variant]:
    """Interleave every class's variants into one ingest order.

    Shuffled, because order decides which member of a class becomes canonical
    and therefore what everything else is compared against. Three seeds are run
    and the spread reported (design/09) rather than one order being presented as
    the result.
    """
    rng = random.Random(seed + 977)
    everything = [v for c in classes for v in c.variants]
    rng.shuffle(everything)
    return everything


if __name__ == "__main__":  # pragma: no cover -- one-shot baseliner
    import sys

    if FIXTURE.exists() and "--rebaseline" not in sys.argv:
        print(f"{FIXTURE} exists; pass --rebaseline to overwrite", file=sys.stderr)
        raise SystemExit(1)
    written = write_jsonl(generate())
    print(f"wrote {written}")
