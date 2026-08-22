"""Addendum-novelty Jaccard. Normative: design/07 §Exact formulas.

Tokenize: Unicode-lowercase -> every non-alphanumeric codepoint becomes a space ->
split on whitespace -> discard empties -> SET of tokens.
J(A, B) = |A & B| / |A | B|;  J = 1.0 when BOTH sets are empty.
Addendum is appended iff J < 0.8.
Fixtures: engraphy/tests/fixtures/jaccard_cases.yaml (byte-exact expected values).
"""


def tokenize(text: str) -> set[str]:
    """str.isalnum() per codepoint defines "alphanumeric"."""
    tokens = []
    current = []
    for ch in text.lower():
        if ch.isalnum():
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return set(tokens)


def jaccard(body_a: str, body_b: str) -> float:
    a, b = tokenize(body_a), tokenize(body_b)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def is_novel(incoming_body: str, canonical_body_plus_addenda: str) -> bool:
    """True iff jaccard(...) < 0.8 (strict)."""
    return jaccard(incoming_body, canonical_body_plus_addenda) < 0.8
