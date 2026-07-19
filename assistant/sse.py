"""Formatters for the Vercel AI SDK "UI Message Stream" SSE protocol.

The frontend consumes this endpoint with ``@ai-sdk/react``'s ``useChat``, which
expects Server-Sent Events framed as the AI SDK UI Message Stream (protocol v1).
Each event is a line ``data: {json}\\n\\n``; the stream ends with ``data: [DONE]``.

A minimal text response is the sequence:
    start -> text-start -> text-delta* -> text-end -> finish -> [DONE]

These helpers are pure string builders so the exact wire format can be unit-tested
without a running model. See https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol.
"""

import json
from typing import Any

# The header value the frontend transport requires to accept the stream.
UI_MESSAGE_STREAM_HEADER = "x-vercel-ai-ui-message-stream"
UI_MESSAGE_STREAM_VERSION = "v1"

# Single text block per response; the id only has to be stable within the stream.
TEXT_ID = "0"

DONE = "data: [DONE]\n\n"


def _frame(part: dict[str, Any]) -> str:
    """Encode one stream part as an SSE ``data:`` event (compact JSON, as the SDK emits)."""
    return f"data: {json.dumps(part, ensure_ascii=False, separators=(',', ':'))}\n\n"


def start() -> str:
    return _frame({"type": "start"})


def text_start() -> str:
    return _frame({"type": "text-start", "id": TEXT_ID})


def text_delta(delta: str) -> str:
    return _frame({"type": "text-delta", "id": TEXT_ID, "delta": delta})


def text_end() -> str:
    return _frame({"type": "text-end", "id": TEXT_ID})


def tool_input_available(tool_call_id: str, tool_name: str, tool_input: Any) -> str:
    return _frame(
        {
            "type": "tool-input-available",
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "input": tool_input,
        }
    )


def tool_output_available(tool_call_id: str, output: Any) -> str:
    return _frame({"type": "tool-output-available", "toolCallId": tool_call_id, "output": output})


def finish() -> str:
    return _frame({"type": "finish"})


def error(message: str) -> str:
    return _frame({"type": "error", "errorText": message})
