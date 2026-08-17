from math_types import scale


def test_scale_returns_int() -> None:
    assert scale(3, 2) == 6
    assert isinstance(scale(3, 2), int)
