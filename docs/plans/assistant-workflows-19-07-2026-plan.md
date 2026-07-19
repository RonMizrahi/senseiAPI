# Plan — Make the Sensei assistant fully work e2e: API-grounded workflows, numeric dates, patient/session fetch, recommended questions

> **Prior phase (DONE):** the assistant is already wired end-to-end (FastAPI `/assistant/chat` ↔ `useChat`, OpenAI, tool loop, PHI allow-list, token caps). That work shipped and is recorded in `senseiAPI/docs/plans/assistant-context-tools-19-07-2026-plan.md`. **This plan is the follow-up: close the remaining functional gaps so the assistant reliably answers real questions about patients and their sessions.**

## Context

Live-probing the running system surfaced three concrete defects the user reported:

1. **Bad timestamps.** `/calendar` and `/patients` return **raw ISO** (`2026-07-20T09:00:00+03:00`, `created_at:…Z`). With `ASSISTANT_ALLOW_ALL_GETS=true` the model reaches those endpoints, and the system prompt currently says *"present dates as received, don't convert"* — so raw ISO leaks into answers. Only `/assistant/context/*` pre-formats (via `_readable`).
2. **"No meeting identifier" for some patients.** Session content lives at `GET /meetings/{meeting_id}/summary` where `meeting_id == calendar_events.id`. But **no GET endpoint lists a patient's meetings with their ids**, and the seed has **zero past meetings and zero summaries** (all 9 appointments are future; summaries are only created by the audio pipeline). So the model has no id to fetch and no content behind it → it says it lacks a meeting identifier.
3. **Suggestion chips ask for unanswerable data** ("high-risk patients", "summarize Michal's trend") — there is no risk endpoint and no seeded summaries.

**Intended outcome:** a therapist can ask "what's on today?", "when did I last meet דנה?", and "summarize my last session with מיכל" and get correct, naturally-worded answers with **numeric dates** (`20/07/2026 09:00`); the assistant reliably chains name → patient → meeting → summary; the panel's recommended questions match what actually works; verified by an independent `qa-engineer` e2e pass.

