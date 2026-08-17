import pytest

from calculator import divide


def test_zero_denominator_is_explicit() -> None:
    with pytest.raises(ValueError):
        divide(10, 0)


def test_nonzero_division_is_unchanged() -> None:
    assert divide(10, 2) == 5

