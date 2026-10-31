import http.client
from collections.abc import Iterator, Mapping
from typing import Any, Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
    ServerErrorRetryHandler,
)

from slackattic.slack.errors import (
    SlackAuthError,
    SlackChannelError,
    SlackError,
    SlackThreadError,
    SlackTransientError,
)

REQUEST_TIMEOUT_SECONDS = 30
RATE_LIMIT_RETRIES = 5
SERVER_ERROR_RETRIES = 3
CONNECTION_RETRIES = 3
DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_PAGES = 10_000

AUTH_ERRORS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "missing_scope",
        "team_access_not_granted",
    }
)
CHANNEL_ERRORS = frozenset({"not_in_channel", "channel_not_found", "is_archived"})
# A deleted thread root can make its replies unreachable; only that thread is skipped
THREAD_ERRORS = frozenset({"thread_not_found"})
TRANSIENT_ERRORS = frozenset(
    {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout"}
)


class WebClientLike(Protocol):
    retry_handlers: list[Any]

    def api_call(self, api_method: str, *, http_verb: str, params: dict[str, Any]) -> Any: ...


class SlackClient:
    """The only place that talks to the Slack Web API."""

    def __init__(
        self,
        token: str,
        *,
        web_client: WebClientLike | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        web = web_client or WebClient(token=token, timeout=REQUEST_TIMEOUT_SECONDS)
        web.retry_handlers = [
            RateLimitErrorRetryHandler(max_retry_count=RATE_LIMIT_RETRIES),
            ServerErrorRetryHandler(max_retry_count=SERVER_ERROR_RETRIES),
            ConnectionErrorRetryHandler(max_retry_count=CONNECTION_RETRIES),
        ]
        self._web = web
        self._page_size = page_size
        self._max_pages = max_pages

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        try:
            response = self._web.api_call(method, http_verb="GET", params=params)
        except SlackApiError as e:
            raise _classify(method, e, params.get("channel"), params.get("ts")) from None
        except (OSError, http.client.HTTPException) as e:
            # URLError, timeouts and dropped connections all land here after retries
            raise SlackTransientError(
                f"{method}: network error ({type(e).__name__}). Check your connection."
            ) from None
        return dict(response.data)

    def paginate(self, method: str, key: str, **params: Any) -> Iterator[dict[str, Any]]:
        """Yield items page by page so large results never sit in memory at once."""
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(self._max_pages):
            data = self.call(method, cursor=cursor, limit=self._page_size, **params)
            yield from data.get(key, [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
            if cursor is None:
                return
            if cursor in seen:
                raise SlackTransientError(f"{method}: pagination cursor repeated")
            seen.add(cursor)
        raise SlackTransientError(f"{method}: more than {self._max_pages} pages")

    def auth_test(self) -> dict[str, Any]:
        return self.call("auth.test")

    def iter_channels(self) -> Iterator[dict[str, Any]]:
        return self.paginate(
            "conversations.list",
            "channels",
            types="public_channel,private_channel",
            # Archived channels stay listed so their stored messages keep a name
            exclude_archived=False,
        )

    def iter_users(self) -> Iterator[dict[str, Any]]:
        return self.paginate("users.list", "members")

    def iter_history(self, channel: str, oldest: str | None = None) -> Iterator[dict[str, Any]]:
        return self.paginate("conversations.history", "messages", channel=channel, oldest=oldest)

    def iter_replies(self, channel: str, thread_ts: str) -> Iterator[dict[str, Any]]:
        # The first item is the thread root itself
        return self.paginate("conversations.replies", "messages", channel=channel, ts=thread_ts)


def _classify(
    method: str, error: SlackApiError, channel: str | None, thread_ts: str | None = None
) -> SlackError:
    response: Mapping[str, Any] = error.response or {}
    code = str(response.get("error") or "unknown_error")
    status = getattr(error.response, "status_code", None)
    # Only the error code goes into messages so request details never leak
    if code in AUTH_ERRORS:
        detail = ""
        if code == "missing_scope" and response.get("needed"):
            detail = f" (needed: {response['needed']}; update the app and reinstall it)"
        return SlackAuthError(f"{method}: {code}{detail}", code)
    if code in THREAD_ERRORS and channel and thread_ts:
        return SlackThreadError(
            f"{method}: {code} for thread {thread_ts} in channel {channel}",
            code,
            channel,
            thread_ts,
        )
    if code in CHANNEL_ERRORS and channel:
        return SlackChannelError(f"{method}: {code} in channel {channel}", code, channel)
    if code in TRANSIENT_ERRORS or status == 429 or (status is not None and status >= 500):
        return SlackTransientError(f"{method}: {code} (status {status}) after retries", code)
    return SlackError(f"{method}: unexpected Slack error '{code}'", code)
