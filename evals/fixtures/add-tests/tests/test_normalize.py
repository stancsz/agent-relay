import pytest

from normalize import normalize


def test_normal_input() -> None:
    assert normalize(" Hello ") == "hello"

