"""Request models for the chat endpoint and the mapping to OpenAI messages.

The shapes mirror what ``@ai-sdk/react``'s ``useChat`` POSTs: ``{ messages: [...] }``
where each message is a ``UIMessage`` — ``{ id, role, parts: [{ type, text }] }``.
Only text parts matter to us today; other part kinds (tool calls, files) and extra
envelope fields (``id``, ``trigger``) are tolerated and ignored.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Roles accepted from the client. "system" is intentionally excluded — the trusted
# system prompt is prepended server-side, so a client-supplied system message could
# only be an attempt to override the assistant's guardrails.
_ALLOWED_ROLES = {"user", "assistant"}

# Boundary caps, mirroring the codebase convention (auth/patients schemas). Keep the
# request bounded so a caller cannot forward an unbounded payload to OpenAI.
_MAX_TEXT_CHARS = 8_000
_MAX_PARTS_PER_MESSAGE = 50
_MAX_MESSAGES = 100


class ChatPart(BaseModel):
    """One part of a UIMessage. We read text parts; the rest are ignored."""

    model_config = ConfigDict(extra="ignore")

    type: str
    text: str | None = Field(default=None, max_length=_MAX_TEXT_CHARS)


class ChatMessage(BaseModel):
    """A single UIMessage from the conversation."""

    model_config = ConfigDict(extra="ignore")

    role: str
    parts: list[ChatPart] = Field(default_factory=list, max_length=_MAX_PARTS_PER_MESSAGE)

    def text(self) -> str:
        """Concatenate this message's text parts into a single content string."""
        return "".join(part.text for part in self.parts if part.type == "text" and part.text)


class ChatRequest(BaseModel):
    """The body useChat sends to ``POST /assistant/chat``."""

    model_config = ConfigDict(extra="ignore")

    messages: list[ChatMessage] = Field(default_factory=list, max_length=_MAX_MESSAGES)


def latest_question_length(request: ChatRequest) -> int:
    """Character length of the most recent user question (0 if there is none)."""
    for message in reversed(request.messages):
        if message.role == "user":
            return len(message.text())
    return 0


def to_openai_messages(request: ChatRequest, system_prompt: str) -> list[dict[str, Any]]:
    """Build the OpenAI message list: the system prompt followed by the conversation.

    Messages with an unsupported role or no text content are dropped, so an empty or
    malformed history yields just the system prompt rather than an API error.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for message in request.messages:
        if message.role not in _ALLOWED_ROLES:
            continue
        content = message.text()
        if content:
            messages.append({"role": message.role, "content": content})
    return messages
