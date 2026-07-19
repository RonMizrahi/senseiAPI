from types import SimpleNamespace
from typing import Any

import pytest

from assistant.client import (
    AssistantError,
    OpenAIAssistant,
    TextChunk,
    ToolCallChunk,
    ToolResultChunk,
)
from assistant.tools import Tools


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _text(content: str | None, finish: str | None = None) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _tool(
    index: int, call_id: str, name: str, args: str, finish: str | None = None
) -> SimpleNamespace:
    fn = SimpleNamespace(name=name, arguments=args)
    tc = SimpleNamespace(index=index, id=call_id, function=fn)
    delta = SimpleNamespace(content=None, tool_calls=[tc])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _empty() -> SimpleNamespace:
    return SimpleNamespace(choices=[])


class _FakeStream:
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = chunks
        self.closed = False

    def __aiter__(self) -> "_FakeStream":
        self._it = iter(self._chunks)
        return self

    async def __anext__(self) -> SimpleNamespace:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True


class _FakeCompletions:
    """Returns one stream per create() call, in order (round 1, round 2, ...)."""

    def __init__(self, rounds: list[list[SimpleNamespace]], error: Exception | None = None) -> None:
        self._rounds = list(rounds)
        self._error = error
        self.calls: list[dict[str, Any]] = []
        self.streams: list[_FakeStream] = []

    async def create(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        stream = _FakeStream(self._rounds.pop(0))
        self.streams.append(stream)
        return stream


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeOpenAI:
    def __init__(self, rounds: list[list[SimpleNamespace]], error: Exception | None = None) -> None:
        self.completions = _FakeCompletions(rounds, error)
        self.chat = _FakeChat(self.completions)


async def _collect(assistant: OpenAIAssistant) -> list[Any]:
    return [e async for e in assistant.stream([{"role": "user", "content": "היי"}])]


def _fake_tools() -> Tools:
    async def fetch(
        url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        return 200, {"paths": {"/assistant/context/agenda": {"get": {"summary": "a"}}}}

    return Tools(base_url="http://api", fetch=fetch)


@pytest.mark.anyio
async def test_streams_text_events_and_advertises_no_tools_when_absent() -> None:
    client = _FakeOpenAI([[_text("שלום"), _empty(), _text(None), _text(" עולם", finish="stop")]])

    events = await _collect(OpenAIAssistant(client=client, model="gpt-4o"))

    assert [e.text for e in events if isinstance(e, TextChunk)] == ["שלום", " עולם"]
    assert "tools" not in client.completions.calls[0]


@pytest.mark.anyio
async def test_runs_the_tool_loop_then_streams_the_answer() -> None:
    rounds = [
        [_tool(0, "call_1", "discover_api", "{}", finish="tool_calls")],
        [_text("הנה מה שמצאתי", finish="stop")],
    ]
    client = _FakeOpenAI(rounds)

    events = await _collect(OpenAIAssistant(client=client, model="gpt-4o", tools=_fake_tools()))

    call = next(e for e in events if isinstance(e, ToolCallChunk))
    result = next(e for e in events if isinstance(e, ToolResultChunk))
    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    assert call.name == "discover_api"
    assert "endpoints" in result.output
    assert text == "הנה מה שמצאתי"
    # Round 2 was called with the tool result folded into the conversation.
    assert len(client.completions.calls) == 2
    assert client.completions.calls[0]["tools"]  # tools advertised when present


@pytest.mark.anyio
async def test_wraps_sdk_errors_without_leaking_detail() -> None:
    client = _FakeOpenAI([], error=RuntimeError("Incorrect API key provided: sk-secret123"))

    with pytest.raises(AssistantError) as excinfo:
        await _collect(OpenAIAssistant(client=client, model="gpt-4o"))

    assert "sk-secret123" not in str(excinfo.value)


@pytest.mark.anyio
async def test_closes_the_underlying_stream() -> None:
    client = _FakeOpenAI([[_text("שלום", finish="stop")]])

    await _collect(OpenAIAssistant(client=client, model="gpt-4o"))

    assert client.completions.streams[0].closed is True


@pytest.mark.anyio
async def test_notes_when_the_answer_is_truncated_by_the_output_cap() -> None:
    client = _FakeOpenAI([[_text("חלק מהתשובה", finish="length")]])

    events = await _collect(OpenAIAssistant(client=client, model="gpt-4o"))
    text = "".join(e.text for e in events if isinstance(e, TextChunk))

    assert "חלק מהתשובה" in text
    assert "קוצרה" in text  # a truncation note was appended


@pytest.mark.anyio
async def test_passes_max_output_tokens_when_set() -> None:
    client = _FakeOpenAI([[_text("hi", finish="stop")]])
    assistant = OpenAIAssistant(client=client, model="gpt-4o", max_output_tokens=2000)

    await _collect(assistant)

    assert client.completions.calls[0]["max_completion_tokens"] == 2000


@pytest.mark.anyio
async def test_caps_runaway_tool_rounds() -> None:
    # The model keeps calling a tool and never answers → fail after the cap, not loop forever.
    rounds = [[_tool(0, "c", "discover_api", "{}", finish="tool_calls")] for _ in range(6)]
    client = _FakeOpenAI(rounds)

    with pytest.raises(AssistantError):
        await _collect(OpenAIAssistant(client=client, model="gpt-4o", tools=_fake_tools()))
