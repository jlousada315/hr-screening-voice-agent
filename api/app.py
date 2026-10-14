from __future__ import annotations

import _bootstrap  # noqa: I001  # isort: skip (load .env before hireme_agent below)

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager

import anyio
import hireme_agent
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from hireme_agent import MODEL
from hireme_agent.observability import shutdown
from starlette.datastructures import UploadFile
from starlette.types import Receive, Scope, Send

import conversation_turn

logger = logging.getLogger("hireme")

# ── FastAPI ───────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _check_model_source()
    _check_provider_key()
    _check_calendar_config()
    await hireme_agent.initialize()
    try:
        yield
    finally:
        await hireme_agent.shutdown()
        await asyncio.to_thread(shutdown)


app = FastAPI(title="hireme-api", version="0.1.0", lifespan=_lifespan)

_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


def _check_model_source() -> None:
    """Log the served model and where it came from; warn when the shell overrides `.env`."""
    logger.info("LLM model %s (from %s).", MODEL, _bootstrap.model_source())
    shadowed = _bootstrap.model_shadowed_by_process_env()
    if shadowed:
        logger.warning(
            "LLM_MODEL in the process environment (%s) overrides the .env value (%s). "
            "Unset the exported variable if that is not intended.",
            MODEL,
            shadowed,
        )


def _check_provider_key() -> None:
    """Warn at boot when GROQ_API_KEY is missing; keep the server available."""
    if not os.environ.get("GROQ_API_KEY"):
        logger.warning(
            "Groq key is not set: MODEL=%r expects GROQ_API_KEY. "
            "Set it (with available credits) or /turn will fail at Groq.",
            MODEL,
        )
    else:
        logger.info("Groq key present (GROQ_API_KEY).")


def _check_calendar_config() -> None:
    """Log whether the owner file shares a booking link."""
    if hireme_agent.calendar_enabled():
        logger.info("Booking link enabled (Booking: in the owner file).")
    else:
        logger.info("Booking link disabled: add a 'Booking:' line to config/owner.md to share one.")


@app.get("/health")
async def health() -> dict:
    """Liveness + the Groq model, whether its auth key is present, and booking-link state."""
    return {
        "status": "ok",
        "model": MODEL,
        "provider": "groq",
        "provider_key": "GROQ_API_KEY",
        "provider_key_set": bool(os.environ.get("GROQ_API_KEY")),
        "calendar_enabled": hireme_agent.calendar_enabled(),
    }


# Disable proxy buffering so each production event reaches the client immediately.
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _sse(events: AsyncIterator[conversation_turn.TurnEvent]) -> AsyncIterator[str]:
    async with aclosing(events):
        async for event in events:
            yield f"data: {json.dumps(event)}\n\n"


class TurnStreamingResponse(StreamingResponse):
    """Own the turn lifetime, including sends while the event generator is suspended."""

    def __init__(self, events: conversation_turn.TurnStream):
        super().__init__(_sse(events), media_type="text/event-stream", headers=_SSE_HEADERS)
        self._turn = events
        self._transport_error: BaseException | None = None

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        except BaseException as exc:
            # ASGI 2.3 swallows disconnect cancellation; 2.4 translates OSError.
            if self._transport_error is None:
                self._transport_error = exc
            raise

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except BaseException as exc:
            if self._transport_error is None:
                self._transport_error = exc
            raise
        finally:
            # A blocked send leaves the iterator suspended at yield. Close it
            # explicitly, even when the caller's AnyIO cancel scope is cancelled.
            try:
                with anyio.CancelScope(shield=True):
                    await self.body_iterator.aclose()
                    await self._turn.aclose()
            except BaseException as exc:
                if self._transport_error is None:
                    self._transport_error = exc
                raise
            finally:
                self._turn.finish(self._transport_error)


def _sse_response(events: conversation_turn.TurnStream) -> StreamingResponse:
    return TurnStreamingResponse(events)


def _require_auth(request: Request) -> None:
    secret = os.environ.get("API_SHARED_SECRET")
    if secret and request.headers.get("authorization") != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _identifier(payload, field: str) -> str | None:
    value = payload.get(field)
    if value is not None and not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"`{field}` must be a string")
    return value or None


def _turn_index(payload) -> int | None:
    """Analytics only, so a malformed value is dropped instead of failing the turn."""
    value = payload.get("turn_index")
    if isinstance(value, bool):
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if 0 <= index <= 10_000 else None


async def _parse_turn(request: Request) -> conversation_turn.TextTurn | conversation_turn.VoiceTurn:
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        try:
            async with request.form() as form:
                upload = form.get("audio")
                if not isinstance(upload, UploadFile):
                    raise HTTPException(status_code=400, detail="missing `audio` file in form")
                return conversation_turn.VoiceTurn(
                    audio=await upload.read(),
                    content_type=upload.content_type or "audio/webm",
                    conversation_id=_identifier(form, "conversation_id"),
                    distinct_id=_identifier(form, "distinct_id"),
                    session_id=_identifier(form, "session_id"),
                    call_id=_identifier(form, "call_id"),
                    turn_index=_turn_index(form),
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid multipart form") from exc
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail="body must be JSON with a `text` field"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object with `text`")
    return conversation_turn.TextTurn(
        text=payload.get("text"),
        conversation_id=_identifier(payload, "conversation_id"),
        distinct_id=_identifier(payload, "distinct_id"),
        session_id=_identifier(payload, "session_id"),
        call_id=_identifier(payload, "call_id"),
        turn_index=_turn_index(payload),
    )


@app.post("/turn")
async def turn(request: Request) -> StreamingResponse:
    """Accept JSON text or multipart audio and encode the Conversation Turn events."""
    _require_auth(request)
    turn_input = await _parse_turn(request)
    try:
        events = await conversation_turn.start(turn_input)
    except conversation_turn.TurnValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except conversation_turn.TranscriptionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _sse_response(events)


@app.post("/greeting")
async def greeting(request: Request) -> StreamingResponse:
    """Open a call: stream the rep's spoken greeting as the first turn of a new conversation."""
    _require_auth(request)
    payload: dict = {}
    if await request.body():
        try:
            parsed = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="body must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        payload = parsed
    events = await conversation_turn.start_greeting(
        conversation_turn.GreetingTurn(
            distinct_id=_identifier(payload, "distinct_id"),
            session_id=_identifier(payload, "session_id"),
            call_id=_identifier(payload, "call_id"),
        )
    )
    return _sse_response(events)
