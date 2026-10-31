import json
from pathlib import Path
from typing import Any

import pytest

from slackattic.core.errors import StorageError
from slackattic.slack.errors import (
    SlackChannelError,
    SlackError,
    SlackThreadError,
    SlackTransientError,
)
from slackattic.storage.jsonl import read_jsonl
from slackattic.storage.state import load_state
from slackattic.sync.messages import DAY_SECONDS as DAY
from slackattic.sync.messages import LOOKBACK_SECONDS, sync_messages

OCT = "1793300000.000100"  # 2026-10 in UTC
NOV = "1793500000.000200"  # 2026-11 in UTC
# Far enough after NOV that the 90-day rescan window starts later than the lookback
LATE = float(NOV) + 200 * DAY


def msg(ts: str, text: str = "hi", **extra: Any) -> dict[str, Any]:
    return {"type": "message", "ts": ts, "user": "U1", "text": text, **extra}


class FakeHistory:
    """Serves each channel's messages newest first and filters by oldest like Slack."""

    def __init__(
        self, channels: dict[str, list[Any]], threads: dict[tuple[str, str], Any] | None = None
    ) -> None:
        self.channels = channels
        self.threads = threads or {}
        self.calls: list[tuple[str, str | None]] = []
        self.reply_calls: list[tuple[str, str]] = []

    def iter_history(self, channel: str, oldest: str | None = None):
        self.calls.append((channel, oldest))
        items = self.channels[channel]
        if isinstance(items, Exception):
            raise items
        for item in reversed(items):
            if isinstance(item, Exception):
                raise item
            if oldest is None or float(item["ts"]) > float(oldest):
                yield item

    def iter_replies(self, channel: str, thread_ts: str):
        self.reply_calls.append((channel, thread_ts))
        items = self.threads[(channel, thread_ts)]
        if isinstance(items, Exception):
            raise items
        yield from items


def files_of(archive: Path, channel: str) -> dict[str, list[dict[str, Any]]]:
    folder = archive / "messages" / channel
    return {p.stem: read_jsonl(p) for p in sorted(folder.glob("*.jsonl"))}


def test_first_run_fetches_everything_split_by_utc_month(tmp_path):
    source = FakeHistory({"C1": [msg(OCT, "회의를 합시다"), msg(NOV)]})
    result = sync_messages(source, tmp_path, ["C1"])
    assert source.calls == [("C1", None)]
    assert result.channels[0].added == 2
    files = files_of(tmp_path, "C1")
    assert [m["ts"] for m in files["2026-10"]] == [OCT]
    assert files["2026-10"][0]["text"] == "회의를 합시다"
    assert [m["ts"] for m in files["2026-11"]] == [NOV]
    assert load_state(tmp_path / "state.json").channels["C1"].latest_ts == NOV


def test_next_run_starts_one_lookback_before_latest(tmp_path):
    source = FakeHistory({"C1": [msg(OCT)]})
    sync_messages(source, tmp_path, ["C1"])
    sync_messages(source, tmp_path, ["C1"], now=LATE)
    expected = f"{float(OCT) - LOOKBACK_SECONDS:.6f}"
    assert source.calls[1] == ("C1", expected)


def test_rerun_is_idempotent(tmp_path):
    source = FakeHistory({"C1": [msg(OCT), msg(NOV)]})
    sync_messages(source, tmp_path, ["C1"])
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = sync_messages(source, tmp_path, ["C1"])
    assert (result.channels[0].added, result.channels[0].updated) == (0, 0)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == snapshot


def test_overlap_updates_edited_message_and_adds_new(tmp_path):
    sync_messages(FakeHistory({"C1": [msg(NOV, "before")]}), tmp_path, ["C1"])
    later = f"{float(NOV) + 60:.6f}"
    source = FakeHistory({"C1": [msg(NOV, "after", reactions=[{"name": "+1"}]), msg(later)]})
    result = sync_messages(source, tmp_path, ["C1"]).channels[0]
    assert (result.added, result.updated) == (1, 1)
    stored = files_of(tmp_path, "C1")["2026-11"]
    assert [m["text"] for m in stored] == ["after", "hi"]
    assert load_state(tmp_path / "state.json").channels["C1"].latest_ts == later


