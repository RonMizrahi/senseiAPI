from typing import Any

import pytest

from assistant.tools import Tools


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_OPENAPI: dict[str, Any] = {
    "paths": {
        "/assistant/context/agenda": {
            "get": {"summary": "agenda", "parameters": [{"name": "days"}]}
        },
        "/assistant/context/patients": {"get": {"summary": "roster"}},
        "/patients": {"get": {"summary": "PHI roster"}, "post": {}},  # must stay hidden
        "/assistant/chat": {"post": {}},
    }
}


def _tools(fetch: Any, *, allow_all: bool = False) -> Tools:
    return Tools(base_url="http://api", fetch=fetch, allow_all_gets=allow_all)


def test_specs_advertises_both_tools() -> None:
    async def fetch(
        url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        return 200, {}

    names = {s["function"]["name"] for s in _tools(fetch).specs()}
    assert names == {"discover_api", "http_get"}


@pytest.mark.anyio
async def test_discover_returns_only_context_get_endpoints() -> None:
    async def fetch(
        url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        return 200, _OPENAPI

    result = await _tools(fetch).dispatch("discover_api", {})

    paths = {e["path"] for e in result["endpoints"]}
    assert paths == {"/assistant/context/agenda", "/assistant/context/patients"}
    assert "/patients" not in paths  # PHI route is never revealed to the model


@pytest.mark.anyio
async def test_http_get_allows_a_context_path_and_forwards_query() -> None:
    seen: dict[str, Any] = {}

    async def fetch(
        url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        seen["url"] = url
        seen["params"] = params
        return 200, {"ok": True}

    result = await _tools(fetch).dispatch(
        "http_get", {"path": "/assistant/context/agenda", "query": {"days": "7"}}
    )

    assert result == {"status": 200, "body": {"ok": True}}
    assert seen["url"] == "http://api/assistant/context/agenda"
    assert seen["params"] == {"days": "7"}


@pytest.mark.anyio
async def test_http_get_refuses_non_context_and_escape_paths() -> None:
    async def fetch(
        url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        raise AssertionError("fetch must not run for a refused path")

    tools = _tools(fetch)
    for bad in [
        "/patients",  # PHI
        "/assistant/chat",  # non-context
        "/assistant/context/../chat",  # traversal
        "http://evil.com/x",  # scheme/host escape
        "/assistant/context//evil.com",  # protocol-relative escape
    ]:
        result = await tools.dispatch("http_get", {"path": bad})
        assert "error" in result, bad


@pytest.mark.anyio
async def test_dispatch_unknown_tool_raises() -> None:
    async def fetch(
        url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        return 200, {}

    with pytest.raises(NotImplementedError):
        await _tools(fetch).dispatch("delete_everything", {})


@pytest.mark.anyio
async def test_discover_strips_endpoints_to_minimal_shape() -> None:
    async def fetch(
        url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        return 200, _OPENAPI

    result = await _tools(fetch).dispatch("discover_api", {})

    agenda = next(e for e in result["endpoints"] if e["path"] == "/assistant/context/agenda")
    assert agenda == {"path": "/assistant/context/agenda", "summary": "agenda", "params": ["days"]}
    assert "method" not in agenda  # redundant (GET-only) — stripped to save tokens
    roster = next(e for e in result["endpoints"] if e["path"] == "/assistant/context/patients")
    assert "params" not in roster  # omitted when empty


@pytest.mark.anyio
async def test_allow_all_discover_lists_every_get_endpoint() -> None:
    async def fetch(
        url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        return 200, _OPENAPI

    result = await _tools(fetch, allow_all=True).dispatch("discover_api", {})

    paths = {e["path"] for e in result["endpoints"]}
    assert "/patients" in paths  # PHI route now visible in allow-all mode
    assert "/assistant/context/agenda" in paths


@pytest.mark.anyio
async def test_allow_all_http_get_reaches_any_api_path() -> None:
    seen: dict[str, Any] = {}

    async def fetch(
        url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        seen["url"] = url
        return 200, {"ok": True}

    result = await _tools(fetch, allow_all=True).dispatch("http_get", {"path": "/patients"})

    assert result == {"status": 200, "body": {"ok": True}}
    assert seen["url"] == "http://api/patients"


@pytest.mark.anyio
async def test_allow_all_still_refuses_host_escape() -> None:
    async def fetch(
        url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        raise AssertionError("fetch must not run for a refused path")

    tools = _tools(fetch, allow_all=True)
    for bad in [
        "http://evil.com/x",  # absolute URL / host escape
        "patients",  # not absolute
        "/data/../../secret",  # traversal
        "/a//b",  # protocol-relative escape
    ]:
        result = await tools.dispatch("http_get", {"path": bad})
        assert "error" in result, bad
