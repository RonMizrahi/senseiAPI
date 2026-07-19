"""PHI-safe, read-only context the chat assistant is allowed to fetch.

This is the *only* data surface the assistant's ``http_get`` tool may reach when it
runs in the default PHI-safe mode. It exposes non-clinical facts — who is next,
meeting cadence, the patient roster (name only), and a patient's meetings with the
``meeting_id`` needed to fetch a summary — while pre-formatting every timestamp as a
numeric local string so the model never sees raw ISO. Keeping the assistant behind
this narrow allow-list is the architectural guardrail a system prompt alone cannot
provide.
"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from calendar_events.repository import CalendarEventRepository
from core.database import SessionDep
from patients.repository import PatientRepository
from summaries.repository import SummaryRepository

router = APIRouter(prefix="/assistant/context", tags=["assistant-context"])

_CADENCE_WINDOW = timedelta(days=365)
_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def _readable(dt: datetime) -> str:
    """Render a UTC timestamp as a plain numeric local string (e.g. "21/07/2026
    10:59"), so the assistant never has to reformat or convert time zones."""
    return dt.astimezone(_ISRAEL_TZ).strftime("%d/%m/%Y %H:%M")


class PatientBrief(BaseModel):
    """A patient the therapist can ask about — name only, no contact/clinical data."""

    id: str
    name: str


class AgendaItem(BaseModel):
    """One upcoming meeting: enough to answer "who's next", nothing clinical.

    ``title`` is intentionally NOT exposed — it is free text the therapist authors
    and could contain clinical content ("מעקב חרדה — …"); patient + time answer the
    question without leaking it.
    """

    patient_name: str | None
    starts_at: str  # human-readable local time, not raw ISO


class Cadence(BaseModel):
    """Scheduling cadence for one patient — readable times and counts only."""

    patient_name: str | None
    last_meeting_at: str | None
    next_meeting_at: str | None
    total_meetings: int


class PatientMeeting(BaseModel):
    """One meeting of a patient, with the ``meeting_id`` needed to fetch its summary.

    This is the surface that lets the assistant chain patient → meeting → summary:
    ``/assistant/context/patients`` resolves a name to a patient id, this endpoint
    lists that patient's meetings (id + readable time + whether a summary exists),
    and ``/meetings/{meeting_id}/summary`` returns the summary text.
    """

    meeting_id: str
    starts_at: str  # human-readable local time, not raw ISO
    has_summary: bool


class AssistantContextService:
    """Builds the safe context views from the existing repositories."""

    def __init__(
        self,
        *,
        patients: PatientRepository,
        events: CalendarEventRepository,
        summaries: SummaryRepository,
    ) -> None:
        self._patients = patients
        self._events = events
        self._summaries = summaries

    async def _patient_names(self) -> dict[uuid.UUID, str]:
        return {patient.id: patient.name for patient in await self._patients.list_all()}

    async def list_patients(self) -> list[PatientBrief]:
        patients = await self._patients.list_all()
        return [PatientBrief(id=str(p.id), name=p.name) for p in patients]

    async def agenda(self, *, days: int) -> list[AgendaItem]:
        now = datetime.now(UTC)
        events = await self._events.list_all(from_at=now, to_at=now + timedelta(days=days))
        names = await self._patient_names()
        return [
            AgendaItem(
                patient_name=names.get(event.patient_id) if event.patient_id else None,
                starts_at=_readable(event.start_at),
            )
            for event in events
        ]

    async def cadence(self, *, patient_id: uuid.UUID) -> Cadence:
        now = datetime.now(UTC)
        events = await self._events.list_all(
            from_at=now - _CADENCE_WINDOW, to_at=now + _CADENCE_WINDOW
        )
        mine = [e for e in events if e.patient_id == patient_id]
        past = [e.start_at for e in mine if e.start_at < now]
        future = [e.start_at for e in mine if e.start_at >= now]
        names = await self._patient_names()
        return Cadence(
            patient_name=names.get(patient_id),
            last_meeting_at=_readable(max(past)) if past else None,
            next_meeting_at=_readable(min(future)) if future else None,
            total_meetings=len(mine),
        )

    async def patient_meetings(self, *, patient_id: uuid.UUID) -> list[PatientMeeting]:
        now = datetime.now(UTC)
        events = await self._events.list_all(
            from_at=now - _CADENCE_WINDOW, to_at=now + _CADENCE_WINDOW
        )
        mine = sorted(
            (e for e in events if e.patient_id == patient_id),
            key=lambda e: e.start_at,
            reverse=True,  # newest first
        )
        result = []
        for event in mine:
            summary = await self._summaries.get_by_meeting_id(event.id)
            result.append(
                PatientMeeting(
                    meeting_id=str(event.id),
                    starts_at=_readable(event.start_at),
                    has_summary=summary is not None and summary.status == "ready",
                )
            )
        return result


def get_context_service(session: SessionDep) -> AssistantContextService:
    return AssistantContextService(
        patients=PatientRepository(session),
        events=CalendarEventRepository(session),
        summaries=SummaryRepository(session),
    )


ContextServiceDep = Depends(get_context_service)


@router.get("/patients", response_model=list[PatientBrief])
async def list_patients(
    service: AssistantContextService = ContextServiceDep,
) -> list[PatientBrief]:
    """The patient roster (name only) so the assistant can resolve a name to an id."""
    return await service.list_patients()


@router.get("/agenda", response_model=list[AgendaItem])
async def agenda(
    days: int = Query(default=7, ge=1, le=60),
    service: AssistantContextService = ContextServiceDep,
) -> list[AgendaItem]:
    """Upcoming meetings in the next ``days`` days — "who is next"."""
    return await service.agenda(days=days)


@router.get("/patient/{patient_id}/cadence", response_model=Cadence)
async def patient_cadence(
    patient_id: uuid.UUID,
    service: AssistantContextService = ContextServiceDep,
) -> Cadence:
    """Meeting cadence for one patient — last/next meeting and total count."""
    try:
        return await service.cadence(patient_id=patient_id)
    except SQLAlchemyError:
        # A malformed id or DB hiccup should not leak internals to the assistant.
        return Cadence(
            patient_name=None, last_meeting_at=None, next_meeting_at=None, total_meetings=0
        )


@router.get("/patient/{patient_id}/meetings", response_model=list[PatientMeeting])
async def patient_meetings(
    patient_id: uuid.UUID,
    service: AssistantContextService = ContextServiceDep,
) -> list[PatientMeeting]:
    """A patient's meetings — each with its ``meeting_id`` (for the summary) and a
    readable time. This is how the assistant reaches a patient's session content."""
    try:
        return await service.patient_meetings(patient_id=patient_id)
    except SQLAlchemyError:
        # A malformed id or DB hiccup should not leak internals to the assistant.
        return []


__all__ = ["router"]
