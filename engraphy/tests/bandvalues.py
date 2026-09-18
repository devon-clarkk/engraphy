"""Synthetic similarities named by the band they select on the ACTIVE profile.

The dedup pipeline tests seed vectors at a controlled cosine (test_dedup.py's
`_unit_vector_at_angle`) to drive a write into a chosen band, then assert what
the write path did with it. The mechanics under test are the same on every
profile; only the number that reaches the chosen band moves.

For most of this suite's life a literal did that job, because one set of
thresholds shipped and 0.87 meant `pending` to everyone reading it. A profile
calibrated somewhere else reads that same literal as a different band: 0.87 is
`pending` against fp32's 0.95/0.80 and `insert` against micro's 0.955/0.902. A
suite pinned to literals therefore exercises the write path only on the profile
it happened to be written for, and reports a broken engine when the truth is a
stale constant.

Naming the band keeps the mechanics under test and lets the number follow the
calibration. A test that seeds `PENDING` is asking for a confirm round-trip,
which is what 0.87 always meant.

Two decimal places, deliberately. `core/dedup.py` rounds every similarity it
reports to 2dp, so a 2dp constant is exactly what comes back out and a test can
assert equality against it without a second rounding step.
"""
from engraphy.core.dedup import BandThresholds, resonance_floor_default

_B = BandThresholds.for_profile()

#: Squarely in the merge band without being 1.0, which takes a different path
#: through the novelty check and is therefore a different test.
MERGE = round((_B.t_high + 1.0) / 2, 2)

#: The middle of the confirm band: the farthest a single value can sit from both
#: edges, so it is the safest thing to seed when a test just wants `pending`.
PENDING = round((_B.t_high + _B.t_low) / 2, 2)

#: A second confirm-band value, nearer `t_high`. Some tests need two distinct
#: pending similarities in one scenario -- typically a replacement that is
#: "supposed to be very similar to what it replaces" beside an unrelated
#: near-duplicate -- and reusing PENDING for both would hide which one a
#: candidate query actually matched.
PENDING_NEAR_MERGE = round(_B.t_low + 0.7 * (_B.t_high - _B.t_low), 2)

#: A `t_high` low enough that PENDING lands above it, for the tests that prove a
#: per-space `dedup.t_high` config row is read and applied. It must also stay at
#: or above `t_low`, or the config validator rejects the pair before the
#: behaviour under test can happen.
CONFIG_T_HIGH_BELOW_PENDING = round((_B.t_low + PENDING) / 2, 2)


# ---- resonance ------------------------------------------------------------
# `resonance.floor` is the other absolute cosine a write path compares against,
# and it moves per profile for the same reason the bands do (core/dedup.py
# `_PROFILE_RESONANCE_FLOOR`). The tests that exercise it need the same three
# positions relative to it that the band tests need relative to the bands.
_F = resonance_floor_default()

#: Comfortably above the floor, so the node appears in the resonance report.
RESONATES = round((_F + 1.0) / 2, 2)

#: The floor itself. The comparison is `>=`, so this value must resonate, and a
#: test asserting that is asserting the boundary semantics rather than a number.
AT_RESONANCE_FLOOR = _F

#: Comfortably below the floor, so the node is not a resonance at all.
BELOW_RESONANCE = round(_F * 0.6, 2)

#: A configured floor that sits above RESONATES, for the test that proves a
#: per-space `resonance.floor` row is read and applied.
ABOVE_RESONATES = round((RESONATES + 1.0) / 2, 2)
