import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from slackattic.core.errors import StorageError
from slackattic.storage.files import write_text_if_changed


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    raise StorageError(f"{path}: invalid JSON on line {lineno}") from None
                if not isinstance(item, dict):
                    raise StorageError(f"{path}: line {lineno} is not a JSON object")
                items.append(item)
    except OSError as e:
        raise StorageError(f"{path}: cannot read file: {e.strerror}") from None
    except UnicodeDecodeError:
        raise StorageError(f"{path}: not valid UTF-8") from None
    return items


def write_jsonl_if_changed(path: Path, items: Iterable[dict[str, Any]]) -> bool:
    lines = (
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for item in items
    )
    return write_text_if_changed(path, "".join(line + "\n" for line in lines))
