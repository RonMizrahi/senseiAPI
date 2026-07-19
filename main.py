import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from assistant import router as assistant_router
from assistant.context import router as assistant_context_router
from audio import router as audio_router
from auth.router import get_current_user
from auth.router import router as auth_router
from calendar_events import router as calendar_router
from core.config import Settings, get_settings, validate_startup_settings
from core.database import close_database, init_database, ping_database
from patients import router as patients_router
from summaries import router as summaries_router
from summaries.service import sweep_interrupted_summaries

logger = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


class RootResponse(BaseModel):
    message: str


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    database: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings_factory = app.dependency_overrides.get(get_settings, get_settings)
    settings = settings_factory()
    validate_startup_settings(settings)
    await init_database(settings)
    await sweep_interrupted_summaries(settings)
    yield
    await close_database(settings.database_url)


app = FastAPI(title="SenseiAPI", version="0.1.0", lifespan=lifespan)

_cors_origins = [
    origin.strip() for origin in get_settings().cors_origins.split(",") if origin.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Let the browser read the assistant's stream marker cross-origin; the AI-SDK
        # useChat transport reads it to recognise the UI message stream.
        expose_headers=["x-vercel-ai-ui-message-stream"],
    )

app.include_router(auth_router)
app.include_router(assistant_router, dependencies=[Depends(get_current_user)])
app.include_router(assistant_context_router, dependencies=[Depends(get_current_user)])
app.include_router(audio_router, dependencies=[Depends(get_current_user)])
app.include_router(calendar_router, dependencies=[Depends(get_current_user)])
app.include_router(patients_router, dependencies=[Depends(get_current_user)])
app.include_router(summaries_router, dependencies=[Depends(get_current_user)])


@app.get("/", response_model=RootResponse)
async def root() -> RootResponse:
    return RootResponse(message="Welcome to SenseiAPI")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/ready", response_model=ReadinessResponse)
async def readiness(settings: SettingsDep, response: Response) -> ReadinessResponse:
    try:
        await ping_database(settings)
    except (OSError, SQLAlchemyError) as exc:
        logger.warning("Database readiness check failed: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="not_ready", database="unavailable")

    return ReadinessResponse(status="ready", database="ok")
