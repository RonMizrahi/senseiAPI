import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from assistant.context import AssistantContextService, get_context_service
from calendar_events.models import CalendarEvent
from main import app
from patients.models import Patient
from summaries.models import StoredSummary

NOW = datetime.now(UTC)
# Numeric local timestamp, e.g. "21/07/2026 10:59".
_NUMERIC_TIME = re.compile(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$")


def _patient(name: str) -> Patient:
    return Patient(
        id=uuid.uuid4(),
        name=name,
        phone="050-0000000",
        email="secret@example.com",
        created_at=NOW,
    )


def _event(title: str, start: datetime, patient_id: uuid.UUID | None) -> CalendarEvent:
    return CalendarEvent(
        id=uuid.uuid4(),
        title=title,
        description="PRIVATE CLINICAL NOTE",
        start_at=start,
        end_at=start + timedelta(hours=1),
        created_at=NOW,
        therapist_id=uuid.uuid4(),
        patient_id=patient_id,
    )


class _FakePatients:
    def __init__(self, patients: list[Patient]) -> None:
        self._patients = patients

    async def list_all(self) -> list[Patient]:
        return self._patients


class _FakeEvents:
    def __init__(self, events: list[CalendarEvent]) -> None:
        self._events = events

    async def list_all(self, *, from_at: datetime, to_at: datetime) -> list[CalendarEvent]:
        return [e for e in self._events if from_at <= e.start_at < to_at]


def _summary(meeting_id: uuid.UUID, status: str) -> StoredSummary:
    return StoredSummary(
        id=uuid.uuid4(),
        meeting_id=meeting_id,
        status=status,  # type: ignore[arg-type]
        text="סיכום דמו" if status == "ready" else None,
        model="demo",
        error=None,
        created_at=NOW,
        updated_at=NOW,
    )


class _FakeSummaries:
    def __init__(self, by_meeting: dict[uuid.UUID, str] | None = None) -> None:
        self._by_meeting = by_meeting or {}

    async def get_by_meeting_id(self, meeting_id: uuid.UUID) -> StoredSummary | None:
        status = self._by_meeting.get(meeting_id)
        return _summary(meeting_id, status) if status else None


def _client(
    patients: list[Patient],
    events: list[CalendarEvent],
    summaries: dict[uuid.UUID, str] | None = None,
) -> TestClient:
    service = AssistantContextService(
        patients=_FakePatients(patients),  # type: ignore[arg-type]
        events=_FakeEvents(events),  # type: ignore[arg-type]
        summaries=_FakeSummaries(summaries),  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_context_service] = lambda: service
    return TestClient(app)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_patients_returns_name_only_and_never_leaks_contact() -> None:
    dana = _patient("דנה לוי")
    client = _client([dana], [])

    res = client.get("/assistant/context/patients")

    assert res.status_code == 200
    body = res.json()
    assert body == [{"id": str(dana.id), "name": "דנה לוי"}]
    # PHI must be structurally absent, not just unused.
    assert "phone" not in body[0]
    assert "email" not in body[0]


def test_agenda_lists_upcoming_meetings_without_clinical_notes() -> None:
    dana = _patient("דנה לוי")
    upcoming = _event("פגישה שבועית", NOW + timedelta(days=1), dana.id)
    past = _event("פגישה שעברה", NOW - timedelta(days=2), dana.id)
    client = _client([dana], [upcoming, past])

    res = client.get("/assistant/context/agenda?days=7")

    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["patient_name"] == "דנה לוי"
    # Time is a numeric local string (dd/mm/yyyy HH:MM), not raw ISO.
    assert _NUMERIC_TIME.match(body[0]["starts_at"])
    # Free-text title and clinical description must never be exposed.
    assert "title" not in body[0]
    assert "description" not in body[0]
    assert "פגישה שבועית" not in res.text
    assert "PRIVATE CLINICAL NOTE" not in res.text


def test_cadence_reports_last_next_and_total() -> None:
    dana = _patient("דנה לוי")
    events = [
        _event("a", NOW - timedelta(days=10), dana.id),
        _event("b", NOW - timedelta(days=3), dana.id),
        _event("c", NOW + timedelta(days=4), dana.id),
    ]
    client = _client([dana], events)

    res = client.get(f"/assistant/context/patient/{dana.id}/cadence")

    assert res.status_code == 200
    body = res.json()
    assert body["patient_name"] == "דנה לוי"
    assert body["total_meetings"] == 3
    assert body["last_meeting_at"] is not None and "T" not in body["last_meeting_at"]
    assert body["next_meeting_at"] is not None and "T" not in body["next_meeting_at"]


def test_patient_meetings_lists_ids_newest_first_with_summary_flag() -> None:
    dana = _patient("דנה לוי")
    older = _event("פגישה שעברה", NOW - timedelta(days=10), dana.id)
    newer = _event("פגישה אחרונה", NOW - timedelta(days=3), dana.id)
    other = _event("לא שלה", NOW - timedelta(days=1), uuid.uuid4())
    client = _client([dana], [older, newer, other], summaries={newer.id: "ready"})

    res = client.get(f"/assistant/context/patient/{dana.id}/meetings")

    assert res.status_code == 200
    body = res.json()
    # Only this patient's meetings, newest first.
    assert [m["meeting_id"] for m in body] == [str(newer.id), str(older.id)]
    # The id needed to fetch a summary is present, and has_summary reflects the row.
    assert body[0]["has_summary"] is True
    assert body[1]["has_summary"] is False
    # Time is numeric local text, not raw ISO.
    assert _NUMERIC_TIME.match(body[0]["starts_at"])


def test_patient_meetings_unknown_patient_is_empty_not_error() -> None:
    client = _client([], [])
    res = client.get(f"/assistant/context/patient/{uuid.uuid4()}/meetings")
    assert res.status_code == 200
    assert res.json() == []


def test_empty_database_yields_empty_views() -> None:
    client = _client([], [])

    assert client.get("/assistant/context/patients").json() == []
    assert client.get("/assistant/context/agenda").json() == []

    cadence = client.get(f"/assistant/context/patient/{uuid.uuid4()}/cadence").json()
    assert cadence["total_meetings"] == 0
    assert cadence["last_meeting_at"] is None
