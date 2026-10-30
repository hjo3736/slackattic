from collections.abc import Callable, Mapping
from typing import Any

Extractor = Callable[[Mapping[str, Any]], Any]


def _nested(key: str, sub: str) -> Extractor:
    return lambda raw: (raw.get(key) or {}).get(sub, "")


def _profile(sub: str) -> Extractor:
    return _nested("profile", sub)


CHANNEL_REQUIRED: tuple[str, ...] = ("id", "name", "is_private", "is_archived", "is_member")
CHANNEL_OPTIONAL: dict[str, Extractor] = {
    "topic": _nested("topic", "value"),
    "purpose": _nested("purpose", "value"),
    "created": lambda raw: raw.get("created"),
}
DEFAULT_CHANNEL_FIELDS: tuple[str, ...] = ("topic", "purpose", "created")

USER_REQUIRED: tuple[str, ...] = ("id", "name", "is_bot", "deleted")
USER_OPTIONAL: dict[str, Extractor] = {
    "real_name": _profile("real_name"),
    "display_name": _profile("display_name"),
    "avatar": _profile("image_72"),
    "title": _profile("title"),
    "tz": lambda raw: raw.get("tz", ""),
}
DEFAULT_USER_FIELDS: tuple[str, ...] = ("real_name", "display_name", "avatar")

_BOOL_FIELDS = frozenset({"is_private", "is_archived", "is_member", "is_bot", "deleted"})


def project(
    raw: Mapping[str, Any],
    required: tuple[str, ...],
    optional: Mapping[str, Extractor],
    enabled: tuple[str, ...],
) -> dict[str, Any]:
    """Turn a raw API object into a stored item with the required and enabled fields."""
    item = {key: raw.get(key, False if key in _BOOL_FIELDS else "") for key in required}
    item.update({name: optional[name](raw) for name in enabled})
    return item


def restrict(
    item: Mapping[str, Any], required: tuple[str, ...], enabled: tuple[str, ...]
) -> dict[str, Any]:
    """Drop fields from an already stored item that are no longer enabled."""
    keep = (*required, *enabled)
    return {key: item[key] for key in keep if key in item}
