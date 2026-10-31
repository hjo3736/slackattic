"""Record real Slack API responses, anonymize them and save them as a test fixture.

Usage: uv run python scripts/record_fixtures.py [--output tests/fixtures/slack/workspace.json]
Reads the token from SLACK_BOT_TOKEN and runs the real sync twice into a temporary folder.
"""

import argparse
import dataclasses
import json
import re
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from slack_sdk import WebClient

from slackattic.core.config import load_config
from slackattic.core.errors import SlackatticError
from slackattic.slack.client import REQUEST_TIMEOUT_SECONDS, SlackClient, WebClientLike
from slackattic.storage.files import dump_json, write_text_if_changed
from slackattic.sync.messages import sync_messages
from slackattic.sync.metadata import sync_metadata

FIXTURE_VERSION = 1
# Small pages so a handful of test messages still spans several pages
PAGE_SIZE = 5
DEFAULT_OUTPUT = Path("tests/fixtures/slack/workspace.json")

# Slack IDs (U…, C…, T…, B…); the digit lookahead skips plain words
ID_RE = re.compile(r"\b(?=[A-Z0-9]*\d)[ABCDEFGSTUW][A-Z0-9]{8,}\b")
TOKEN_RE = re.compile(r"xox[a-z]-[0-9A-Za-z-]+")
PERSON_KEYS = frozenset(
    {
        "real_name",
        "real_name_normalized",
        "display_name",
        "display_name_normalized",
        "first_name",
        "last_name",
    }
)
# avatar_hash can be derived from the account's email (Gravatar), so it goes too
CLEARED_KEYS = frozenset({"title", "phone", "skype", "status_text", "pronouns", "avatar_hash"})
AVATAR_URL = "https://example.invalid/avatar.png"
FAKE_DOMAIN = "example"
FAKE_TEAM = "Example Team"
MIN_CHECKED_NAME = 3
# Changes on every call (users.list returns the server time), so it would break replays
VOLATILE_KEYS = frozenset({"cache_ts"})


class LeakError(Exception):
    """A real identifier survived anonymization."""


