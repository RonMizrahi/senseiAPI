"""Orchestrates a chat request into an AI SDK UI Message Stream.

The service is stateless: the frontend sends the full conversation each request
(matching the client's localStorage-owned history), and we stream the reply back
as SSE frames the ``useChat`` transport understands.
"""

from collections.abc import AsyncIterator

from assistant import sse
from assistant.client import (
    AssistantClient,
    AssistantError,
    TextChunk,
    ToolCallChunk,
    ToolResultChunk,
)
from assistant.prompt import ASSISTANT_SYSTEM_PROMPT
from assistant.schemas import ChatRequest, to_openai_messages
from assistant.tokens import trim_to_token_budget


class AssistantService:
    """Turns a :class:`ChatRequest` into a stream of AI-SDK SSE frames."""

    def __init__(
        self,
        *,
        client: AssistantClient,
        system_prompt: str = ASSISTANT_SYSTEM_PROMPT,
        max_input_tokens: int | None = None,
    ) -> None:
        self._client = client
        self._system_prompt = system_prompt
        self._max_input_tokens = max_input_tokens

    async def stream_sse(self, request: ChatRequest) -> AsyncIterator[str]:
        messages = to_openai_messages(request, self._system_prompt)
        if self._max_input_tokens:
            messages = trim_to_token_budget(messages, self._max_input_tokens)

        yield sse.start()
        text_started = False
        try:
            async for event in self._client.stream(messages):
                if isinstance(event, ToolCallChunk):
                    yield sse.tool_input_available(event.id, event.name, event.arguments)
                elif isinstance(event, ToolResultChunk):
                    yield sse.tool_output_available(event.id, event.output)
                elif isinstance(event, TextChunk):
                    if not text_started:
                        yield sse.text_start()
                        text_started = True
                    yield sse.text_delta(event.text)
        except AssistantError as exc:
            # A mid-stream model failure becomes an error part the client renders,
            # never a broken HTTP response (the 200 stream has already begun).
            yield sse.error(str(exc))
            yield sse.DONE
            return

        if text_started:
            yield sse.text_end()
        yield sse.finish()
        yield sse.DONE