def test_messages_outside_overlap_are_kept(tmp_path):
    sync_messages(FakeHistory({"C1": [msg(OCT), msg(NOV)]}), tmp_path, ["C1"])
    # Slack no longer returns the October message (e.g. past 90 days)
    sync_messages(FakeHistory({"C1": [msg(NOV)]}), tmp_path, ["C1"])
    assert [m["ts"] for m in files_of(tmp_path, "C1")["2026-10"]] == [OCT]


def test_system_messages_are_stored(tmp_path):
    join = msg(OCT, "<@U2> has joined the channel", subtype="channel_join")
    sync_messages(FakeHistory({"C1": [join]}), tmp_path, ["C1"])
    assert files_of(tmp_path, "C1")["2026-10"][0]["subtype"] == "channel_join"


def test_empty_channel_keeps_no_latest(tmp_path):
    sync_messages(FakeHistory({"C1": []}), tmp_path, ["C1"])
    assert load_state(tmp_path / "state.json").channels["C1"].latest_ts is None


def test_channel_error_skips_only_that_channel(tmp_path):
    source = FakeHistory(
        {"C1": SlackChannelError("not_in_channel", "not_in_channel", "C1"), "C2": [msg(OCT)]}
    )
    result = sync_messages(source, tmp_path, ["C1", "C2"])
    assert [e.channel for e in result.skipped] == ["C1"]
    state = load_state(tmp_path / "state.json")
    assert state.channels["C1"].latest_ts is None
    assert state.channels["C2"].latest_ts == OCT


def test_transient_error_keeps_finished_channels_and_old_state(tmp_path):
    sync_messages(FakeHistory({"C2": [msg(OCT)]}), tmp_path, ["C2"])
    source = FakeHistory({"C1": [msg(OCT)], "C2": [msg(NOV), SlackTransientError("x")]})
    with pytest.raises(SlackTransientError):
        sync_messages(source, tmp_path, ["C1", "C2"])
    state = load_state(tmp_path / "state.json")
    assert state.channels["C1"].latest_ts == OCT
    assert state.channels["C2"].latest_ts == OCT
    assert "2026-11" not in files_of(tmp_path, "C2")


def test_thread_state_is_preserved(tmp_path):
    (tmp_path / "state.json").write_text(
        '{"version": 1, "channels": {"C1": {"threads": {"1.000000": "2.000000"}}}}',
        encoding="utf-8",
    )
    sync_messages(FakeHistory({"C1": [msg(OCT)]}), tmp_path, ["C1"])
    assert load_state(tmp_path / "state.json").channels["C1"].threads == {"1.000000": "2.000000"}


def test_message_without_ts_stops_run(tmp_path):
    source = FakeHistory({"C1": [{"type": "message", "text": "x", "ts": "bad"}]})
    with pytest.raises(SlackError, match="valid ts"):
        sync_messages(source, tmp_path, ["C1"])
    assert not (tmp_path / "state.json").exists()


def test_corrupt_month_file_is_not_overwritten(tmp_path):
    path = tmp_path / "messages" / "C1" / "2026-10.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{broken\n", encoding="utf-8")
    with pytest.raises(StorageError, match="line 1"):
        sync_messages(FakeHistory({"C1": [msg(OCT)]}), tmp_path, ["C1"])
    assert path.read_text(encoding="utf-8") == "{broken\n"


def ts_after(base: str, seconds: float) -> str:
    return f"{float(base) + seconds:.6f}"


R1 = ts_after(OCT, 60)
R2 = ts_after(OCT, 120)


def root(ts: str, latest_reply: str, **extra: Any) -> dict[str, Any]:
    fields = {"thread_ts": ts, "reply_count": 1, "latest_reply": latest_reply, **extra}
    return msg(ts, extra.pop("text", "root"), **{k: v for k, v in fields.items() if k != "text"})


def reply(ts: str, parent: str = OCT, **extra: Any) -> dict[str, Any]:
    return msg(ts, "reply", thread_ts=parent, **extra)


