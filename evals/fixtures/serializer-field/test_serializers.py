from serializers import serialize_admin, serialize_user


def test_both_serializers_include_active() -> None:
    user = {"name": "Ada", "active": True}
    assert serialize_user(user) == {"name": "Ada", "active": True}
    assert serialize_admin(user) == {"name": "Ada", "active": True}

