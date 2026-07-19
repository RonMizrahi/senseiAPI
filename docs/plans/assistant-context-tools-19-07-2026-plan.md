# Plan — Trauma-informed prompt + PHI-safe context tools + expandable tool-call UI

> **STATUS (2026-07-19): MA [DONE] · MB [DONE] · MC [DONE] — all verified live end-to-end.**
> - **Backend** (PR #1, branch `assistant/openai-chat-endpoint`): trauma-informed prompt, PHI-safe `/assistant/context/*` API, `discover_api` + `http_get` tools (allow-listed), streaming tool-call loop emitting AI-SDK tool parts. `ruff`/`ruff format`/`mypy`/`pytest` → **149 passed**.
> - **Frontend** (PR #5, branch `assistant/usechat-streaming`): expandable 1-line tool-call chips (v1.3.0). `lint`/`typecheck`/`test` (**365 passed**)/`build` green.
> - **Gate A:** tool-loop review (no bugs) + adversarial security review — allow-list held against every bypass class (traversal/encoding/`@`/`#`/`?`/absolute-URL/SSRF); one PHI finding fixed (dropped free-text `title` from agenda).
> - **Live verification (Gate B + QA):** browser → "מי הבא?" → model calls `discover_api` then `http_get /assistant/context/agenda` → two collapsed tool chips render → expand shows full request/response → grounded, honest answer citing the API call. Adversarial probes: bad `days`/uuid → 422; roster = `{id,name}` only; agenda = `{patient_name,starts_at}` only (no PHI). Verified with a real OpenAI key + `gpt-5.4-mini` + Postgres.
> - **Key decision:** the PHI boundary is the tool allow-list (architecture), not the prompt — matching the research's central caveat that a prompt alone can't enforce safety.


**Date:** 2026-07-19 · **Branches:** extend the existing open PRs — backend on `assistant/openai-chat-endpoint` (PR #1), frontend on `assistant/usechat-streaming` (PR #5); this is all one feature. · **Research:** `docs/research/trauma-therapist-assistant-prompt-19-07-2026-research.md`.

## Context

The assistant currently streams generic answers with a generic Hebrew prompt and **no tools** (`Tools.specs()` → `[]`). The user wants: (1) a **hardened, trauma-informed system prompt** (researched, approved); (2) two **real tools** — `http_get` (GET-only) + `discover_api` (reads live OpenAPI) — grounded on a **PHI-free** data surface; (3) an **expandable 1-line tool-call UI** in the panel. Key research finding: a prompt alone can't enforce safety — the **allow-listed, PHI-free tool layer is the architectural guardrail** that makes it safe.

## Milestone A — Backend: PHI-safe context API + hardened prompt
**Repo:** senseiAPI · branch `assistant/openai-chat-endpoint`.

1. **Hardened prompt** — replace `ASSISTANT_SYSTEM_PROMPT` in `assistant/prompt.py` with the approved trauma-informed Hebrew prompt (see research doc). Update the `summaries`-style module docstring.
2. **PHI-safe context router** — new `assistant/context.py`: `router = APIRouter(prefix="/assistant/context", tags=["assistant-context"])`, mounted in `main.py` with `Depends(get_current_user)`. Pydantic response models expose **only** non-PHI fields.
   - `GET /patients` → `[{ id, name }]` (from `PatientRepository.list_all()`, **drop phone/email**).
   - `GET /agenda?days=7` → upcoming events `[{ title, patient_name, starts_at }]` (from `CalendarEventRepository.list_all(now, now+days)`, resolve `patient_name` via a patient-id→name map; **drop description/notes**). Cap `days` (1–60).
   - `GET /patient/{patient_id}/cadence` → `{ patient_name, last_meeting_at, next_meeting_at, total_meetings }` (list a wide window, filter by `patient_id` in the service; timestamps only, **no clinical content**).
   - Service builds on the existing repositories; DB optional (empty DB → empty lists, which the assistant handles).
3. **Tests** — `tests/test_assistant_context.py`: `TestClient` with `get_db_session`/repos overridden by fakes (seeded events/patients with random UUIDs); assert each endpoint's 2xx **shape** and that **no phone/email/description** leaks. Include empty-DB → `[]`.
4. **Quality:** `ruff` · `ruff format` · `mypy` · `pytest`, then the code-quality pipeline.

## Milestone B — Backend: the two tools + tool-call streaming
**Repo:** senseiAPI · same branch.

1. **`assistant/tools.py`** — implement:
   - `discover_api()` → fetches the live `GET {self_base}/openapi.json`, **filters `paths` to `/assistant/context/*`**, returns a compact list of `{ method, path, summary, params }`. So the model only ever learns the safe surface.
   - `http_get(path, query?)` → **allow-list guard**: reject anything not under `/assistant/context/`; issue the GET against the app's own base URL, forwarding the caller's `Authorization` header; return status + JSON (truncated). No other verb.
   - `specs()` returns both tool schemas; `dispatch(name, args)` routes to them (no more blanket `NotImplementedError`).
2. **`assistant/client.py`** — run the OpenAI **tool-call loop**: when the model emits tool calls, execute via `Tools.dispatch`, feed results back, continue until a final text answer. Emit AI-SDK stream parts for each: `tool-input-start` / `tool-input-available` / `tool-output-available` (so the UI can render them), then the text deltas. Keep the existing sanitize/close/guard behavior.
3. **Config** — `assistant_self_base_url` (default `http://localhost:8000`) so the tools can call back into this app; document in `.env.example`.
4. **Tests** — tool unit tests (allow-list rejects non-`/assistant/context` paths; `discover_api` filters to the safe namespace; `dispatch` routes) + a service test asserting the tool-call loop emits `tool-*` parts then text, all with a **fake** OpenAI client (never real API, never real network — inject a fake fetcher).
5. **Quality:** same gates + pipeline.

## Milestone C — Frontend: expandable tool-call UI
**Repo:** SENSEI · branch `assistant/usechat-streaming`.

1. **Render tool parts** — `useChat` message `parts` now include tool parts (`tool-…`). In `AiAssistant.tsx`, render each as a **collapsed 1-line chip** — `discovery` or `call api <path>` — with a disclosure control.
2. **Expand on click** → show the full interaction: the tool input (args / discovered paths) and the tool output (the API response), in a scrollable, monospaced, `dir="ltr"` block (technical strings). Keep Hebrew labels, logical CSS, tokens, a11y (button role, `aria-expanded`).
3. **Tests** — extend `aiAssistantLive.test.tsx` (or a new file): mock an SSE stream containing `tool-input-available` + `tool-output-available` + text; assert the collapsed chip renders and expands to the full request/response.
4. **Changelog + version bump** (guarded trio) to 1.3.0.
5. **Quality:** `lint` · `typecheck` · `test` · `build`, then the pipeline.

## Verification
Boot backend (with Postgres for real agenda/cadence data, or empty DB → graceful) + frontend; ask "מי הבא?" / "מתי נפגשתי לאחרונה עם X?" → the assistant calls `discover_api` then `http_get`, the panel shows the collapsed tool chips, and clicking expands the full `/assistant/context/*` request+response. Re-run the qa-engineer adversarial pass focused on the tool allow-list (attempt `http_get` to a PHI route like `/patients` → must be refused).

## Guardrails / decisions
- **PHI boundary = the tool allow-list**, not the prompt (per the research's key caveat). `http_get` refuses any non-`/assistant/context/*` path; `discover_api` never reveals PHI routes.
- Demo, authenticated: the tools forward the caller's bearer token; in dev (`ENABLE_SECURITY=false`) that's the `TEST_USER`.
- Names are seeded **demo** patients (not real PHI); the safe-namespace boundary still generalizes to real deployments.
