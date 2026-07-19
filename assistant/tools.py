"""The assistant's tools: read-only, GET-only, same-host.

Two tools are exposed to the model:

- ``discover_api`` — reads this API's live OpenAPI spec and returns the available
  **GET** endpoints, stripped to the minimal shape (``_strip_endpoint``) to save tokens.
- ``http_get`` — issues a GET to a same-host path (SSRF/traversal guarded).

Scope is set by ``allow_all_gets``: when false (default) both tools are confined to the
PHI-safe ``/assistant/context/*`` surface — the architectural guardrail the system
prompt alone cannot provide; when true (demo) they reach any GET on this API, incl. PHI.

The HTTP fetcher is injected so tests never touch the network.
"""

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SAFE_PREFIX = "/assistant/context/"


class Fetcher(Protocol):
    """Performs one GET; returns (status_code, parsed_body)."""

    async def __call__(
        self, url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, Any]: ...


def _is_safe_path(path: str, *, allow_all: bool) -> bool:
    """A same-host GET path: absolute, no traversal, no host/scheme escape. When
    ``allow_all`` is false, additionally confined to the PHI-safe context namespace."""
    if not path.startswith("/") or ".." in path or "//" in path[1:]:
        return False
    return True if allow_all else path.startswith(SAFE_PREFIX)


def _strip_endpoint(path: str, operation: dict[str, Any]) -> dict[str, Any]:
    """The minimal shape the model needs to call a GET endpoint — path, and only a
    non-empty summary / param-name list. Everything else in the OpenAPI operation
    (responses, schemas, verb — always GET here) is dropped to save tokens."""
    entry: dict[str, Any] = {"path": path}
    summary = operation.get("summary")
    if summary:
        entry["summary"] = summary
    params = [
        p["name"] for p in operation.get("parameters", []) if isinstance(p, dict) and p.get("name")
    ]
    if params:
        entry["params"] = params
    return entry


class Tools:
    """Registry of the assistant's read-only tools."""

    def __init__(
        self,
        *,
        base_url: str,
        fetch: Fetcher,
        auth_header: str | None = None,
        allow_all_gets: bool = False,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._fetch = fetch
        self._headers = {"Authorization": auth_header} if auth_header else {}
        self._allow_all = allow_all_gets

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "discover_api",
                    "description": (
                        "מחזיר את רשימת נקודות הקצה הזמינות (GET בלבד) לשליפת מידע מהמערכת. "
                        "השתמשו בזה תחילה כדי לגלות אילו נתונים ניתן לשלוף ובאילו נתיבים."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "http_get",
                    "description": (
                        "שולף מידע בבקשת GET מנקודת קצה במערכת. השתמשו ב-path כפי "
                        "שהתקבל מ-discover_api."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "נתיב במערכת כפי שהתקבל מ-discover_api.",
                            },
                            "query": {
                                "type": "object",
                                "description": "פרמטרי שאילתה אופציונליים.",
                                "additionalProperties": {"type": "string"},
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
        ]

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "discover_api":
            return await self._discover()
        if name == "http_get":
            return await self._http_get(arguments.get("path", ""), arguments.get("query"))
        raise NotImplementedError(f"tool {name!r} is not implemented")

    async def _discover(self) -> dict[str, Any]:
        status, spec = await self._fetch(f"{self._base}/openapi.json", headers=self._headers)
        if status != 200 or not isinstance(spec, dict):
            return {"error": "could not load the API description"}
        endpoints = []
        for path, methods in spec.get("paths", {}).items():
            if not self._allow_all and not path.startswith(SAFE_PREFIX):
                continue
            operation = methods.get("get")  # GET only — other verbs are irrelevant here
            if operation is not None:
                endpoints.append(_strip_endpoint(path, operation))
        return {"endpoints": endpoints}

    async def _http_get(self, path: str, query: dict[str, str] | None) -> dict[str, Any]:
        if not _is_safe_path(path, allow_all=self._allow_all):
            logger.warning("assistant http_get refused non-allow-listed path: %s", path)
            return {"error": f"refused: {path} is not an allowed path"}
        params = {k: str(v) for k, v in (query or {}).items()}
        status, body = await self._fetch(
            f"{self._base}{path}", headers=self._headers, params=params
        )
        return {"status": status, "body": body}
