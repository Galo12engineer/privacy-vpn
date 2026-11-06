import math

import pytest

from app.services.account_number import (
    ACCOUNT_NUMBER_DIGITS,
    generate_account_number,
    hash_account_number,
    is_valid_format,
    normalize_account_number,
)


def test_entropy_at_least_60_bits():
    assert ACCOUNT_NUMBER_DIGITS * math.log2(10) >= 60


def test_generate_format_and_grouping():
    n = generate_account_number()
    parts = n.split(" ")
    assert len(parts) == 5  # 20 digits in groups of 4
    assert all(len(p) == 4 and p.isdigit() for p in parts)
    assert len(normalize_account_number(n)) == 20


def test_generate_is_random():
    assert generate_account_number() != generate_account_number()


def test_normalize_strips_separators():
    assert normalize_account_number("1234 5678 9012 3456 7890") == "12345678901234567890"
    assert normalize_account_number("1234-5678-9012-3456-7890") == "12345678901234567890"


def test_normalize_rejects_bad_length():
    with pytest.raises(ValueError):
        normalize_account_number("123")
    assert not is_valid_format("123")
    assert is_valid_format("1234 5678 9012 3456 7890")


def test_hash_deterministic_across_formats():
    h1 = hash_account_number("1234 5678 9012 3456 7890", "pepper-1")
    h2 = hash_account_number("12345678901234567890", "pepper-1")
    assert h1 == h2
    assert len(h1) == 64  # hex sha256


def test_hash_depends_on_pepper():
    h1 = hash_account_number("12345678901234567890", "pepper-1")
    h2 = hash_account_number("12345678901234567890", "pepper-2")
    assert h1 != h2
