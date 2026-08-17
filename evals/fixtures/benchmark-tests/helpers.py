def normalize_email(value: str) -> str:
    return value.strip().lower()


def slugify(value: str) -> str:
    return "-".join(value.lower().split())


def redact_secret(value: str) -> str:
    return "*" * max(0, len(value) - 4) + value[-4:]


def is_blank(value: str) -> bool:
    return not value.strip()


def parse_csv_line(value: str) -> list[str]:
    return [item.strip() for item in value.split(",")]


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    return f"{value / 1024:.1f} KB"


def dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def safe_get(mapping: dict[str, str], key: str, default: str | None = None) -> str | None:
    return mapping.get(key, default)


def make_tag(name: str) -> str:
    return f"<{name}>"
