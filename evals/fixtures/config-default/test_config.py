from config import get_timeout


def test_missing_timeout_uses_default() -> None:
    assert get_timeout({}) == 30


def test_explicit_timeout_wins() -> None:
    assert get_timeout({"timeout": 7}) == 7

