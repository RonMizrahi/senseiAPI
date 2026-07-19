"""Seed the running API with the demo roster, meetings, and past-session summaries.

The frontend's offline roster lives in SENSEI/src/data/mockPatients.ts, but in
API-connected mode the app reads patients/calendar/summaries from THIS backend — so
the database needs its own seed. This mirrors that roster and, for the assistant to
answer "summarize the last session with X", also materializes a few PAST meetings
plus a ready summary for each (demo text, model="demo").

Meetings are created over the API; summaries have no POST endpoint (they are produced
by the audio pipeline), so they are written directly via SummaryRepository.mark_ready.

Usage (with the API running and ENABLE_SECURITY off, i.e. the dev default):
    python scripts/seed_demo.py            # -> http://localhost:8000
    BASE_URL=http://localhost:8000 python scripts/seed_demo.py

Idempotent: patients are keyed by name, meetings by (patient, start time), and
mark_ready overwrites the one summary row per meeting — so re-running inserts nothing new.
"""

import json
import os
import sys
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Make repo modules (core, summaries) importable when run directly as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

PATIENTS = [
    {"name": "דנה לוי", "phone": "054-1234567", "email": "dana.l@mail.com"},
    {"name": "יוסי מזרחי", "phone": "052-7654321", "email": "yossi.m@mail.com"},
    {"name": "מיכל כהן", "phone": "053-9988776", "email": "michal.c@mail.com"},
    {"name": "אבי פרץ", "phone": "054-3322110", "email": "avi.p@mail.com"},
    {"name": "סימבה", "phone": "054-9876543", "email": "simba@mail.com"},
]

# (patient name, days from now, HH:MM local, title) — upcoming.
APPOINTMENTS = [
    ("דנה לוי", 1, "09:00", "פגישה שבועית"),
    ("דנה לוי", 8, "13:00", "פגישת מעקב"),
    ("יוסי מזרחי", 2, "10:00", "פגישה שבועית"),
    ("יוסי מזרחי", 9, "15:00", "פגישת מעקב"),
    ("מיכל כהן", 3, "11:00", "פגישה שבועית"),
    ("מיכל כהן", 10, "09:30", "פגישת וידאו"),
    ("אבי פרץ", 4, "12:00", "פגישת מעקב"),
    ("אבי פרץ", 11, "16:00", "פגישה שבועית"),
    ("סימבה", 6, "10:00", "פגישת המשך"),
]

# Past-session demo summaries, reused from the monorepo migration 0006_seed_demo_sessions.sql.
_S1 = (
    "המטופל דיווח על שיפור מתון בתחושת השליטה במצבי לחץ. "
    "תרגלנו נשימה סרעפתית והרחבנו את חשיפה ההדרגתית."
)
_S2 = (
    "עלו תכנים סביב חרדת ביצוע בעבודה. "
    "זוהו דפוסי הימנעות חוזרים; הוגדרה משימה התנהגותית לשבוע הקרוב."
)
_S3 = (
    "הפגישה התמקדה ביחסים בין-אישיים ובקושי להציב גבולות. "
    "נצפתה עלייה ברגישות רגשית בהשוואה לפגישות קודמות."
)
_S4 = "דיווח על מצב רוח ירוד יחסית. בחנו טריגרים אפשריים ועיבדנו אירוע משמעותי מהשבוע."
_S5 = "התקדמות יפה במטרות הטיפול. המטופל מתאר שימוש עצמאי בכלים שנלמדו."
_S6 = "פגישת פתיחה: מיפוי תלונות עיקריות, היסטוריה והגדרת ציפיות."

# (patient name, days AGO, HH:MM local, title, summary text)
PAST_SESSIONS = [
    ("דנה לוי", 7, "09:00", "פגישה שבועית", _S1),
    ("דנה לוי", 14, "09:00", "פגישה שבועית", _S5),
    ("יוסי מזרחי", 5, "10:00", "פגישה שבועית", _S2),
    ("יוסי מזרחי", 12, "10:00", "פגישה שבועית", _S6),
    ("מיכל כהן", 6, "11:00", "פגישה שבועית", _S3),
    ("מיכל כהן", 13, "11:00", "פגישה שבועית", _S4),
    ("אבי פרץ", 8, "12:00", "פגישת מעקב", _S4),
    ("סימבה", 9, "10:00", "פגישת המשך", _S1),
]


