import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from slackattic.core.errors import ConfigError
from slackattic.core.fields import (
    CHANNEL_OPTIONAL,
    CHANNEL_REQUIRED,
    DEFAULT_CHANNEL_FIELDS,
    DEFAULT_USER_FIELDS,
    USER_OPTIONAL,
    USER_REQUIRED,
)

TOKEN_ENV = "SLACK_BOT_TOKEN"
BOT_TOKEN_PREFIX = "xoxb-"
DEFAULT_CONFIG_PATH = Path("slackattic.toml")
DEFAULT_ARCHIVE_DIR = Path("archive")
_ALLOWED_KEYS = frozenset({"archive_dir", "channels", "fields"})
_FIELD_GROUPS = {
    "channels": (CHANNEL_REQUIRED, CHANNEL_OPTIONAL, DEFAULT_CHANNEL_FIELDS),
    "users": (USER_REQUIRED, USER_OPTIONAL, DEFAULT_USER_FIELDS),
}


@dataclass(frozen=True)
class Config:
    # Kept out of repr so the token never shows up in logs or tracebacks
    token: str = field(repr=False)
    archive_dir: Path = DEFAULT_ARCHIVE_DIR
    # Empty means every channel the bot has joined
    channels: tuple[str, ...] = ()
    channel_fields: tuple[str, ...] = DEFAULT_CHANNEL_FIELDS
    user_fields: tuple[str, ...] = DEFAULT_USER_FIELDS


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Build the config from the TOML file and the token environment variable.

    A missing default file means defaults; a missing explicit path is an error.
    """
    env = os.environ if env is None else env
    data = _read_file(path)
    _check_keys(data)
    fields = _read_fields(data)
    return Config(
        token=_read_token(env),
        archive_dir=_read_archive_dir(data),
        channels=_read_channels(data),
        channel_fields=fields["channels"],
        user_fields=fields["users"],
    )


def _read_file(path: Path | None) -> dict[str, object]:
    explicit = path is not None
    path = DEFAULT_CONFIG_PATH if path is None else path
    if not path.exists():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None
    except OSError as e:
        raise ConfigError(f"{path}: cannot read file: {e.strerror}") from None


def _check_keys(data: Mapping[str, object]) -> None:
    if any("token" in key.lower() for key in data):
        raise ConfigError(
            f"do not put the token in the config file; set the {TOKEN_ENV} environment variable"
        )
    unknown = sorted(set(data) - _ALLOWED_KEYS)
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_KEYS))
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)} (allowed: {allowed})")


def _read_token(env: Mapping[str, str]) -> str:
    token = env.get(TOKEN_ENV, "").strip()
    if not token:
        raise ConfigError(
            f"{TOKEN_ENV} is not set. If you just set it, open a new terminal and try again."
        )
    if not token.startswith(BOT_TOKEN_PREFIX):
        raise ConfigError(f"{TOKEN_ENV} must be a bot token starting with '{BOT_TOKEN_PREFIX}'")
    return token


def _read_archive_dir(data: Mapping[str, object]) -> Path:
    value = data.get("archive_dir", str(DEFAULT_ARCHIVE_DIR))
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("archive_dir must be a non-empty string")
    return Path(value)


def _read_channels(data: Mapping[str, object]) -> tuple[str, ...]:
    value = data.get("channels", [])
    if not isinstance(value, list) or not all(isinstance(c, str) and c.strip() for c in value):
        raise ConfigError('channels must be a list of channel IDs, e.g. ["C0123456789"]')
    return tuple(c.strip() for c in value)


def _read_fields(data: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    table = data.get("fields", {})
    if not isinstance(table, dict):
        raise ConfigError('[fields] must be a table, e.g. [fields] users = ["real_name"]')
    unknown = sorted(set(table) - set(_FIELD_GROUPS))
    if unknown:
        raise ConfigError(
            f"unknown [fields] key(s): {', '.join(unknown)} (allowed: channels, users)"
        )
    return {group: _read_field_list(group, table) for group in _FIELD_GROUPS}


def _read_field_list(group: str, table: Mapping[str, object]) -> tuple[str, ...]:
    required, optional, default = _FIELD_GROUPS[group]
    if group not in table:
        return default
    value = table[group]
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ConfigError(f"fields.{group} must be a list of field names")
    for name in value:
        if name in required:
            raise ConfigError(f"fields.{group}: '{name}' is always saved; remove it from the list")
        if name not in optional:
            choices = ", ".join(optional)
            raise ConfigError(f"fields.{group}: unknown field '{name}' (choose from: {choices})")
    # Keep the first occurrence so duplicates do not change the output
    return tuple(dict.fromkeys(value))
