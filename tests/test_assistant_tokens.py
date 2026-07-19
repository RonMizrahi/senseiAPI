from assistant.tokens import trim_to_token_budget


# Deterministic 1-token-per-char counter so the tests need no real tokenizer.
def _count(text: str) -> int:
    return len(text)


def _msg(role: str, char: str, n: int) -> dict[str, str]:
    return {"role": role, "content": char * n}


def test_no_trim_when_under_budget() -> None:
    messages = [_msg("system", "S", 5), _msg("user", "a", 5)]
    assert trim_to_token_budget(messages, 10_000, count=_count) == messages


def test_drops_oldest_history_but_keeps_system_and_latest() -> None:
    messages = [
        _msg("system", "S", 10),
        _msg("user", "a", 10),  # oldest — should be dropped
        _msg("assistant", "b", 10),
        _msg("user", "c", 10),  # latest — must survive
    ]

    trimmed = trim_to_token_budget(messages, max_tokens=40, count=_count)

    firsts = [m["content"][0] for m in trimmed]
    assert firsts[0] == "S"  # system kept at the front
    assert "c" in firsts  # latest question kept
    assert "a" not in firsts  # oldest dropped to fit the budget


def test_keeps_latest_message_even_if_it_exceeds_the_budget() -> None:
    messages = [_msg("system", "S", 1), _msg("user", "x", 100)]

    trimmed = trim_to_token_budget(messages, max_tokens=1, count=_count)

    assert trimmed[-1]["content"] == "x" * 100  # never drop the live turn
