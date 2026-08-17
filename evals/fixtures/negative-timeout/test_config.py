import pytest

from config import parse_timeout


def test_negative_timeout_raises() -> None:
    with pytest.raises(ValueError):
        parse_timeout(-1)


def test_zero_and_positive_values_remain_valid() -> None:
    assert parse_timeout(0) == 0
    assert parse_timeout(30) == 30

