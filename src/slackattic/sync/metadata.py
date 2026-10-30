from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from slackattic.core.config import Config
from slackattic.core.errors import StorageError
from slackattic.core.fields import (
    CHANNEL_OPTIONAL,
    CHANNEL_REQUIRED,
    USER_OPTIONAL,
    USER_REQUIRED,
    project,
    restrict,
)
from slackattic.storage.files import read_json, write_json_if_changed

CHANNELS_FILE = "channels.json"
USERS_FILE = "users.json"


class MetadataSource(Protocol):
    def iter_channels(self) -> Iterator[dict[str, Any]]: ...

    def iter_users(self) -> Iterator[dict[str, Any]]: ...


@dataclass(frozen=True)
class MetadataResult:
    channels_changed: bool
    users_changed: bool
    # Channels to collect messages from in this run
    selected: list[dict[str, Any]]
    # Configured channel IDs that are unknown, archived or not joined
    missing: list[str]


def sync_metadata(client: MetadataSource, config: Config) -> MetadataResult:
    # Fetch everything before writing so an API failure never leaves a half-written list
    fetched_channels = [
        project(raw, CHANNEL_REQUIRED, CHANNEL_OPTIONAL, config.channel_fields)
        for raw in client.iter_channels()
    ]
    users = [
        project(raw, USER_REQUIRED, USER_OPTIONAL, config.user_fields)
        for raw in client.iter_users()
    ]

    channels_path = config.archive_dir / CHANNELS_FILE
    channels = _merge_channels(_load_previous(channels_path), fetched_channels, config)
    channels_changed = write_json_if_changed(channels_path, channels)
    users_changed = write_json_if_changed(config.archive_dir / USERS_FILE, _sort(users))

    selected = [
        c
        for c in channels
        if c.get("is_member")
        and not c.get("is_archived")
        and (not config.channels or c["id"] in config.channels)
    ]
    selected_ids = {c["id"] for c in selected}
    missing = [cid for cid in config.channels if cid not in selected_ids]
    return MetadataResult(channels_changed, users_changed, selected, missing)


def _merge_channels(
    previous: list[dict[str, Any]], fetched: list[dict[str, Any]], config: Config
) -> list[dict[str, Any]]:
    """Keep channels seen before so archived messages still have a channel name."""
    merged = {c["id"]: c for c in fetched}
    for old in previous:
        if old["id"] not in merged:
            kept = restrict(old, CHANNEL_REQUIRED, config.channel_fields)
            merged[old["id"]] = {**kept, "is_member": False}
    return _sort(merged.values())


def _load_previous(path: Path) -> list[dict[str, Any]]:
    data = read_json(path, default=[])
    if not isinstance(data, list) or not all(
        isinstance(item, dict) and isinstance(item.get("id"), str) for item in data
    ):
        raise StorageError(f"{path}: expected a list of objects with an 'id'; fix or remove it")
    return data


def _sort(items: Any) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: item["id"])
