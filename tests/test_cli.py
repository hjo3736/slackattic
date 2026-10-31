import pytest
from typer.testing import CliRunner

from slackattic.cli import app as app_module
from slackattic.cli.app import app
from slackattic.core.config import TOKEN_ENV
from slackattic.core.errors import ExitCode
from slackattic.slack.errors import SlackAuthError

TOKEN = "xoxb-secret-value"
runner = CliRunner()


class FakeSlackClient:
    error: Exception | None = None

    def __init__(self, token: str) -> None:
        self.token = token

    def auth_test(self) -> dict[str, str]:
        if self.error:
            raise self.error
        return {"team": "Test", "url": "https://test.slack.com/", "user": "slackattic"}

    def iter_channels(self):
        if self.error:
            raise self.error
        yield {"id": "C1", "name": "공개-채널", "is_member": True, "is_archived": False}

    def iter_users(self):
        yield {"id": "U1", "name": "u", "profile": {}}

    def iter_history(self, channel, oldest=None):
        yield {"type": "message", "ts": "1793344750.472099", "text": "회의록"}

    def iter_replies(self, channel, thread_ts):
        yield from ()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    FakeSlackClient.error = None
    monkeypatch.setattr(app_module, "SlackClient", FakeSlackClient)


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("sync", "import", "build", "serve", "doctor"):
        assert name in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("slackattic ")


@pytest.mark.parametrize("name", ["import", "build", "serve"])
def test_stub_commands_exit_not_implemented(name):
    result = runner.invoke(app, [name])
    assert result.exit_code == ExitCode.NOT_IMPLEMENTED


def test_sync_writes_metadata_then_reports_unchanged(tmp_path):
    first = runner.invoke(app, ["sync"])
    assert first.exit_code == 0
    assert "channels.json: updated" in first.output
    assert "#공개-채널 (C1): fetched 1, new 1, updated 0" in first.output
    assert (tmp_path / "archive" / "users.json").exists()
    assert (tmp_path / "archive" / "messages" / "C1" / "2026-10.jsonl").exists()
    second = runner.invoke(app, ["sync"])
    assert "channels.json: unchanged" in second.output
    assert "users.json: unchanged" in second.output
    assert "fetched 1, new 0, updated 0" in second.output


def test_sync_warns_about_missing_configured_channel(tmp_path):
    (tmp_path / "slackattic.toml").write_text('channels = ["C9"]\n', encoding="utf-8")
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "warning: channel C9" in result.output


def test_sync_reports_slack_error():
    FakeSlackClient.error = SlackAuthError("conversations.list: invalid_auth", "invalid_auth")
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == ExitCode.ERROR
    assert "invalid_auth" in result.output


def test_doctor_ok_without_leaking_token():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "config OK" in result.output
    assert "slack OK" in result.output
    assert "Test" in result.output
    assert TOKEN not in result.output


def test_doctor_reports_slack_auth_error():
    FakeSlackClient.error = SlackAuthError("auth.test: invalid_auth", "invalid_auth")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == ExitCode.ERROR
    assert "invalid_auth" in result.output
    assert "slack OK" not in result.output


def test_doctor_reports_missing_token(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == ExitCode.ERROR
    assert "not set" in result.output
    assert "Traceback" not in result.output


def test_doctor_uses_config_option(tmp_path):
    path = tmp_path / "other.toml"
    path.write_text('archive_dir = "elsewhere"\n', encoding="utf-8")
    result = runner.invoke(app, ["--config", str(path), "doctor"])
    assert result.exit_code == 0
    assert "elsewhere" in result.output
