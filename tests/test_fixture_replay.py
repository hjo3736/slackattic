import re

import pytest

from fixture_web import WORKSPACE_FIXTURE, ReplayWeb
from slackattic.core.config import Config
from slackattic.slack.client import SlackClient
from slackattic.sync.messages import sync_messages
from slackattic.sync.metadata import sync_metadata

FAKE_ID = re.compile(r"^[A-Z]\d{10}$")


def run_sync(archive):
    web = ReplayWeb()
    client = SlackClient("xoxb-test", web_client=web, page_size=web.page_size)
    config = Config(token="xoxb-test", archive_dir=archive)
    meta = sync_metadata(client, config)
    result = sync_messages(client, archive, [c["id"] for c in meta.selected])
    return web, meta, result


def snapshot(archive):
    return {p.relative_to(archive).as_posix(): p.read_bytes() for p in archive.rglob("*.*")}


def test_replayed_sync_archives_every_joined_channel_across_pages(tmp_path):
    web, meta, result = run_sync(tmp_path)
    assert not result.skipped
    assert len(result.channels) == len(meta.selected) >= 1
    assert all(ch.fetched > 0 for ch in result.channels)
    assert any(m == "conversations.history" and "cursor" in p for m, p in web.calls)


def test_second_replayed_sync_changes_nothing(tmp_path):
    run_sync(tmp_path)
    before = snapshot(tmp_path)
    _, meta, result = run_sync(tmp_path)
    assert not meta.channels_changed and not meta.users_changed
    assert all(ch.added == 0 and ch.updated == 0 for ch in result.channels)
    assert snapshot(tmp_path) == before


def test_unrecorded_request_fails_loudly():
    with pytest.raises(AssertionError, match="no recorded response"):
        ReplayWeb().api_call("chat.postMessage", http_verb="POST", params={})


def test_fixture_holds_only_fake_ids_and_no_tokens():
    text = WORKSPACE_FIXTURE.read_text(encoding="utf-8")
    assert "xox" not in text
    ids = set(re.findall(r"\b(?=[A-Z0-9]*\d)[ABCDEFGSTUW][A-Z0-9]{8,}\b", text))
    assert ids and all(FAKE_ID.match(i) for i in ids)
