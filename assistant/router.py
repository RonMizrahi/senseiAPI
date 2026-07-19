from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from assistant.dependencies import get_assistant_service
from assistant.schemas import ChatRequest, latest_question_length
from assistant.service import AssistantService
from assistant.sse import UI_MESSAGE_STREAM_HEADER, UI_MESSAGE_STREAM_VERSION
from core.config import Settings, get_settings

router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.post("/chat")
async def chat(
    request: ChatRequest,
    service: AssistantService = Depends(get_assistant_service),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Stream an assistant reply as a Vercel AI SDK UI Message Stream.

    The frontend consumes this with ``@ai-sdk/react``'s ``useChat``; the response is
    Server-Sent Events, so the reply renders token-by-token. The stream is stateless —
    the client sends the full conversation each turn.
    """
    question_length = latest_question_length(request)
    if question_length == 0:
        # No user text — reject before invoking the (paid) model.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="a user question is required",
        )
    if question_length > settings.assistant_max_question_chars:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the question is too long",
        )
    return StreamingResponse(
        service.stream_sse(request),
        media_type="text/event-stream",
        headers={
            UI_MESSAGE_STREAM_HEADER: UI_MESSAGE_STREAM_VERSION,
            "cache-control": "no-cache",
            # Disable proxy buffering so deltas reach the browser as they are produced.
            "x-accel-buffering": "no",
        },
    )
