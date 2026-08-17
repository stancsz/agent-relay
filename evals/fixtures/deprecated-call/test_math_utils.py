from math_utils import use_add


def test_use_add_uses_existing_add() -> None:
    assert use_add(1, 2) == 3

