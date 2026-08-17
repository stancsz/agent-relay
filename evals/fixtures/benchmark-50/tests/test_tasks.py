from tasks import (
    add_active,
    add_headers,
    append_value,
    build_schema,
    cached_value,
    clamp_percent,
    copy_metadata,
    copy_tags,
    dedupe,
    first_or_none,
    format_bytes,
    format_labels,
    get_retry_limit,
    is_blank,
    is_even,
    join_names,
    last_item,
    make_tag,
    nonnegative_count,
    normalize_email,
    parse_bool,
    parse_csv_line,
    parse_port,
    read_timeout,
    redact_secret,
    rename_fields,
    require_nonempty,
    require_positive,
    safe_get,
    safe_slice,
    select_columns,
    serialize_pair,
    serialize_user,
    scale_int,
    slugify,
)


def test_require_nonempty():
    assert require_nonempty("ok") == "ok"
    try:
        require_nonempty("")
    except ValueError:
        pass
    else:
        raise AssertionError("empty value must be rejected")


def test_parse_port():
    assert parse_port("8080") == 8080
    for value in ("0", "65536"):
        try:
            parse_port(value)
        except ValueError:
            pass
        else:
            raise AssertionError("port must be in range")


def test_clamp_percent():
    assert clamp_percent(40) == 40
    for value in (-1, 101):
        try:
            clamp_percent(value)
        except ValueError:
            pass
        else:
            raise AssertionError("percentage must be in range")


def test_require_positive():
    assert require_positive(2) == 2
    try:
        require_positive(0)
    except ValueError:
        pass
    else:
        raise AssertionError("zero must be rejected")


def test_parse_bool():
    assert parse_bool("true") is True
    assert parse_bool("FALSE") is False
    try:
        parse_bool("maybe")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown boolean must be rejected")


def test_nonnegative_count():
    assert nonnegative_count(0) == 0
    try:
        nonnegative_count(-1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative count must be rejected")


def test_safe_slice():
    assert safe_slice(["a", "b", "c"], 0, 2) == ["a", "b"]
    for start, end in ((-1, 2), (2, 1)):
        try:
            safe_slice(["a", "b"], start, end)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid slice bounds must be rejected")


def test_normalize_email():
    assert normalize_email(" User@Example.COM ") == "user@example.com"


def test_slugify():
    assert slugify("Hello   World") == "hello-world"


def test_redact_secret():
    assert redact_secret("secret-value") == "*******alue"


def test_is_blank():
    assert is_blank("  \t") is True
    assert is_blank("x") is False


def test_parse_csv_line():
    assert parse_csv_line("a, b, c") == ["a", "b", "c"]


def test_format_bytes():
    assert format_bytes(512) == "512 B"
    assert format_bytes(2048) == "2.0 KB"


def test_dedupe():
    assert dedupe(["a", "b", "a"]) == ["a", "b"]


def test_safe_get():
    assert safe_get({"a": "1"}, "missing", "fallback") == "fallback"


def test_make_tag():
    assert make_tag("item") == "<item>"


def test_add_active():
    assert add_active({"name": "Ada", "active": True}) == {"name": "Ada", "active": True}


def test_copy_metadata():
    assert copy_metadata({"id": 1, "name": "Ada", "tags": ["admin"]}) == {
        "id": 1,
        "name": "Ada",
        "tags": ["admin"],
    }


def test_rename_fields():
    assert rename_fields({"first_name": "Ada", "last_name": "Lovelace", "email": "a@example.com"}) == {
        "first": "Ada",
        "last": "Lovelace",
        "email": "a@example.com",
    }


def test_serialize_pair():
    assert serialize_pair("left", "right") == "left=left;right=right"


def test_format_labels():
    assert format_labels([" Alpha ", "BETA"]) == "alpha, beta"


def test_add_headers():
    assert add_headers([["Ada", "admin"]]) == [["name", "email"], ["Ada", "admin"]]


def test_select_columns():
    assert select_columns({"id": 1, "name": "Ada", "active": True}) == {"id": 1, "name": "Ada"}


def test_copy_tags():
    assert copy_tags({"tags": ["a", "b"]}) == {"tags": ["a", "b"], "tag_count": 2}


def test_scale_int():
    assert scale_int(3, 4) == 12
    assert scale_int.__annotations__["return"] is int


def test_join_names():
    assert join_names(["Ada", "Grace"]) == "Ada, Grace"
    assert join_names.__annotations__["values"] == list[str]


def test_append_value():
    assert append_value() == [1]
    assert append_value() == [1]


def test_is_even():
    assert is_even(4) is True
    assert is_even(3) is False


def test_read_timeout():
    assert read_timeout({}) == 30
    assert read_timeout({"timeout": 5}) == 5


def test_build_schema():
    assert build_schema() == {"name": str, "active": bool}


def test_get_retry_limit():
    assert get_retry_limit({}) == 3
    assert get_retry_limit({"retries": 5}) == 5


def test_serialize_user():
    assert serialize_user({"name": "Ada", "email": "a@example.com"}) == {
        "name": "Ada",
        "email": "a@example.com",
    }


def test_last_item():
    assert last_item(["a", "b"]) == "b"


def test_cached_value():
    assert cached_value({"a": "1"}, "a") == "1"
    assert cached_value({}, "missing") is None


def test_first_or_none():
    assert first_or_none(["a"]) == "a"
    assert first_or_none([]) is None
