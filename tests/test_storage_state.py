import json

import pytest

from slackattic.core.errors import StorageError
from slackattic.storage.jsonl import read_jsonl, write_jsonl_if_changed
from slackattic.storage.state import ChannelState, State, load_state, save_state


def test_missing_state_is_empty(tmp_path):
    assert load_state(tmp_path / "state.json") == State()


def test_state_round_trip_and_stable(tmp_path):
    path = tmp_path / "state.json"
    state = State({"C1": ChannelState("1.000001", {"2.000000": "3.000000"}), "C2": ChannelState()})
    assert save_state(path, state) is True
    assert load_state(path) == state
    assert save_state(path, load_state(path)) is False
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "version": 1,
        "channels": {
            "C1": {"latest_ts": "1.000001", "threads": {"2.000000": "3.000000"}},
            "C2": {"threads": {}},
        },
    }


@pytest.mark.parametrize(
    "text",
    [
        '{"version": 2, "channels": {}}',
        "[]",
        '{"version": 1, "channels": []}',
        '{"version": 1, "channels": {"C1": {"latest_ts": "abc"}}}',
        '{"version": 1, "channels": {"C1": {"threads": []}}}',
        '{"version": 1, "channels": {"C1": "x"}}',
    ],
)
def test_invalid_state_is_rejected(tmp_path, text):
    path = tmp_path / "state.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(StorageError):
        load_state(path)


def test_jsonl_round_trip_compact_utf8_lf(tmp_path):
    path = tmp_path / "m" / "2026-10.jsonl"
    items = [{"ts": "1.0", "text": "회의록"}, {"ts": "2.0", "text": "b"}]
    assert write_jsonl_if_changed(path, items) is True
    assert path.read_bytes().decode("utf-8") == (
        '{"text":"회의록","ts":"1.0"}\n{"text":"b","ts":"2.0"}\n'
    )
    assert read_jsonl(path) == items
    assert write_jsonl_if_changed(path, items) is False


def test_jsonl_reports_bad_line(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text('{"ts": "1"}\n\n{bad\n', encoding="utf-8")
    with pytest.raises(StorageError, match="line 3"):
        read_jsonl(path)


def test_jsonl_rejects_non_object(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text("[1]\n", encoding="utf-8")
    with pytest.raises(StorageError, match="not a JSON object"):
        read_jsonl(path)
