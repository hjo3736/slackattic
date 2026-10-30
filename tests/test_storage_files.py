import os

import pytest

from slackattic.core.errors import StorageError
from slackattic.storage import files
from slackattic.storage.files import read_json, write_json_if_changed


def test_writes_utf8_lf_sorted_and_creates_dirs(tmp_path):
    path = tmp_path / "a" / "b" / "data.json"
    assert write_json_if_changed(path, {"name": "공개-채널", "id": "C1"}) is True
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8") == '{\n  "id": "C1",\n  "name": "공개-채널"\n}\n'


def test_skips_unchanged_content(tmp_path):
    path = tmp_path / "data.json"
    write_json_if_changed(path, [1, 2])
    os.utime(path, (1, 1))
    assert write_json_if_changed(path, [1, 2]) is False
    assert path.stat().st_mtime == 1


def test_rewrites_changed_content(tmp_path):
    path = tmp_path / "data.json"
    write_json_if_changed(path, [1])
    assert write_json_if_changed(path, [2]) is True
    assert read_json(path, default=None) == [2]


def test_read_missing_returns_default(tmp_path):
    assert read_json(tmp_path / "none.json", default=[]) == []


def test_invalid_json_reports_location(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('[\n  {"id": }\n]', encoding="utf-8")
    with pytest.raises(StorageError, match="line 2"):
        read_json(path, default=[])


def test_directory_path_that_is_a_file(tmp_path):
    blocker = tmp_path / "archive"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(StorageError, match="expected a directory"):
        write_json_if_changed(blocker / "data.json", [])


def test_retries_locked_file_then_succeeds(tmp_path, monkeypatch):
    real_replace = os.replace
    failures = iter([PermissionError(), PermissionError()])

    def flaky(src, dst):
        error = next(failures, None)
        if error:
            raise error
        real_replace(src, dst)

    monkeypatch.setattr(files.os, "replace", flaky)
    monkeypatch.setattr(files, "REPLACE_RETRY_SECONDS", 0)
    path = tmp_path / "data.json"
    assert write_json_if_changed(path, [1]) is True
    assert read_json(path, default=None) == [1]


def test_gives_up_on_locked_file_and_cleans_temp(tmp_path, monkeypatch):
    def locked(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(files.os, "replace", locked)
    monkeypatch.setattr(files, "REPLACE_RETRY_SECONDS", 0)
    path = tmp_path / "data.json"
    with pytest.raises(StorageError, match="cannot write"):
        write_json_if_changed(path, [1])
    assert list(tmp_path.iterdir()) == []
