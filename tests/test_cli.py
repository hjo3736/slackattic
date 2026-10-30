import pytest
from typer.testing import CliRunner

from slackattic.cli.app import app
from slackattic.core.config import TOKEN_ENV
from slackattic.core.errors import ExitCode

TOKEN = "xoxb-secret-value"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("sync", "import", "build", "serve", "doctor"):
        assert name in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("slackattic ")


@pytest.mark.parametrize("name", ["sync", "import", "build", "serve"])
def test_stub_commands_exit_not_implemented(name):
    result = runner.invoke(app, [name])
    assert result.exit_code == ExitCode.NOT_IMPLEMENTED


def test_doctor_ok_without_leaking_token():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "config OK" in result.output
    assert TOKEN not in result.output


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