### Decisions (confirmed with user)
- **Timestamp format:** numeric `dd/MM/yyyy HH:mm` (`20/07/2026 09:00`), rendered `dir=ltr`.
- **Seed depth:** seed past meetings **and** demo `ready` summaries (labeled demo), reusing the Hebrew summary text from the monorepo migration `SENSEI-MONOREPO/apps/api/db/migrations/0006_seed_demo_sessions.sql`.
- **Reliability:** add a dedicated per-patient meetings endpoint (deterministic 2-step chain), not prompt-only workflows.
- **Workflows live in the system prompt** as "playbooks" (recipes over the real API) — the architecture is a stateless tool loop, so guided recipes in the prompt are the workflow mechanism.
- **Branches:** REUSE the existing feature branches (backend `assistant/openai-chat-endpoint` / frontend `assistant/usechat-streaming`, PRs #1/#5) — do NOT create new branches. **Squash to a single commit per repo.**

### Grounding facts (from code + live probes)
- One identifier only: `calendar_events.id` = `meeting_id` = `event_id` (`summaries/orm.py:16`, `transcripts/orm.py:18`).
- `/calendar` filters by **date range only** (no `patient_id` filter); no `GET /patients/{id}`; no name search; no transcript route.
- `_readable` (`assistant/context.py:28-31`) is the single formatter used by agenda + cadence — changing it fixes all context timestamps at once.
- Summaries have no POST API (created via audio pipeline: `audio/router.py:110-116`); repository has `create_pending` + `mark_ready` (`summaries/repository.py:39,57`) → seed must write via DB, not API.

---

## Milestone 1 — Backend: patient-meetings endpoint, numeric dates, session seed, prompt playbooks
**Repo:** `senseiAPI` · **Branch:** REUSE `assistant/openai-chat-endpoint` (do not branch anew). Follow `senseiAPI/AGENTS.md` (FastAPI/Python). Load `testing-standards`.

### Steps
1. **Numeric timestamps** (`assistant/context.py:28-31`) — change `_readable` to `dt.astimezone(_ISRAEL_TZ).strftime("%d/%m/%Y %H:%M")` → `"20/07/2026 09:00"`. Fixes agenda + cadence + the new endpoint uniformly. Update the docstring/comment.
2. **New endpoint** `GET /assistant/context/patient/{patient_id}/meetings` (add to `assistant/context.py`, reuse `CalendarEventRepository`, `PatientRepository`, `SummaryRepository`):
   - Response model `PatientMeeting { meeting_id: str, title: str | None, starts_at: str (numeric readable), has_summary: bool }`, newest-first over a ±365d window (mirror the cadence window).
   - `has_summary` via `SummaryRepository.get_by_meeting_id(...)` (status `ready`) per event — few events, fine. This is the id source that ends the "no meeting identifier" failure. `title` included (demo + allow-all); note it may hold free text.
   - Guard DB errors like `patient_cadence` (`context.py:138-144`).
3. **System-prompt playbooks** (`assistant/prompt.py`) — add `## תהליכי עבודה (Playbooks)` mapping questions → exact GET chains, and rewrite the `## הצגת התשובה` date rule:
   - רשימת מטופלים → `/assistant/context/patients`; יומן/מי הבא/היום → `/assistant/context/agenda?days=N`.
   - קצב/מתי נפגשנו/כמה פגישות → `patients` (שם→מזהה) → `/assistant/context/patient/{id}/cadence`.
   - פגישות מטופל/סיכום פגישה → `patients` (שם→מזהה) → `/assistant/context/patient/{id}/meetings` (`meeting_id`, זמן, `has_summary`) → `/meetings/{meeting_id}/summary`.
   - Date rule → **prefer these context endpoints (times already numeric); present dates as `dd/MM/yyyy HH:mm`; never show ISO; convert any raw ISO.** Keep the existing no-API-paths / `#/patient/<id>` link / risk stanzas.
4. **Seed past sessions + summaries** — extend `scripts/seed_demo.py` (or new `scripts/seed_sessions.py`): per patient, insert a few **past** calendar events + matching `ready` `summaries` rows using the Hebrew `text` from `0006_seed_demo_sessions.sql`. Meetings POST via API with past `start_at`; **summaries via the DB** (async `SummaryRepository.create_pending` + `mark_ready`, `model="demo"`) — no summary POST exists. Idempotent (fixed UUIDs / conflict guard).

### Test steps (`testing-standards`; fakes/DB fixtures, never real OpenAI)
- Update existing `_readable`/context tests to assert numeric format (`"20/07/2026 09:00"`).
- Unit: meetings service builds `PatientMeeting` list (order, `has_summary` true/false, readable time) from fake repositories.
- Integration (`TestClient`): `GET /assistant/context/patient/{id}/meetings` → `200` + shape; unknown/malformed id → graceful (no 500).
- Seed: importable + idempotent (second run inserts 0 new).

### Quality gate
Gate A `code-quality-pipeline`, then `ruff check .` · `ruff format --check .` · `mypy .` (py3.11 venv) · `pytest -m "not integration"`. **Squash the branch to one commit**; update PR #1 via `pr-mr-prepare`.

---

## Milestone 2 — Frontend: recommended questions aligned to the working workflows
**Repo:** `SENSEI` · **Branch:** REUSE `assistant/usechat-streaming` (PR #5). Load `testing-standards`.

### Steps
1. **Rewrite `SUGGESTIONS`** (`src/components/layout/AiAssistant.tsx:59-63`) to questions that resolve e2e via the M1 workflows (shared across live + mock). Proposed (Hebrew, plural voice, no emoji):
   - `מה יש לי ביומן היום?` → agenda.
   - `מתי נפגשתי לאחרונה עם דנה לוי?` → cadence.
   - `סכמו את הפגישה האחרונה עם מיכל כהן` → meetings → summary.
   Drop the "high-risk patients" chip (no data source).
2. **Changelog + version** — new `CHANGELOG.md` entry + bump `package.json` version + README badge together (guarded triple).

### Test steps
- Update the suggestion-chip test to assert new labels and that clicking sends the mapped question. Keep `tests/aiAssistantLive.test.tsx` green.
- `npm run lint` · `npm run typecheck` · `npm test` · `npm run build`.

### Quality gate
Gate A `code-quality-pipeline` on the changed file. Commit to `assistant/usechat-streaming`; **squash to one commit**; PR #5 updated.

---

## Milestone 3 — Integrated e2e verification & QA (acceptance gate)
1. Restart backend on M1 code + re-seed (`ASSISTANT_ALLOW_ALL_GETS=true`, real key); restart frontend on M2 code (`local-deploy`).
2. **Reproduce-then-confirm** each reported bug in the browser (Playwright MCP):
   - "מה יש לי ביומן היום?" → dates read `dd/MM/yyyy HH:mm`, **no ISO**.
   - "מתי נפגשתי לאחרונה עם דנה לוי?" → numeric date, correct patient.
   - "סכמו את הפגישה האחרונה עם מיכל כהן" → assistant chains patients→meetings→summary, returns the seeded summary — **no "no meeting identifier"**.
   - Tool chips expand; `#/patient/<id>` renders as "פתיחת הכרטיס".
3. **Hand to `qa-engineer`** on the running system: full green-path coverage of the three workflows across all 5 patients + break-it (unknown patient, patient with no summaries, injection, oversized input). Convert confirmed findings into committed tests. Gate on verdict.

---

## Close-out (after PRs)
- Mark milestones `[DONE]` here + append the QA verdict.
- Update `senseiAPI/AGENTS.md` (+ `CLAUDE.md`): new meetings endpoint, numeric `_readable`, session seed step, prompt playbooks. Update `SENSEI/CLAUDE.md` if needed. Save a dated copy of this plan under each repo's `docs/plans/`.

## Verification (how to test e2e)
- **Pre-fix repro:** `curl "http://localhost:8000/calendar?from=…&to=…"` shows raw ISO; a summary request yields "no meeting identifier".
- **Post-fix:** `curl http://localhost:8000/assistant/context/patient/<id>/meetings` → `[{meeting_id, title, starts_at:"20/07/2026 09:00", has_summary:true}]`; `curl /meetings/<meeting_id>/summary` → seeded Hebrew text; browser probes (M3 step 2) pass; backend `pytest` + frontend `npm test`/`build` green; `qa-engineer` verdict PASS.

## Risks / notes
- Changing `_readable` breaks existing format assertions — update them in the same commit (caught by `pytest`).
- Seeding summaries touches the DB directly (no API) — seed script only, idempotent, demo-gated; never a runtime endpoint.
- `title` on the meetings endpoint may hold free text — acceptable under demo `allow_all_gets`.
- Reused monorepo summary text targets a *different* backend's schema — copy `text` strings only (senseiAPI `summaries` has no `insight` column).
- **Branch reuse + squash:** both repos keep their existing branch/PR; history collapses to one commit each (`git reset --soft <base> && commit`, or interactive squash) — confirm the base before squashing so earlier assistant commits aren't lost.
