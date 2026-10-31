import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "record_fixtures.py"
spec = importlib.util.spec_from_file_location("record_fixtures", SCRIPT)
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)


def auth(url="https://myteam.slack.com/", team="My Team"):
    return {"method": "auth.test", "params": {}, "response": {"url": url, "team": team}}


def history(params, response):
    return {"method": "conversations.history", "params": params, "response": response}


def test_ids_are_replaced_consistently_including_mentions():
    items = [
        auth(),
        history(
            {"channel": "C0REAL00001", "limit": 5},
            {"messages": [{"user": "U0REAL00001", "text": "<@U0REAL00001> 회의 <#C0REAL00001>"}]},
        ),
    ]
    out = rf.anonymize(items)
    text = json.dumps(out, ensure_ascii=False)
    assert "U0REAL00001" not in text and "C0REAL00001" not in text
    message = out[1]["response"]["messages"][0]
    assert message["text"] == f"<@{message['user']}> 회의 <#{out[1]['params']['channel']}>"


def test_cursors_are_replaced_in_responses_and_follow_up_requests():
    items = [
        auth(),
        history({"channel": "C1AAAAAAAA1"}, {"response_metadata": {"next_cursor": "dXNlcjpVMEND"}}),
        history({"channel": "C1AAAAAAAA1", "cursor": "dXNlcjpVMEND"}, {"messages": []}),
    ]
    out = rf.anonymize(items)
    assert "dXNlcjpVMEND" not in json.dumps(out)
    cursors = {o["params"].get("cursor") for o in out} | {
        o["response"].get("response_metadata", {}).get("next_cursor") for o in out
    }
    assert "cursor-1" in cursors


def test_people_lose_names_and_avatars_but_bots_keep_theirs():
    member = {
        "id": "U0REAL00001",
        "name": "someone",
        "profile": {
            "real_name": "Hong Gildong",
            "image_72": "https://avatars.slack-edge.com/x.png",
            "avatar_hash": "gabc1234def5",
        },
    }
    bot = {
        "id": "U0BOTBOT001",
        "name": "slackattic",
        "is_bot": True,
        "profile": {"real_name": "slackattic"},
    }
    items = [
        auth(),
        {
            "method": "users.list",
            "params": {},
            "response": {"members": [member, bot], "cache_ts": 1},
        },
    ]
    out = rf.anonymize(items)
    members = out[1]["response"]["members"]
    assert members[0]["name"].startswith("person-")
    assert members[0]["profile"]["real_name"].startswith("person-")
    assert members[0]["profile"]["image_72"] == rf.AVATAR_URL
    assert members[0]["profile"]["avatar_hash"] == ""
    assert members[1]["name"] == "slackattic"
    assert "cache_ts" not in out[1]["response"]


def test_workspace_domain_and_name_are_replaced():
    out = rf.anonymize([auth()])
    assert out[0]["response"] == {"url": "https://example.slack.com/", "team": "Example Team"}


def test_leftover_real_value_fails_without_printing_it():
    anon = rf.Anonymizer()
    anon.fake_id("U0REAL00001")
    with pytest.raises(rf.LeakError) as e:
        anon.check('{"user": "U0REAL00001"}')
    assert "U0REAL00001" not in str(e.value)


def test_token_like_text_fails():
    with pytest.raises(rf.LeakError):
        rf.Anonymizer().check("xoxb-123-abc")


def test_changed_response_for_the_same_request_fails():
    items = [
        auth(),
        history({"channel": "C1AAAAAAAA1"}, {"messages": []}),
        history({"channel": "C1AAAAAAAA1"}, {"messages": [{"ts": "1.0"}]}),
    ]
    with pytest.raises(rf.SlackatticError):
        rf.anonymize(items)


def test_recorder_refuses_token_parameters():
    class Inner:
        retry_handlers = []

        def api_call(self, *a, **k):
            raise AssertionError("must not be called")

    with pytest.raises(rf.LeakError):
        rf.RecordingWeb(Inner()).api_call("auth.test", http_verb="GET", params={"token": "x"})
