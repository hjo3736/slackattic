import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from slackattic.core.config import DEFAULT_RESCAN_DAYS
from slackattic.core.timestamps import format_ts, month_of, parse_ts, shift_ts
from slackattic.slack.errors import SlackChannelError, SlackError, SlackThreadError
from slackattic.storage.jsonl import read_jsonl, write_jsonl_if_changed
from slackattic.storage.state import ChannelState, load_state, save_state

STATE_FILE = "state.json"
MESSAGES_DIR = "messages"
DAY_SECONDS = 24 * 60 * 60
# Re-read this much before the newest stored message so edges and late changes are caught
LOOKBACK_SECONDS = DAY_SECONDS


class HistorySource(Protocol):
    def iter_history(self, channel: str, oldest: str | None = None) -> Iterator[dict[str, Any]]: ...

    def iter_replies(self, channel: str, thread_ts: str) -> Iterator[dict[str, Any]]: ...


@dataclass
class ChannelResult:
    channel_id: str
    fetched: int = 0
    added: int = 0
    updated: int = 0
    # Threads whose replies were fetched because they are new or changed
    threads: int = 0
    skipped_threads: list[SlackThreadError] = field(default_factory=list)


@dataclass
class MessagesResult:
    channels: list[ChannelResult] = field(default_factory=list)
    skipped: list[SlackChannelError] = field(default_factory=list)


Months = dict[str, dict[str, dict[str, Any]]]


def sync_messages(
    client: HistorySource,
    archive_dir: Path,
    channel_ids: list[str],
    rescan_days: int = DEFAULT_RESCAN_DAYS,
    now: float | None = None,
) -> MessagesResult:
    if rescan_days < 1:
        raise ValueError("rescan_days must be 1 or more")
    clock = Decimal(str(time.time() if now is None else now))
    window_start = shift_ts(format_ts(clock), -rescan_days * DAY_SECONDS)
    state_path = archive_dir / STATE_FILE
    state = load_state(state_path)
    result = MessagesResult()
    for channel_id in channel_ids:
        try:
            result.channels.append(
                _sync_channel(
                    client, archive_dir, channel_id, state.channel(channel_id), window_start
                )
            )
        except SlackChannelError as e:
            result.skipped.append(e)
            continue
        # Saved only after the channel's files are written so state never runs ahead of data
        save_state(state_path, state)
    return result


def _sync_channel(
    client: HistorySource,
    archive_dir: Path,
    channel_id: str,
    channel_state: ChannelState,
    window_start: str,
) -> ChannelResult:
    oldest = None
    if channel_state.latest_ts is not None:
        # The earlier of the two, so a long gap between runs never skips messages
        oldest = min(
            shift_ts(channel_state.latest_ts, -LOOKBACK_SECONDS), window_start, key=parse_ts
        )

    by_month: Months = defaultdict(dict)
    top_level: list[str] = []
    changed: list[tuple[str, str]] = []
    for message in client.iter_history(channel_id, oldest):
        ts = _put(by_month, channel_id, message)
        top_level.append(ts)
        latest_reply = message.get("latest_reply")
        is_root = message.get("thread_ts") == ts
        if (
            is_root
            and isinstance(latest_reply, str)
            and channel_state.threads.get(ts) != latest_reply
        ):
            changed.append((ts, latest_reply))

    result = ChannelResult(channel_id)
    fetched_threads: dict[str, str] = {}
    for root, latest_reply in changed:
        try:
            replies = _replies(client, channel_id, root)
        except SlackThreadError as e:
            # State is left alone so the thread is tried again next run
            result.skipped_threads.append(e)
            continue
        for reply in replies:
            _put(by_month, channel_id, reply)
        fetched_threads[root] = latest_reply
    result.threads = len(fetched_threads)
    result.fetched = sum(len(m) for m in by_month.values())

    _write_months(archive_dir / MESSAGES_DIR / channel_id, by_month, result)

    channel_state.threads.update(fetched_threads)
    if oldest is not None:
        # Roots before the window are never re-read, so their entries would only grow the file
        start = parse_ts(oldest)
        channel_state.threads = {
            root: reply for root, reply in channel_state.threads.items() if parse_ts(root) >= start
        }
    if top_level:
        # Replies are left out so the lookback is measured from channel messages only
        newest = max(top_level, key=parse_ts)
        current = channel_state.latest_ts
        if current is None or parse_ts(newest) > parse_ts(current):
            channel_state.latest_ts = newest
    return result


def _replies(client: HistorySource, channel_id: str, root: str) -> list[dict[str, Any]]:
    # The root and broadcast replies also come from history; keeping one source avoids churn
    return [
        m
        for m in client.iter_replies(channel_id, root)
        if m.get("ts") != root and m.get("subtype") != "thread_broadcast"
    ]


def _put(by_month: Months, channel_id: str, message: dict[str, Any]) -> str:
    ts = message.get("ts")
    try:
        month = month_of(ts)
    except ValueError:
        raise SlackError(f"{channel_id}: message without a valid ts: {ts!r}") from None
    by_month[month][ts] = message
    return ts


def _write_months(channel_dir: Path, by_month: Months, result: ChannelResult) -> None:
    for month in sorted(by_month):
        path = channel_dir / f"{month}.jsonl"
        stored = {m["ts"]: m for m in read_jsonl(path) if isinstance(m.get("ts"), str)}
        for ts, message in by_month[month].items():
            if ts not in stored:
                result.added += 1
            elif stored[ts] != message:
                result.updated += 1
            # Latest copy wins so edits and reactions inside the window are refreshed
            stored[ts] = message
        write_jsonl_if_changed(path, (stored[ts] for ts in sorted(stored, key=parse_ts)))
