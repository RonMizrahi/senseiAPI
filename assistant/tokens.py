"""Token counting + history trimming, to keep the prompt under a token ceiling.

Uses ``tiktoken`` (OpenAI's tokenizer) when available, falling back to a rough
character estimate so it never breaks offline. The counter is injectable so tests
stay hermetic (no tokenizer download).
"""

from collections.abc import Callable, Sequence
from typing import Any

# Small fixed overhead per chat message (role/formatting), matching OpenAI's guidance.
_PER_MESSAGE_TOKENS = 4

_counter: Callable[[str], int] | None = None


def _build_counter() -> Callable[[str], int]:
    try:
        import tiktoken

        # o200k_base is the encoding for the GPT-4o / GPT-5 families.
        encoding = tiktoken.get_encoding("o200k_base")
        return lambda text: len(encoding.encode(text))
    except Exception:
        # Rough fallback (~4 chars/token) if tiktoken is unavailable/offline.
        return lambda text: max(1, len(text) // 4)


def count_tokens(text: str) -> int:
    global _counter
    if _counter is None:
        _counter = _build_counter()
    return _counter(text)


def _message_tokens(message: dict[str, Any], count: Callable[[str], int]) -> int:
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    return count(text) + _PER_MESSAGE_TOKENS


def trim_to_token_budget(
    messages: Sequence[dict[str, Any]],
    max_tokens: int,
    *,
    count: Callable[[str], int] = count_tokens,
) -> list[dict[str, Any]]:
    """Drop the oldest messages until the prompt fits ``max_tokens``.

    The leading system message and the most recent messages are always kept (at least
    the latest question), so trimming never removes the guardrails or the live turn.
    """
    messages = list(messages)
    if not messages:
        return messages

    head = messages[:1] if messages[0].get("role") == "system" else []
    tail = messages[len(head) :]

    budget = max_tokens - sum(_message_tokens(m, count) for m in head)
    kept: list[dict[str, Any]] = []
    for message in reversed(tail):
        cost = _message_tokens(message, count)
        if kept and budget - cost < 0:
            break  # keep at least the latest message, then stop once full
        budget -= cost
        kept.append(message)
    kept.reverse()
    return head + kept