class Anonymizer:
    """Replace identifiers with stable fakes, numbered in the order they are first seen."""

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.cursors: dict[str, str] = {}
        self.names: dict[str, str] = {}
        # Workspace subdomain and name, replaced wherever they appear in text
        self.replacements: dict[str, str] = {}

    def replace(self, real: str | None, fake: str) -> None:
        if real:
            self.replacements[real] = fake

    def fake_id(self, real: str) -> str:
        if real not in self.ids:
            same_prefix = sum(1 for k in self.ids if k[0] == real[0])
            self.ids[real] = f"{real[0]}{same_prefix + 1:010d}"
        return self.ids[real]

    def cursor(self, real: str) -> str:
        if real not in self.cursors:
            self.cursors[real] = f"cursor-{len(self.cursors) + 1}"
        return self.cursors[real]

    def person(self, real: str) -> str:
        if real not in self.names:
            self.names[real] = f"person-{len(self.names) + 1}"
        return self.names[real]

    def text(self, value: str) -> str:
        if "slack-edge.com" in value or "slack.com/avatar" in value:
            return AVATAR_URL
        value = ID_RE.sub(lambda m: self.fake_id(m.group(0)), value)
        for real, fake in self.replacements.items():
            value = value.replace(real, fake)
        return value

    def value(
        self, data: Any, key: str | None = None, person: bool = False, bot: bool = False
    ) -> Any:
        if isinstance(data, dict):
            # Bots keep their names (including in their profile) so the app name is not a leak
            bot = bot or bool(data.get("is_bot"))
            # User objects carry a profile; message copies of them sit under user_profile
            is_person = not bot and ("profile" in data or key in ("profile", "user_profile"))
            return {
                k: self.value(v, k, is_person, bot)
                for k, v in data.items()
                if k not in VOLATILE_KEYS
            }
        if isinstance(data, list):
            return [self.value(v, key, person, bot) for v in data]
        if not isinstance(data, str) or not data:
            return data
        if key == "next_cursor":
            return self.cursor(data)
        if not bot and (key in PERSON_KEYS or (person and key == "name")):
            return self.person(data)
        if key in CLEARED_KEYS:
            return ""
        if key and key.startswith("image_"):
            return AVATAR_URL
        return self.text(data)

    def params(self, params: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in params.items():
            if k == "cursor":
                if v not in self.cursors:
                    raise LeakError("request cursor was never seen in a response")
                out[k] = self.cursors[v]
            else:
                out[k] = self.value(v, k)
        return out

    def check(self, text: str) -> None:
        """Fail if any real identifier, name, cursor or token is still in the output."""
        if TOKEN_RE.search(text):
            raise LeakError("a token-like string is still present")
        # Very short names would match unrelated text, so they are not checked
        names = [n for n in self.names if len(n) >= MIN_CHECKED_NAME]
        leftovers = [
            real for real in (*self.ids, *self.cursors, *names, *self.replacements) if real in text
        ]
        if leftovers:
            # Report counts only so the real values are not printed
            raise LeakError(f"{len(leftovers)} real value(s) are still present")


class RecordingWeb:
    """Pass calls to the real client and keep each successful request and response."""

    def __init__(self, inner: WebClientLike) -> None:
        self._inner = inner
        self.interactions: list[dict[str, Any]] = []

    @property
    def retry_handlers(self) -> list[Any]:
        return self._inner.retry_handlers

    @retry_handlers.setter
    def retry_handlers(self, handlers: list[Any]) -> None:
        self._inner.retry_handlers = handlers

    def api_call(self, api_method: str, *, http_verb: str, params: dict[str, Any]) -> Any:
        if any("token" in k for k in params):
            raise LeakError("refusing to record a request that carries a token parameter")
        response = self._inner.api_call(api_method, http_verb=http_verb, params=params)
        self.interactions.append(
            {"method": api_method, "params": dict(params), "response": dict(response.data)}
        )
        return response


def interaction_key(method: str, params: dict[str, Any]) -> str:
    return method + " " + json.dumps(params, ensure_ascii=False, sort_keys=True)


def record(token: str, base_config: Any, now: int) -> list[dict[str, Any]]:
    recorder = RecordingWeb(WebClient(token=token, timeout=REQUEST_TIMEOUT_SECONDS))
    client = SlackClient(token, web_client=recorder, page_size=PAGE_SIZE)
    client.auth_test()
    with tempfile.TemporaryDirectory() as tmp:
        config = dataclasses.replace(base_config, archive_dir=Path(tmp), channels=())
        # Two runs cover both the first fetch and the incremental fetch with oldest set
        for _ in range(2):
            meta = sync_metadata(client, config)
            ids = [c["id"] for c in meta.selected]
            # A fixed clock keeps the rescan window, and so the recorded requests, replayable
            result = sync_messages(client, config.archive_dir, ids, config.rescan_days, now)
            if result.skipped:
                raise SlackatticError(f"{len(result.skipped)} channel(s) were skipped")
    return recorder.interactions


def anonymize(interactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    anon = Anonymizer()
    for item in interactions:
        if item["method"] == "auth.test":
            data = item["response"]
            anon.replace(_subdomain(data.get("url")), FAKE_DOMAIN)
            anon.replace(data.get("team"), FAKE_TEAM)
    out: dict[str, dict[str, Any]] = {}
    # Recording order keeps cursors mapped before the request that sends them back
    for item in interactions:
        response = anon.value(item["response"])
        params = anon.params(item["params"])
        key = interaction_key(item["method"], params)
        if key in out and out[key]["response"] != response:
            raise SlackatticError("the workspace changed during recording; run it again")
        out[key] = {"method": item["method"], "params": params, "response": response}
    result = [out[k] for k in sorted(out)]
    anon.check(dump_json(result))
    return result


def _subdomain(url: str | None) -> str | None:
    match = re.match(r"https://([^./]+)\.slack\.com", url or "")
    return match.group(1) if match else None


def channel_summary(interactions: list[dict[str, Any]]) -> Iterator[str]:
    for item in interactions:
        if item["method"] in ("conversations.history", "conversations.replies"):
            p = item["params"]
            count = len(item["response"].get("messages", []))
            page = "next page" if p.get("cursor") else "first page"
            since = f", oldest {p['oldest']}" if p.get("oldest") else ""
            thread = f", thread {p['ts']}" if p.get("ts") else ""
            yield f"{p['channel']}: {count} message(s), {page}{since}{thread}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        config = load_config()
        now = int(time.time())
        interactions = anonymize(record(config.token, config, now))
        payload = {
            "interactions": interactions,
            "now": now,
            "page_size": PAGE_SIZE,
            "rescan_days": config.rescan_days,
            "version": FIXTURE_VERSION,
        }
        changed = write_text_if_changed(args.output, dump_json(payload))
    except (SlackatticError, LeakError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for line in channel_summary(interactions):
        print(line)
    print(f"{args.output}: {'updated' if changed else 'unchanged'} ({len(interactions)} calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
