"""The shims (design/09 §The shared core / shim boundary).

A shim may **translate** (raw suite format -> `Corpus`) and **score** (only where
the suite's own protocol demands something the default judge cannot express). It
may not ingest, retrieve, answer, count tokens, or time anything -- all of that
is shared-core work, so that no suite can be measured on a different footing
from another.

A shim past ~250 lines is a design smell: it means the shared core is missing a
declared capability, and the fix is to add the capability where an auditor can
see it in the manifest, not to special-case it here.
"""

__all__ = []
