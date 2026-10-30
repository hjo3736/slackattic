import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
    ServerErrorRetryHandler,
)

from slackattic.slack.client import SlackClient
from slackattic.slack.errors import (
    SlackAuthError,
    SlackChannelError,
    SlackError,
    SlackTransientError,
)

TOKEN = "xoxb-secret-value"


class FakeResponse(dict):
    def __init__(self, data: dict[str, Any], status_code: int = 200) -> None:
        super().__init__(data)
        self.status_code = status_code


class FakeWeb:
    """Replays queued results in order and records every call."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.retry_handlers: list[Any] = []

    def api_call(self, api_method: str, *, http_verb: str, params: dict[str, Any]) -> Any:
        self.calls.append((api_method, params))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(data=result)


def api_error(code: str, status: int = 200, **extra: Any) -> SlackApiError:
    return SlackApiError("failed", FakeResponse({"ok": False, "error": code, **extra}, status))


def page(items: list[dict[str, Any]], cursor: str = "") -> dict[str, Any]:
    return {"ok": True, "messages": items, "response_metadata": {"next_cursor": cursor}}


def make(*results: Any, **kwargs: Any) -> tuple[SlackClient, FakeWeb]:
    web = FakeWeb(*results)
    return SlackClient(TOKEN, web_client=web, **kwargs), web


def test_configures_retry_handlers():
    _, web = make()
    types = [type(h) for h in web.retry_handlers]
    assert types == [
        RateLimitErrorRetryHandler,
        ServerErrorRetryHandler,
        ConnectionErrorRetryHandler,
    ]


def test_call_drops_none_params():
    client, web = make({"ok": True})
    client.call("conversations.history", channel="C1", oldest=None)
    assert web.calls == [("conversations.history", {"channel": "C1"})]


def test_paginate_follows_cursor_and_streams():
    client, web = make(page([{"ts": "1"}, {"ts": "2"}], "c2"), page([{"ts": "3"}]))
    items = client.iter_history("C1")
    assert next(items) == {"ts": "1"}
    assert len(web.calls) == 1
    assert [m["ts"] for m in items] == ["2", "3"]
    assert web.calls[1][1]["cursor"] == "c2"
    assert "cursor" not in web.calls[0][1]


def test_paginate_handles_missing_metadata():
    client, _ = make({"ok": True, "messages": [{"ts": "1"}]})
    assert list(client.iter_history("C1")) == [{"ts": "1"}]


def test_paginate_stops_on_repeated_cursor():
    client, _ = make(page([], "same"), page([], "same"))
    with pytest.raises(SlackTransientError, match="repeated"):
        list(client.iter_history("C1"))


def test_paginate_stops_at_max_pages():
    client, _ = make(page([], "a"), page([], "b"), max_pages=2)
    with pytest.raises(SlackTransientError, match="more than 2 pages"):
        list(client.iter_history("C1"))


def test_list_methods_pass_expected_params():
    client, web = make(
        {"ok": True, "channels": [{"id": "C1"}]},
        {"ok": True, "members": [{"id": "U1"}]},
    )
    assert list(client.iter_channels()) == [{"id": "C1"}]
    assert list(client.iter_users()) == [{"id": "U1"}]
    assert web.calls[0][1]["types"] == "public_channel,private_channel"
    assert web.calls[0][1]["exclude_archived"] is False


@pytest.mark.parametrize("code", ["invalid_auth", "token_revoked", "not_authed"])
def test_auth_errors(code):
    client, _ = make(api_error(code))
    with pytest.raises(SlackAuthError) as info:
        client.auth_test()
    assert info.value.code == code


def test_missing_scope_names_needed_scope():
    client, _ = make(api_error("missing_scope", needed="emoji:read"))
    with pytest.raises(SlackAuthError, match="needed: emoji:read"):
        client.call("emoji.list")


@pytest.mark.parametrize("code", ["not_in_channel", "channel_not_found"])
def test_channel_errors_carry_channel(code):
    client, _ = make(api_error(code))
    with pytest.raises(SlackChannelError) as info:
        list(client.iter_history("C9"))
    assert info.value.channel == "C9"


def test_channel_code_without_channel_is_not_skippable():
    client, _ = make(api_error("channel_not_found"))
    with pytest.raises(SlackError) as info:
        client.call("conversations.info")
    assert not isinstance(info.value, SlackChannelError)


@pytest.mark.parametrize(
    "error",
    [api_error("ratelimited", 429), api_error("internal_error", 500), api_error("x", 503)],
)
def test_transient_api_errors(error):
    client, _ = make(error)
    with pytest.raises(SlackTransientError):
        client.auth_test()


@pytest.mark.parametrize("error", [TimeoutError("timed out"), ConnectionResetError()])
def test_network_errors_are_transient(error):
    client, _ = make(error)
    with pytest.raises(SlackTransientError, match="network error"):
        client.auth_test()


def test_unknown_error_stops_run():
    client, _ = make(api_error("something_new"))
    with pytest.raises(SlackError, match="something_new") as info:
        client.auth_test()
    assert type(info.value) is SlackError


def test_error_messages_do_not_leak_token():
    client, _ = make(api_error("invalid_auth", token=TOKEN))
    with pytest.raises(SlackAuthError) as info:
        client.auth_test()
    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None


class ScriptedHandler(BaseHTTPRequestHandler):
    script: list[tuple[int, dict[str, Any]]] = []
    hits = 0

    def do_GET(self) -> None:
        type(self).hits += 1
        status, body = type(self).script.pop(0)
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if status == 429:
            self.send_header("Retry-After", "0")
        self.end_headers()
        self.wfile.write(payload)

    do_POST = do_GET

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def slack_server() -> Iterator[type[ScriptedHandler]]:
    ScriptedHandler.script, ScriptedHandler.hits = [], 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ScriptedHandler.base_url = f"http://127.0.0.1:{server.server_address[1]}/api/"
    try:
        yield ScriptedHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def real_client(handler: type[ScriptedHandler]) -> SlackClient:
    web = WebClient(token=TOKEN, base_url=handler.base_url, timeout=5)
    return SlackClient(TOKEN, web_client=web)


def test_real_client_retries_rate_limit(slack_server):
    limited = (429, {"ok": False, "error": "ratelimited"})
    slack_server.script = [limited, limited, (200, {"ok": True, "team": "Test"})]
    assert real_client(slack_server).auth_test()["team"] == "Test"
    assert slack_server.hits == 3


def test_real_client_gives_up_after_retry_limit(slack_server):
    slack_server.script = [(429, {"ok": False, "error": "ratelimited"})] * 6
    with pytest.raises(SlackTransientError):
        real_client(slack_server).auth_test()
    assert slack_server.hits == 6
