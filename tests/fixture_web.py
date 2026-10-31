import copy
import json
from pathlib import Path
from typing import Any

WORKSPACE_FIXTURE = Path(__file__).parent / "fixtures" / "slack" / "workspace.json"


def interaction_key(method: str, params: dict[str, Any]) -> str:
    # Must match scripts/record_fixtures.py
    return method + " " + json.dumps(params, ensure_ascii=False, sort_keys=True)


class ReplayResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


class ReplayWeb:
    """Stand-in for slack_sdk.WebClient that answers from a recorded fixture."""

    def __init__(self, path: Path = WORKSPACE_FIXTURE) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.page_size: int = payload["page_size"]
        # The clock and window used while recording, so requests match exactly
        self.now: int = payload["now"]
        self.rescan_days: int = payload["rescan_days"]
        self.retry_handlers: list[Any] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = {
            interaction_key(i["method"], i["params"]): i["response"]
            for i in payload["interactions"]
        }

    def api_call(self, api_method: str, *, http_verb: str, params: dict[str, Any]) -> Any:
        self.calls.append((api_method, dict(params)))
        key = interaction_key(api_method, params)
        if key not in self._responses:
            # Fail loudly so a changed request shape is noticed instead of returning nothing
            raise AssertionError(f"no recorded response for {key}; re-record the fixture")
        return ReplayResponse(copy.deepcopy(self._responses[key]))
