"""Unit tests for the pure logic in the demo seed script.

The seed's network/DB effects need a running API + Postgres, but its idempotency
hinges on _event_key being a stable, timezone-independent identity for a meeting —
that is worth locking, and every seeded appointment must reference a known patient.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from scripts.seed_demo import APPOINTMENTS, PAST_SESSIONS, PATIENTS, _event_key


def test_event_key_is_timezone_independent() -> None:
    # The same instant expressed in two zones must produce the same dedupe key,
    # so re-running the seed against times an endpoint echoes back never duplicates.
    israel = datetime(2026, 7, 20, 9, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    same_instant_utc = israel.astimezone(UTC)
    assert _event_key("p1", israel) == _event_key("p1", same_instant_utc)


def test_event_key_differs_by_patient_and_time() -> None:
    t = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    assert _event_key("p1", t) != _event_key("p2", t)
    assert _event_key("p1", t) != _event_key("p1", t.replace(hour=10))


def test_every_seeded_meeting_references_a_known_patient() -> None:
    names = {p["name"] for p in PATIENTS}
    assert {a[0] for a in APPOINTMENTS} <= names
    assert {s[0] for s in PAST_SESSIONS} <= names


def test_every_past_session_carries_summary_text() -> None:
    assert all(text.strip() for *_, text in PAST_SESSIONS)
