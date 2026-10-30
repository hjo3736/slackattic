from pathlib import Path
from typing import Any

import pytest

from slackattic.core.errors import StorageError
from slackattic.slack.errors import SlackChannelError, SlackError, SlackTransientError
from slackattic.storage.jsonl import read_jsonl
from slackattic.storage.state import load_state
from slackattic.sync.messages import LOOKBACK_SECONDS, sync_messages

OCT = "1793300000.000100"  # 2026-10 in UTC
NOV = "1793500000.000200"  # 2026-11 in UTC


def msg(ts: str, text: str = "hi", **extra: Any) -> dict[str, Any]:
    return {"type": "message", "ts": ts, "user": "U1", "text": text, **extra}


class FakeHistory:
    """Serves each channel's messages newest first and filters by oldest like Slack."""

    def __init__(self, channels: dict[str, list[Any]]) -> None:
        self.channels = channels
        self.calls: list[tuple[str, str | None]] = []

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
    sync_messages(source, tmp_path, ["C1"])
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