def test_replies_are_stored_without_duplicating_root_or_broadcasts(tmp_path):
    broadcast = reply(R2, subtype="thread_broadcast")
    source = FakeHistory(
        {"C1": [root(OCT, R2), broadcast]},
        {("C1", OCT): [root(OCT, R2, text="root from replies"), reply(R1), broadcast]},
    )
    result = sync_messages(source, tmp_path, ["C1"]).channels[0]
    assert source.reply_calls == [("C1", OCT)]
    assert (result.threads, result.added) == (1, 3)
    stored = files_of(tmp_path, "C1")["2026-10"]
    assert [m["ts"] for m in stored] == [OCT, R1, R2]
    # History keeps ownership of the root, so its copy is the one stored
    assert stored[0]["text"] == "root"
    state = load_state(tmp_path / "state.json").channels["C1"]
    assert state.threads == {OCT: R2}


def test_latest_ts_ignores_replies(tmp_path):
    later_reply = ts_after(NOV, 60)
    source = FakeHistory(
        {"C1": [root(OCT, later_reply)]},
        {("C1", OCT): [root(OCT, later_reply), reply(later_reply)]},
    )
    sync_messages(source, tmp_path, ["C1"])
    assert load_state(tmp_path / "state.json").channels["C1"].latest_ts == OCT
    assert [m["ts"] for m in files_of(tmp_path, "C1")["2026-11"]] == [later_reply]


def test_unchanged_thread_is_not_fetched_again(tmp_path):
    threads = {("C1", OCT): [root(OCT, R1), reply(R1)]}
    source = FakeHistory({"C1": [root(OCT, R1)]}, threads)
    sync_messages(source, tmp_path, ["C1"])
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = sync_messages(source, tmp_path, ["C1"]).channels[0]
    assert source.reply_calls == [("C1", OCT)]
    assert (result.threads, result.added, result.updated) == (0, 0, 0)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == snapshot


def test_new_reply_on_old_root_inside_window_is_fetched(tmp_path):
    sync_messages(
        FakeHistory({"C1": [root(OCT, R1)]}, {("C1", OCT): [reply(R1)]}), tmp_path, ["C1"]
    )
    # A month later the root is far outside the 1-day lookback but inside the 90-day window
    now = float(OCT) + 30 * DAY
    source = FakeHistory(
        {"C1": [root(OCT, R2, reply_count=2)]}, {("C1", OCT): [reply(R1), reply(R2)]}
    )
    result = sync_messages(source, tmp_path, ["C1"], now=now).channels[0]
    assert source.calls == [("C1", f"{now - 90 * DAY:.6f}")]
    assert (result.threads, result.added) == (1, 1)
    assert [m["ts"] for m in files_of(tmp_path, "C1")["2026-10"]] == [OCT, R1, R2]


def test_thread_error_skips_only_that_thread_and_retries_next_run(tmp_path):
    other = ts_after(OCT, 600)
    error = SlackThreadError("thread_not_found", "thread_not_found", "C1", OCT)
    threads = {("C1", OCT): error, ("C1", other): [reply(ts_after(other, 1), other)]}
    source = FakeHistory({"C1": [root(OCT, R1), root(other, ts_after(other, 1))]}, threads)
    result = sync_messages(source, tmp_path, ["C1"]).channels[0]
    assert [e.thread_ts for e in result.skipped_threads] == [OCT]
    assert load_state(tmp_path / "state.json").channels["C1"].threads == {other: ts_after(other, 1)}
    threads[("C1", OCT)] = [reply(R1)]
    sync_messages(source, tmp_path, ["C1"])
    assert ("C1", OCT) in source.reply_calls[-1:]


def test_threads_before_the_window_are_dropped_from_state(tmp_path):
    threads = {"1.000000": "2.000000", NOV: NOV}
    state = {"version": 1, "channels": {"C1": {"latest_ts": NOV, "threads": threads}}}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    sync_messages(FakeHistory({"C1": [msg(NOV)]}), tmp_path, ["C1"], now=float(NOV) + DAY)
    assert load_state(tmp_path / "state.json").channels["C1"].threads == {NOV: NOV}


def test_long_gap_between_runs_starts_from_lookback_not_window(tmp_path):
    sync_messages(FakeHistory({"C1": [msg(OCT)]}), tmp_path, ["C1"])
    source = FakeHistory({"C1": [msg(OCT)]})
    sync_messages(source, tmp_path, ["C1"], rescan_days=1, now=float(OCT) + 10 * DAY)
    assert source.calls == [("C1", f"{float(OCT) - LOOKBACK_SECONDS:.6f}")]


def test_rescan_days_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        sync_messages(FakeHistory({}), tmp_path, [], rescan_days=0)
