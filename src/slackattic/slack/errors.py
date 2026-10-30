from slackattic.core.errors import SlackatticError


class SlackError(SlackatticError):
    """Slack API failure that stops the whole run."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class SlackAuthError(SlackError):
    """Token or permission problem; retrying will not help."""


class SlackChannelError(SlackError):
    """Problem limited to one channel; the run can skip it and continue."""

    def __init__(self, message: str, code: str, channel: str) -> None:
        super().__init__(message, code)
        self.channel = channel


class SlackTransientError(SlackError):
    """Network, server or rate limit failure left after retries."""
