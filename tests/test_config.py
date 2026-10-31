from pathlib import Path

import pytest

from slackattic.core.config import TOKEN_ENV, Config, load_config
from slackattic.core.errors import ConfigError

TOKEN = "xoxb-test-token"
ENV = {TOKEN_ENV: TOKEN}


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    # The default config path is relative, so keep each test in its own directory
    monkeypatch.chdir(tmp_path)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_no_file():
    config = load_config(env=ENV)
    assert config == Config(token=TOKEN, archive_dir=Path("archive"), channels=())


def test_reads_default_file():
    write(Path("slackattic.toml"), 'archive_dir = "data"\nchannels = ["C1", " C2 "]\n')
    config = load_config(env=ENV)
    assert config.archive_dir == Path("data")
    assert config.channels == ("C1", "C2")


def test_reads_korean_values(tmp_path):
    path = write(tmp_path / "custom.toml", 'archive_dir = "아카이브"\n')
    assert load_config(path, env=ENV).archive_dir == Path("아카이브")


def test_explicit_missing_file_is_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml", env=ENV)


def test_invalid_toml():
    write(Path("slackattic.toml"), "archive_dir = \n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(env=ENV)


@pytest.mark.parametrize("key", ["token", "slack_bot_token", "Token"])
def test_rejects_token_in_file(key):
    write(Path("slackattic.toml"), f'{key} = "xoxb-leak"\n')
    with pytest.raises(ConfigError, match="do not put the token"):
        load_config(env=ENV)


def test_rejects_unknown_key():
    write(Path("slackattic.toml"), 'archve_dir = "x"\n')
    with pytest.raises(ConfigError, match="archve_dir"):
        load_config(env=ENV)


@pytest.mark.parametrize("text", ['archive_dir = ""\n', "archive_dir = 3\n"])
def test_rejects_bad_archive_dir(text):
    write(Path("slackattic.toml"), text)
    with pytest.raises(ConfigError, match="archive_dir"):
        load_config(env=ENV)


@pytest.mark.parametrize("text", ['channels = "C1"\n', "channels = [1]\n", 'channels = [""]\n'])
def test_rejects_bad_channels(text):
    write(Path("slackattic.toml"), text)
    with pytest.raises(ConfigError, match="channels"):
        load_config(env=ENV)


@pytest.mark.parametrize("env", [{}, {TOKEN_ENV: ""}, {TOKEN_ENV: "   "}])
def test_missing_token(env):
    with pytest.raises(ConfigError, match="not set"):
        load_config(env=env)


@pytest.mark.parametrize("token", ["xoxp-user", "xoxc-session", "abc"])
def test_rejects_non_bot_token(token):
    with pytest.raises(ConfigError, match="bot token"):
        load_config(env={TOKEN_ENV: token})


def test_strips_token_whitespace():
    assert load_config(env={TOKEN_ENV: f"  {TOKEN}\n"}).token == TOKEN


def test_repr_hides_token():
    assert TOKEN not in repr(load_config(env=ENV))


def test_field_defaults():
    config = load_config(env=ENV)
    assert config.channel_fields == ("topic", "purpose", "created")
    assert config.user_fields == ("real_name", "display_name", "avatar")


def test_custom_fields_keep_order_and_drop_duplicates():
    write(
        Path("slackattic.toml"),
        '[fields]\nusers = ["tz", "real_name", "tz"]\nchannels = []\n',
    )
    config = load_config(env=ENV)
    assert config.user_fields == ("tz", "real_name")
    assert config.channel_fields == ()


def test_unset_group_keeps_default():
    write(Path("slackattic.toml"), '[fields]\nusers = ["title"]\n')
    assert load_config(env=ENV).channel_fields == ("topic", "purpose", "created")


def test_rejects_required_field():
    write(Path("slackattic.toml"), '[fields]\nusers = ["id"]\n')
    with pytest.raises(ConfigError, match="always saved"):
        load_config(env=ENV)


def test_rejects_unknown_field_with_choices():
    write(Path("slackattic.toml"), '[fields]\nusers = ["realname"]\n')
    with pytest.raises(ConfigError, match="realname.*real_name"):
        load_config(env=ENV)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ('fields = "x"\n', "must be a table"),
        ("[fields]\nmessages = []\n", r"unknown \[fields\] key"),
        ('[fields]\nusers = "real_name"\n', "must be a list"),
        ("[fields]\nusers = [1]\n", "must be a list"),
    ],
)
def test_rejects_bad_fields_shape(text, match):
    write(Path("slackattic.toml"), text)
    with pytest.raises(ConfigError, match=match):
        load_config(env=ENV)


def test_rescan_days_defaults_to_90():
    assert load_config(env=ENV).rescan_days == 90


def test_reads_rescan_days(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("rescan_days = 14\n", encoding="utf-8")
    assert load_config(path, env=ENV).rescan_days == 14


@pytest.mark.parametrize("value", ["0", "-1", "true", '"30"', "1.5"])
def test_rejects_bad_rescan_days(tmp_path, value):
    path = tmp_path / "c.toml"
    path.write_text(f"rescan_days = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="rescan_days"):
        load_config(path, env=ENV)
