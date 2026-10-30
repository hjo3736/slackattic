from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from slackattic.core.timestamps import month_of, parse_ts, shift_ts
from slackattic.slack.errors import SlackChannelError, SlackError
from slackattic.storage.jsonl import read_jsonl, write_jsonl_if_changed
from slackattic.storage.state import ChannelState, load_state, save_state

STATE_FILE = "state.json"
MESSAGES_DIR = "messages"
# Re-read this much before the newest stored message so edges and late changes are caught
LOOKBACK_SECONDS = 24 * 60 * 60


class HistorySource(Protocol):
    def iter_history(self, channel: str, oldest: str | None = None) -> Iterator[dict[str, Any]]: ...


@dataclass
class ChannelResult:
    channel_id: str
    fetched: int = 0
    added: int = 0
    updated: int = 0


@dataclass
class MessagesResult:
    channels: list[ChannelResult] = field(default_factory=list)
    skipped: list[SlackChannelError] = field(default_factory=list)


def sync_messages(
    client: HistorySource, archive_dir: Path, channel_ids: list[str]
) -> MessagesResult:
    state_path = archive_dir / STATE_FILE
    state = load_state(state_path)
    result = MessagesResult()
    for channel_id in channel_ids:
        try:
            result.channels.append(
                _sync_channel(client, archive_dir, channel_id, state.channel(channel_id))
            )
        except SlackChannelError as e:
            result.skipped.append(e)
            continue
        # Saved only after the channel's files are written so state never runs ahead of data
        save_state(state_path, state)
    return result


def _sync_channel(
    client: HistorySource, archive_dir: Path, channel_id: str, channel_state: ChannelState
) -> ChannelResult:
    oldest = None
    if channel_state.latest_ts is not None:
        oldest = shift_ts(channel_state.latest_ts, -LOOKBACK_SECONDS)

    by_month: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for message in client.iter_history(channel_id, oldest):
        ts = message.get("ts")
        try:
            month = month_of(ts)
        except ValueError:
            raise SlackError(f"{channel_id}: message without a valid ts: {ts!r}") from None
        by_month[month][ts] = message

    result = ChannelResult(channel_id, fetched=sum(len(m) for m in by_month.values()))
    channel_dir = archive_dir / MESSAGES_DIR / channel_id
    for month in sorted(by_month):
        path = channel_dir / f"{month}.jsonl"
        stored = {m["ts"]: m for m in read_jsonl(path) if isinstance(m.get("ts"), str)}
        for ts, message in by_month[month].items():
            if ts not in stored:
                result.added += 1
            elif stored[ts] != message:
                result.updated += 1
            # Latest copy wins so edits and reactions inside the lookback are refreshed
            stored[ts] = message
        write_jsonl_if_changed(path, (stored[ts] for ts in sorted(stored, key=parse_ts)))

    fetched_ts = [ts for messages in by_month.values() for ts in messages]
    if fetched_ts:
        newest = max(fetched_ts, key=parse_ts)
        current = channel_state.latest_ts
        if current is None or parse_ts(newest) > parse_ts(current):
            channel_state.latest_ts = newest
    return result