def _api(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request) as response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw else None)


def _event_key(patient_id: str, start: datetime) -> tuple[str, str]:
    """A stable identity for a meeting — patient + the start instant to the minute,
    normalized to UTC so it matches regardless of the timezone an endpoint echoes back."""
    return (patient_id, start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"))


def _existing_events() -> dict[tuple[str, str], str]:
    """Map (patient_id, start-minute-UTC) -> event id for meetings already in the DB.

    The window stays under the calendar API's 365-day range cap and comfortably spans
    every seeded meeting (past sessions ≤ ~2 weeks ago, appointments ≤ ~2 weeks ahead)."""
    now = datetime.now(ISRAEL_TZ)
    window = timedelta(days=60)
    _, events = _api(
        "GET",
        f"/calendar?from={(now - window).date()}&to={(now + window).date()}",
    )
    keys: dict[tuple[str, str], str] = {}
    for event in events or []:
        if event.get("patient_id"):
            start = datetime.fromisoformat(event["start_at"])
            keys[_event_key(event["patient_id"], start)] = event["id"]
    return keys


def _ensure_event(
    existing: dict[tuple[str, str], str], patient_id: str, start: datetime, title: str
) -> str:
    """Return the id of the meeting at this time for this patient, creating it if absent."""
    key = _event_key(patient_id, start)
    if key in existing:
        return existing[key]
    _, created = _api(
        "POST",
        "/calendar",
        {
            "title": title,
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(minutes=50)).isoformat(),
            "patient_id": patient_id,
        },
    )
    event_id = str(created["id"])
    existing[key] = event_id
    return event_id


async def _write_summaries(pairs: list[tuple[str, str]]) -> None:
    """Write a ready demo summary for each (meeting_id, text) directly via the DB."""
    import calendar_events.orm  # noqa: F401  # register calendar_events for the summary FK
    from core.config import get_settings
    from core.database import close_database, get_sessionmaker
    from summaries.repository import SummaryRepository

    database_url = get_settings().database_url
    assert database_url, "database_url must be set to seed summaries"
    sessionmaker = get_sessionmaker(database_url)
    try:
        async with sessionmaker() as session:
            repo = SummaryRepository(session)
            for meeting_id, text in pairs:
                await repo.mark_ready(uuid.UUID(meeting_id), text=text, model="demo")
    finally:
        # Dispose the asyncpg pool cleanly before the event loop closes.
        await close_database(database_url)


def main() -> None:
    _, existing_patients = _api("GET", "/patients")
    ids_by_name: dict[str, str] = {p["name"]: p["id"] for p in existing_patients or []}

    for patient in PATIENTS:
        if patient["name"] in ids_by_name:
            print(f"patient exists: {patient['name']}")
            continue
        _, created = _api("POST", "/patients", patient)
        ids_by_name[patient["name"]] = created["id"]
        print(f"created patient: {patient['name']}")

    now = datetime.now(ISRAEL_TZ)
    events = _existing_events()

    for name, day_offset, hhmm, title in APPOINTMENTS:
        patient_id = ids_by_name.get(name)
        if not patient_id:
            continue
        hour, minute = (int(x) for x in hhmm.split(":"))
        start = (now + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        _ensure_event(events, patient_id, start, title)
        print(f"appointment: {name} +{day_offset}d {hhmm}")

    summaries: list[tuple[str, str]] = []
    for name, days_ago, hhmm, title, text in PAST_SESSIONS:
        patient_id = ids_by_name.get(name)
        if not patient_id:
            continue
        hour, minute = (int(x) for x in hhmm.split(":"))
        start = (now - timedelta(days=days_ago)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        meeting_id = _ensure_event(events, patient_id, start, title)
        summaries.append((meeting_id, text))
        print(f"past session: {name} -{days_ago}d {hhmm}")

    import asyncio

    asyncio.run(_write_summaries(summaries))
    print(f"summaries written: {len(summaries)}")
    print("done.")


if __name__ == "__main__":
    main()
