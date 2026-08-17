from helpers import (
    dedupe,
    format_bytes,
    is_blank,
    make_tag,
    normalize_email,
    parse_csv_line,
    redact_secret,
    safe_get,
    slugify,
)


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
