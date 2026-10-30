from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slackattic.core.errors import StorageError
from slackattic.core.timestamps import parse_ts
from slackattic.storage.files import read_json, write_json_if_changed

STATE_VERSION = 1


@dataclass
class ChannelState:
    # Newest message ts stored for this channel; None until something is stored
    latest_ts: str | None = None
    # Week 2: thread root ts -> latest_reply whose replies were fully stored
    threads: dict[str, str] = field(default_factory=dict)


@dataclass
class State:
    channels: dict[str, ChannelState] = field(default_factory=dict)

    def channel(self, channel_id: str) -> ChannelState:
        return self.channels.setdefault(channel_id, ChannelState())


def load_state(path: Path) -> State:
    data = read_json(path, default=None)
    if data is None:
        return State()
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        raise StorageError(
            f"{path}: unsupported state format (expected version {STATE_VERSION}); "
            "fix or remove the file"
        )
    channels = data.get("channels")
    if not isinstance(channels, dict):
        raise StorageError(f"{path}: 'channels' must be an object")
    return State({cid: _parse_channel(path, cid, raw) for cid, raw in channels.items()})


def save_state(path: Path, state: State) -> bool:
    return write_json_if_changed(path, _dump(state))


def _parse_channel(path: Path, channel_id: str, raw: Any) -> ChannelState:
    try:
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        latest = raw.get("latest_ts")
        if latest is not None:
            parse_ts(latest)
        threads = raw.get("threads", {})
        if not isinstance(threads, dict):
            raise ValueError("'threads' must be an object")
        for root, reply in threads.items():
            parse_ts(root)
            parse_ts(reply)
    except ValueError as e:
        raise StorageError(f"{path}: channel {channel_id}: {e}") from None
    return ChannelState(latest, dict(threads))


def _dump(state: State) -> dict[str, Any]:
    channels = {}
    for cid, ch in state.channels.items():
        entry: dict[str, Any] = {"threads": ch.threads}
        if ch.latest_ts is not None:
            entry["latest_ts"] = ch.latest_ts
        channels[cid] = entry
    return {"version": STATE_VERSION, "channels": channels}
