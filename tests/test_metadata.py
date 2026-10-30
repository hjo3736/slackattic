from pathlib import Path
from typing import Any

import pytest

from slackattic.core.config import Config
from slackattic.core.errors import StorageError
from slackattic.slack.errors import SlackTransientError
from slackattic.storage.files import read_json
from slackattic.sync.metadata import sync_metadata


def raw_channel(cid: str, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "name": name,
        "is_private": False,
        "is_archived": False,
        "is_member": True,
        "created": 1700000000,
        "num_members": 3,
        "topic": {"value": "주제", "creator": "U1"},
        "purpose": {"value": ""},
        **extra,
    }


def raw_user(uid: str, name: str, **extra: Any) -> dict[str, Any]:
    profile = {"real_name": "홍길동", "display_name": "길동", "image_72": "a.png", "phone": "010"}
    return {
        "id": uid,
        "name": name,
        "is_bot": False,
        "deleted": False,
        "updated": 1700000000,
        "profile": profile,
        **extra,
    }


class FakeSource:
    def __init__(self, channels: list[dict[str, Any]], users: list[Any]) -> None:
        self.channels = channels
        self.users = users

    def iter_channels(self):
        yield from self.channels

    def iter_users(self):
        for user in self.users:
            if isinstance(user, Exception):
                raise user
            yield user


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    return Config(token="xoxb-x", archive_dir=tmp_path / "archive", **overrides)


def test_saves_selected_fields_only(tmp_path):
    config = make_config(tmp_path)
    source = FakeSource([raw_channel("C2", "공개-채널")], [raw_user("U1", "gildong")])
    sync_metadata(source, config)
    channels = read_json(config.archive_dir / "channels.json", None)
    users = read_json(config.archive_dir / "users.json", None)
    assert channels == [
        {
            "id": "C2",
            "name": "공개-채널",
            "is_private": False,
            "is_archived": False,
            "is_member": True,
            "topic": "주제",
            "purpose": "",
            "created": 1700000000,
        }
    ]
    assert users == [
        {
            "id": "U1",
            "name": "gildong",
            "is_bot": False,
            "deleted": False,
            "real_name": "홍길동",
            "display_name": "길동",
            "avatar": "a.png",
        }
    ]


def test_disabled_fields_are_not_saved(tmp_path):
    config = make_config(tmp_path, channel_fields=(), user_fields=("tz",))
    source = FakeSource([raw_channel("C1", "a")], [raw_user("U1", "u", tz="Asia/Seoul")])
    sync_metadata(source, config)
    users = read_json(config.archive_dir / "users.json", None)
    assert users[0]["tz"] == "Asia/Seoul"
    assert "real_name" not in users[0]
    assert "topic" not in read_json(config.archive_dir / "channels.json", None)[0]


def test_second_run_without_changes_writes_nothing(tmp_path):
    config = make_config(tmp_path)
    source = FakeSource([raw_channel("C1", "a")], [raw_user("U1", "u")])
    first = sync_metadata(source, config)
    second = sync_metadata(source, config)
    assert (first.channels_changed, first.users_changed) == (True, True)
    assert (second.channels_changed, second.users_changed) == (False, False)


def test_vanished_channel_is_kept_as_not_member(tmp_path):
    config = make_config(tmp_path)
    sync_metadata(FakeSource([raw_channel("C1", "a"), raw_channel("C2", "b")], []), config)
    result = sync_metadata(FakeSource([raw_channel("C1", "a")], []), config)
    channels = read_json(config.archive_dir / "channels.json", None)
    assert [c["id"] for c in channels] == ["C1", "C2"]
    assert channels[1]["is_member"] is False
    assert channels[1]["name"] == "b"
    assert [c["id"] for c in result.selected] == ["C1"]


def test_kept_channel_drops_newly_disabled_fields(tmp_path):
    sync_metadata(FakeSource([raw_channel("C2", "b")], []), make_config(tmp_path))
    config = make_config(tmp_path, channel_fields=("created",))
    sync_metadata(FakeSource([], []), config)
    kept = read_json(config.archive_dir / "channels.json", None)[0]
    assert "topic" not in kept
    assert kept["created"] == 1700000000


def test_selection_skips_non_member_and_archived(tmp_path):
    channels = [
        raw_channel("C1", "joined"),
        raw_channel("C2", "outside", is_member=False),
        raw_channel("C3", "old", is_archived=True),
    ]
    result = sync_metadata(FakeSource(channels, []), make_config(tmp_path))
    assert [c["id"] for c in result.selected] == ["C1"]


def test_configured_channels_filter_and_report_missing(tmp_path):
    channels = [
        raw_channel("C1", "a"),
        raw_channel("C2", "b"),
        raw_channel("C3", "c", is_member=False),
    ]
    config = make_config(tmp_path, channels=("C2", "C3", "C9"))
    result = sync_metadata(FakeSource(channels, []), config)
    assert [c["id"] for c in result.selected] == ["C2"]
    assert result.missing == ["C3", "C9"]


def test_api_failure_writes_nothing(tmp_path):
    config = make_config(tmp_path)
    users = [raw_user("U1", "u"), SlackTransientError("x")]
    with pytest.raises(SlackTransientError):
        sync_metadata(FakeSource([raw_channel("C1", "a")], users), config)
    assert not config.archive_dir.exists()


@pytest.mark.parametrize("text", ["{not json", '{"id": "C1"}', '[{"name": "no id"}]'])
def test_corrupt_previous_file_is_not_overwritten(tmp_path, text):
    config = make_config(tmp_path)
    config.archive_dir.mkdir()
    path = config.archive_dir / "channels.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(StorageError):
        sync_metadata(FakeSource([raw_channel("C1", "a")], []), config)
    assert path.read_text(encoding="utf-8") == text
