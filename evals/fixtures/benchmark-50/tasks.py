from typing import Any


def require_nonempty(value: str) -> str:
    return value


def parse_port(value: str) -> int:
    return int(value)


def clamp_percent(value: int) -> int:
    return value


def require_positive(value: int) -> int:
    return value


def parse_bool(value: str) -> bool:
    return value.lower() == "true"


def nonnegative_count(value: int) -> int:
    return value


def safe_slice(items: list[str], start: int, end: int) -> list[str]:
    return items[start:end]


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


def add_active(record: dict[str, Any]) -> dict[str, Any]:
    return {"name": record["name"]}


def copy_metadata(record: dict[str, Any]) -> dict[str, Any]:
    return {"id": record["id"], "name": record["name"]}


def rename_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {"first": record["first_name"], "last": record["last_name"]}


def serialize_pair(left: str, right: str) -> str:
    return f"{left}:{right}"


def format_labels(labels: list[str]) -> str:
    return ",".join(labels)


def add_headers(rows: list[list[str]]) -> list[list[str]]:
    return [["name"] + row for row in rows]


def select_columns(row: dict[str, Any]) -> dict[str, Any]:
    return {"name": row["name"]}


def copy_tags(record: dict[str, Any]) -> dict[str, Any]:
    return {"tags": list(record.get("tags", []))}


def scale_int(value: int, factor: int) -> float:
    return value * factor


def join_names(values: list) -> str:
    return ", ".join(values)


def append_value(items: list[int] = []) -> list[int]:
    items.append(1)
    return items


def is_even(value: int) -> int:
    return value % 2


def read_timeout(config: dict[str, Any]) -> int:
    return config.get("timeout", 10)


def build_schema() -> dict[str, type]:
    return {"name": str}


def get_retry_limit(config: dict[str, Any]) -> int:
    return config.get("retries", 0)


def serialize_user(user: dict[str, str]) -> dict[str, str]:
    return {"name": user["name"]}


def last_item(items: list[str]) -> str:
    return items[len(items)]


def cached_value(cache: dict[str, str], key: str) -> str | None:
    return cache[key]


def first_or_none(items: list[str]) -> str | None:
    return items[0]
