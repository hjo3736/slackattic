import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from slackattic.core.errors import StorageError

REPLACE_ATTEMPTS = 5
REPLACE_RETRY_SECONDS = 0.2


def dump_json(data: Any) -> str:
    """Serialize deterministically so unchanged data gives byte-identical files."""
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise StorageError(f"{path}: cannot read file: {e.strerror}") from None
    except UnicodeDecodeError:
        raise StorageError(f"{path}: not valid UTF-8; fix or remove the file") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise StorageError(
            f"{path}: invalid JSON at line {e.lineno}, column {e.colno}; fix or remove the file"
        ) from None


def write_json_if_changed(path: Path, data: Any) -> bool:
    """Write atomically and skip the write when the content is the same."""
    text = dump_json(data)
    if path.exists() and read_text_or_none(path) == text:
        return False
    ensure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    try:
        # newline="" keeps LF on Windows so diffs stay identical across OSes
        with tmp.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    except OSError as e:
        raise StorageError(f"{path}: cannot write file: {e.strerror}") from None
    finally:
        # A leftover temp file is harmless; never let cleanup hide the real error
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    return True


def read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def ensure_dir(path: Path) -> None:
    if path.exists() and not path.is_dir():
        raise StorageError(f"{path}: expected a directory but found a file")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise StorageError(f"{path}: cannot create directory: {e.strerror}") from None


def _replace_with_retry(src: Path, dst: Path) -> None:
    # Windows refuses to replace a file another program (editor, antivirus) has open
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_RETRY_SECONDS)
