import re
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
# Added to a stored message once Slack stops returning it; value is the UTC detection time
DELETED_KEY = "slackattic_deleted"
MONTH_RE = re.compile(r"\d{4}-\d{2}")


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
    # Stored messages newly marked as deleted
    deleted: int = 0
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
    detected_at = datetime.fromtimestamp(int(clock), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path = archive_dir / STATE_FILE
    state = load_state(state_path)
    result = MessagesResult()
    for channel_id in channel_ids:
        try:
            result.channels.append(
                _sync_channel(
                    client,
                    archive_dir,
                    channel_id,
                    state.channel(channel_id),
                    window_start,
                    detected_at,
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
    detected_at: str,
) -> ChannelResult:
    oldest = None
    if channel_state.latest_ts is not None:
        # The earlier of the two, so a long gap between runs never skips messages
        oldest = min(
            shift_ts(channel_state.latest_ts, -LOOKBACK_SECONDS), window_start, key=parse_ts
        )

    files = _MonthFiles(archive_dir / MESSAGES_DIR / channel_id)
    by_month: Months = defaultdict(dict)
    top_level: list[str] = []
    changed: list[tuple[str, str]] = []
    # Thread root -> reply ts Slack returned this run; only these threads are judged for deletions
    seen_replies: dict[str, set[str]] = {}
    for message in client.iter_history(channel_id, oldest):
        ts = _put(by_month, channel_id, message)
        top_level.append(ts)
        stored = files.message(ts)
        had_replies = stored is not None and _reply_count(stored) > 0
        latest_reply = message.get("latest_reply") if message.get("thread_ts") == ts else None
        if isinstance(latest_reply, str):
            # A lower count means a reply was deleted even if latest_reply did not move
            shrank = had_replies and _reply_count(message) < _reply_count(stored)
            if channel_state.threads.get(ts) != latest_reply or shrank:
                changed.append((ts, latest_reply))
        elif had_replies:
            # Every reply is gone, so there is nothing to fetch
            seen_replies[ts] = set()

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
        seen_replies[root] = {r["ts"] for r in replies}
    result.threads = len(fetched_threads)
    result.fetched = sum(len(m) for m in by_month.values())

    months = set(by_month)
    if oldest is not None:
        # Months with nothing returned this run may still hold messages that were deleted
        months |= files.months_since(month_of(oldest))
    seen = _Seen(oldest, set(top_level), seen_replies)
    deleted_roots = _write_months(files, sorted(months), by_month, seen, detected_at, result)

    channel_state.threads.update(fetched_threads)
    for root in deleted_roots | {r for r, ts in seen_replies.items() if not ts}:
        channel_state.threads.pop(root, None)
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


class _MonthFiles:
    """One channel's stored month files, each read at most once per run."""

    def __init__(self, channel_dir: Path) -> None:
        self.dir = channel_dir
        self._months: Months = {}

    def path(self, month: str) -> Path:
        return self.dir / f"{month}.jsonl"

    def get(self, month: str) -> dict[str, dict[str, Any]]:
        if month not in self._months:
            rows = read_jsonl(self.path(month))
            self._months[month] = {m["ts"]: m for m in rows if isinstance(m.get("ts"), str)}
        return self._months[month]

    def message(self, ts: str) -> dict[str, Any] | None:
        return self.get(month_of(ts)).get(ts)

    def months_since(self, first: str) -> set[str]:
        # YYYY-MM names compare correctly as strings
        return {
            p.stem
            for p in self.dir.glob("*.jsonl")
            if MONTH_RE.fullmatch(p.stem) and p.stem >= first
        }


@dataclass
class _Seen:
    """What Slack returned this run, used to decide which stored messages were deleted."""

    oldest: str | None
    top_level: set[str]
    replies: dict[str, set[str]]

    def is_gone(self, ts: str, message: dict[str, Any]) -> bool:
        # Nothing is judged on a first run or at or before the window start (oldest is exclusive)
        if self.oldest is None or not _after(ts, self.oldest):
            return False
        if _is_top_level(message):
            return ts not in self.top_level
        root = message.get("thread_ts")
        return root in self.replies and ts not in self.replies[root]


def _write_months(
    files: _MonthFiles,
    months: list[str],
    by_month: Months,
    seen: _Seen,
    detected_at: str,
    result: ChannelResult,
) -> set[str]:
    deleted_roots: set[str] = set()
    for month in months:
        stored = files.get(month)
        for ts, message in by_month.get(month, {}).items():
            if ts not in stored:
                result.added += 1
            elif stored[ts] != message:
                result.updated += 1
            # Latest copy wins, which also clears the deleted mark of a message that came back
            stored[ts] = message
        for ts, message in list(stored.items()):
            if DELETED_KEY in message or not seen.is_gone(ts, message):
                continue
            # The first detection time is kept so reruns do not change the file
            stored[ts] = {**message, DELETED_KEY: detected_at}
            result.deleted += 1
            if message.get("thread_ts") == ts:
                deleted_roots.add(ts)
        write_jsonl_if_changed(
            files.path(month), (stored[ts] for ts in sorted(stored, key=parse_ts))
        )
    return deleted_roots


def _is_top_level(message: dict[str, Any]) -> bool:
    thread_ts = message.get("thread_ts")
    return thread_ts in (None, message.get("ts")) or message.get("subtype") == "thread_broadcast"


def _reply_count(message: dict[str, Any]) -> int:
    count = message.get("reply_count")
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _after(ts: str, oldest: str) -> bool:
    try:
        return parse_ts(ts) > parse_ts(oldest)
    except ValueError:
        # A stored row with a broken ts is left untouched rather than marked
        return False
