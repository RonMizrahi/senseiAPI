"""The assistant model client: an abstraction plus its OpenAI implementation.

Mirrors ``summaries/summarizer.py`` — a small ABC so the service depends on an
interface, a ``Protocol`` for the underlying SDK so tests can inject a fake, and a
concrete ``OpenAIAssistant`` that streams from the Chat Completions API and runs a
tool-call loop (see ``assistant/tools.py``).

The stream yields typed events — text deltas plus tool-call/tool-result events —
so the service can render them as AI-SDK stream parts. The concrete class is
deliberately free of a hard ``openai`` import; the SDK is imported lazily in
``assistant/dependencies.py``.
"""

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from assistant.tools import Tools

logger = logging.getLogger(__name__)

# Cap tool rounds so a misbehaving model can't loop forever.
_MAX_TOOL_ROUNDS = 4


@dataclass(frozen=True)
class TextChunk:
    text: str


@dataclass(frozen=True)
class ToolCallChunk:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResultChunk:
    id: str
    name: str
    output: Any


StreamEvent = TextChunk | ToolCallChunk | ToolResultChunk


class _ChatCompletions(Protocol):
    async def create(
        self, *, model: str, messages: Any, stream: bool, tools: Any = None
    ) -> Any: ...


class _Chat(Protocol):
    @property
    def completions(self) -> _ChatCompletions: ...


class OpenAIChatClient(Protocol):
    """The slice of ``openai.AsyncOpenAI`` the assistant uses (read-only properties
    so the real ``AsyncOpenAI`` and simple test fakes both satisfy it)."""

    @property
    def chat(self) -> _Chat: ...


# Shown to the client when the model fails. Deliberately generic — the underlying
# SDK error (which can embed the misconfigured API key) is logged server-side only.
_UNAVAILABLE_MESSAGE = "העוזר אינו זמין כרגע. נסו שוב מאוחר יותר."


class AssistantError(Exception):
    """Raised when the model fails. The message is safe to surface to the client —
    never put raw upstream SDK error text here (it may contain secrets)."""


class AssistantClient(ABC):
    """Streams an assistant reply as a sequence of typed events."""

    @abstractmethod
    def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]: ...


class OpenAIAssistant(AssistantClient):
    """Assistant backed by the hosted OpenAI Chat Completions API, with tool calls."""

    def __init__(
        self,
        *,
        client: OpenAIChatClient,
        model: str,
        tools: Tools | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._tools = tools
        self._max_output_tokens = max_output_tokens

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        convo: list[dict[str, Any]] = list(messages)
        specs = self._tools.specs() if self._tools else []

        for _round in range(_MAX_TOOL_ROUNDS + 1):
            kwargs: dict[str, Any] = {"model": self._model, "messages": convo, "stream": True}
            if specs:
                kwargs["tools"] = specs
            if self._max_output_tokens:
                kwargs["max_completion_tokens"] = self._max_output_tokens

            try:
                completion = await self._client.chat.completions.create(**kwargs)
            except Exception as exc:
                logger.error("openai assistant request failed", exc_info=exc)
                raise AssistantError(_UNAVAILABLE_MESSAGE) from exc

            tool_calls: dict[int, dict[str, str]] = {}
            finish_reason: str | None = None
            try:
                async for chunk in completion:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.delta.content:
                        yield TextChunk(choice.delta.content)  # stream text as it arrives
                    for tc in choice.delta.tool_calls or []:
                        acc = tool_calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function and tc.function.name:
                            acc["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            acc["args"] += tc.function.arguments
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
            except Exception as exc:
                logger.error("openai assistant stream failed", exc_info=exc)
                raise AssistantError(_UNAVAILABLE_MESSAGE) from exc
            finally:
                close = getattr(completion, "close", None)
                if close is not None:
                    await close()

            if finish_reason != "tool_calls" or not tool_calls:
                # The model produced its final answer (already streamed). If it hit the
                # output-token cap, tell the reader the reply was clipped.
                if finish_reason == "length":
                    yield TextChunk("\n\n(התשובה קוצרה עקב מגבלת האורך.)")
                return

            ordered = [tool_calls[i] for i in sorted(tool_calls)]
            convo.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["args"] or "{}"},
                        }
                        for c in ordered
                    ],
                }
            )
            for c in ordered:
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield ToolCallChunk(id=c["id"], name=c["name"], arguments=args)
                result = await self._run_tool(c["name"], args)
                yield ToolResultChunk(id=c["id"], name=c["name"], output=result)
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        raise AssistantError(_UNAVAILABLE_MESSAGE)  # ran out of tool rounds

    async def _run_tool(self, name: str, args: dict[str, Any]) -> Any:
        if self._tools is None:
            return {"error": "tool not available"}
        try:
            return await self._tools.dispatch(name, args)
        except Exception as exc:  # a tool failure is fed back to the model, not fatal
            logger.error("assistant tool %s failed", name, exc_info=exc)
            return {"error": "tool call failed"}
