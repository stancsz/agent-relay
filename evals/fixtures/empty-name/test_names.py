import pytest

from names import first_name


def test_empty_names_raise() -> None:
    with pytest.raises(ValueError):
        first_name([])


def test_first_name_is_preserved() -> None:
    assert first_name(["Ada", "Lin"]) == "Ada"

