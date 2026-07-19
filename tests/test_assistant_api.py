from collections.abc import AsyncIterator, Sequence
from typing import Any

from fastapi.testclient import TestClient

from assistant.client import AssistantClient, AssistantError, StreamEvent, TextChunk
from assistant.dependencies import get_assistant_service
from assistant.service import AssistantService
from core.config import Settings, get_settings
from main import app


class _FakeAssistant(AssistantClient):
    def __init__(self, deltas: list[str], error: str | None = None) -> None:
        self._deltas = deltas
        self._error = error

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        for delta in self._deltas:
            yield TextChunk(delta)
        if self._error is not None:
            raise AssistantError(self._error)


def _client(deltas: list[str], error: str | None = None) -> TestClient:
    service = AssistantService(client=_FakeAssistant(deltas, error))
    app.dependency_overrides[get_assistant_service] = lambda: service
    return TestClient(app)


_BODY = {"messages": [{"role": "user", "parts": [{"type": "text", "text": "שלום"}]}]}


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_chat_streams_the_ai_sdk_ui_message_protocol() -> None:
    client = _client(["שלום", " עולם"])

    res = client.post("/assistant/chat", json=_BODY)

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["x-vercel-ai-ui-message-stream"] == "v1"
    body = res.text
    assert '"type":"start"' in body
    assert '"delta":"שלום"' in body
    assert '"type":"finish"' in body
    assert body.rstrip().endswith("[DONE]")


def test_chat_reports_a_model_failure_as_an_error_part_not_a_500() -> None:
    client = _client([], error="model overloaded")

    res = client.post("/assistant/chat", json=_BODY)

    assert res.status_code == 200
    assert '"type":"error"' in res.text
    assert "model overloaded" in res.text


def test_chat_rejects_an_overlong_question() -> None:
    """A user question past the char cap is refused with 422 before streaming."""
    client = _client(["ok"])
    body = {"messages": [{"role": "user", "parts": [{"type": "text", "text": "א" * 5000}]}]}

    res = client.post("/assistant/chat", json=body)

    assert res.status_code == 422


def test_chat_rejects_a_request_with_no_user_question() -> None:
    """Degenerate bodies (no messages / empty / no text parts) must not invoke the
    paid model — they are refused with 422 before streaming."""
    client = _client(["ok"])
    for body in ({}, {"messages": []}, {"messages": [{"role": "user", "parts": []}]}):
        res = client.post("/assistant/chat", json=body)
        assert res.status_code == 422, body


def test_chat_returns_503_when_the_assistant_is_not_configured() -> None:
    """With no OPENAI_API_KEY, the real dependency refuses (forced, not env-dependent)."""
    app.dependency_overrides[get_settings] = lambda: Settings(
        openai_api_key=None, database_url=None
    )
    res = TestClient(app).post("/assistant/chat", json=_BODY)

    assert res.status_code == 503
