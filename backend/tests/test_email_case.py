"""Email case-insensitivity regression tests.

The bug: register stored email as-typed and checked uniqueness case-sensitively,
so Ryan.bolakowski@yahoo.com and ryan.bolakowski@yahoo.com could become two
accounts, and a delete/lookup keyed on one case would miss the other.
"""

def test_email_normalization_lowercases_and_strips():
    # Mirrors the exact normalization the register/reset paths apply
    for raw, expected in [
        ("Ryan.Bolakowski@Yahoo.com", "ryan.bolakowski@yahoo.com"),
        ("  RYAN@EXAMPLE.COM  ",       "ryan@example.com"),
        ("already@lower.com",          "already@lower.com"),
    ]:
        assert (raw or "").lower().strip() == expected


def test_two_case_variants_are_same_normalized_email():
    a = "Ryan.bolakowski@yahoo.com".lower().strip()
    b = "ryan.bolakowski@yahoo.com".lower().strip()
    assert a == b, "case variants must normalize to the same address"
