import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from slackattic.core.errors import ConfigError

TOKEN_ENV = "SLACK_BOT_TOKEN"
BOT_TOKEN_PREFIX = "xoxb-"
DEFAULT_CONFIG_PATH = Path("slackattic.toml")
DEFAULT_ARCHIVE_DIR = Path("archive")
_ALLOWED_KEYS = frozenset({"archive_dir", "channels"})


@dataclass(frozen=True)
class Config:
    # Kept out of repr so the token never shows up in logs or tracebacks
    token: str = field(repr=False)
    archive_dir: Path = DEFAULT_ARCHIVE_DIR
    # Empty means every channel the bot has joined
    channels: tuple[str, ...] = ()


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Build the config from the TOML file and the token environment variable.

    A missing default file means defaults; a missing explicit path is an error.
    """
    env = os.environ if env is None else env
    data = _read_file(path)
    _check_keys(data)
    return Config(
        token=_read_token(env),
        archive_dir=_read_archive_dir(data),
        channels=_read_channels(data),
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
