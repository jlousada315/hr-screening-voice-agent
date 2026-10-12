from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal, TypedDict
from weakref import WeakKeyDictionary

import anyio
import hireme_agent
import httpx
from hireme_agent.observability import TurnObservation

logger = logging.getLogger("hireme")


_EL_BASE = "https://api.elevenlabs.io/v1"
_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v1")
_TTS_MODEL = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_turbo_v2_5")
_TIMEOUT = httpx.Timeout(60.0)


def _tts_speed() -> float:
    """Read the configured speech speed, falling back safely and clamping provider bounds."""
    try:
        speed = float(os.environ.get("ELEVENLABS_TTS_SPEED", "1.12"))
    except ValueError:
        speed = 1.12
    return min(1.2, max(0.7, speed))


_MARKDOWN_LINK = re.compile(r"!?\[([^]]*)\]\([^)]+\)")
_BARE_URL = re.compile(r"https?://(?:www\.)?([^/\s?#]+)(?:[^\s]*)?", re.IGNORECASE)


def _spoken_text(text: str) -> str:
    """Turn display-oriented Markdown and URLs into clean text for speech only."""
    text = _MARKDOWN_LINK.sub(lambda match: match.group(1), text)
    text = _BARE_URL.sub(lambda match: match.group(1).replace(".", " dot "), text)
    text = re.sub(r"```(?:\w+)?\s*|```", "", text)
    text = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)", "", text)
    text = re.sub(r"[*_~`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ── ElevenLabs STT / TTS ──────────────────────────────────────────────────────


def _api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    return key


async def _transcribe(audio: bytes, content_type: str = "audio/webm") -> str:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            f"{_EL_BASE}/speech-to-text",
            headers={"xi-api-key": _api_key()},
            data={"model_id": _STT_MODEL},
            files={"file": ("audio", audio, content_type)},
        )
        r.raise_for_status()
        return r.json().get("text", "").strip()


_TTS_RETRY_DELAY = 0.4  # seconds before the single retry of a transient TTS failure


async def _synthesize(text: str) -> bytes:
    """Speak one sentence, retrying once on a rate limit, server error or timeout."""
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID")
    if not voice_id:
        raise RuntimeError("ELEVENLABS_VOICE_ID is not set")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for attempt in (1, 2):
            try:
                r = await client.post(
                    f"{_EL_BASE}/text-to-speech/{voice_id}",
                    headers={"xi-api-key": _api_key(), "accept": "audio/mpeg"},
                    json={
                        "text": text,
                        "model_id": _TTS_MODEL,
                        "voice_settings": {"speed": _tts_speed()},
                    },
                )
                r.raise_for_status()
                return r.content
            except (httpx.HTTPStatusError, httpx.TimeoutException) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                transient = status is None or status == 429 or status >= 500
                if attempt == 2 or not transient:
                    raise
                await asyncio.sleep(_TTS_RETRY_DELAY)
    raise AssertionError("unreachable")


_BUSY_REPLY = "I'm a little busy right now. Please ask me again in a few seconds."


def _is_transient_provider_error(exc: BaseException) -> bool:
    """Rate limit, overload or timeout: the user can simply try again shortly."""
    status = getattr(exc, "status_code", None)
    if status in (429, 500, 502, 503, 504):
        return True
    return isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__


# ── Sentence streaming ─────────────────────────────────────────────────────────────


# Sentence terminator: one or more of . ! ? followed by whitespace or end-of-buffer.
# The lookahead avoids splitting inside numbers/abbreviations mid-stream ("3.5" — the
# period is followed by a digit, so it won't match until a real boundary lands). Unlike
# the cookbook's `re.split(r"[.!?]+")`, we keep the terminal punctuation with the
# sentence so TTS preserves question/exclamation intonation.
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")


def _drain_sentences(buffer: str) -> tuple[list[str], str]:
    """Pull completed sentences off the front of `buffer`, keeping terminal punctuation.

    Returns `(complete_sentences, remainder)` where `remainder` is the trailing,
    not-yet-terminated text to keep buffering until more tokens arrive.
    """
    sentences: list[str] = []
    last = 0
    for m in _SENTENCE_END.finditer(buffer):
        chunk = buffer[last : m.end()].strip()
        if chunk:
            sentences.append(chunk)
        last = m.end()
    return sentences, buffer[last:]


@dataclass(frozen=True, slots=True)
class TextTurn:
    text: str
    conversation_id: str | None = None
    distinct_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class VoiceTurn:
    audio: bytes
    content_type: str = "audio/webm"
    conversation_id: str | None = None
    distinct_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class GreetingTurn:
    """The rep speaks first when a call starts; always a new conversation, no model call."""

    distinct_id: str | None = None
    session_id: str | None = None


class TokenEvent(TypedDict):
    type: Literal["token"]
    text: str


class TranscriptEvent(TypedDict):
    type: Literal["transcript"]
    text: str


class SentenceEvent(TypedDict):
    type: Literal["sentence"]
    text: str
    audio: str


class DoneEvent(TypedDict):
    type: Literal["done"]
    conversation_id: str
    trace_id: str


class ErrorEvent(TypedDict):
    type: Literal["error"]
    text: str


TurnEvent = TokenEvent | TranscriptEvent | SentenceEvent | DoneEvent | ErrorEvent


class TurnValidationError(ValueError):
    """Invalid content; the HTTP adapter can return 400 before streaming."""


class TranscriptionError(RuntimeError):
    """Speech transcription failed before streaming; map to HTTP 502."""


class TurnStream(AsyncIterator[TurnEvent]):
    """Own event cleanup; the transport calls finish after its final send."""

    def __init__(self, observation: TurnObservation):
        self._observation = observation
        self._events: AsyncGenerator[TurnEvent, None]

    def __aiter__(self) -> TurnStream:
        return self

    async def __anext__(self) -> TurnEvent:
        return await anext(self._events)

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            await self._events.aclose()

    def finish(self, error: BaseException | None = None) -> None:
        # A provider failure takes precedence over subsequent transport failures.
        if self._observation.error is None:
            self._observation.error = error
        self._observation.finish()


async def start(turn: TextTurn | VoiceTurn) -> TurnStream:
    """Validate/transcribe before streaming; finalize pre-stream failures internally."""
    conversation_id = hireme_agent.prepare_conversation(turn.conversation_id)
    observation = TurnObservation(
        conversation_id,
        turn.distinct_id,
        turn.session_id,
        "voice" if isinstance(turn, VoiceTurn) else "text",
        hireme_agent.PROMPT_VERSION,
        # One trace per call: every turn of a conversation is a span under the same trace.
        trace_id=conversation_id,
    )
    try:
        if isinstance(turn, VoiceTurn):
            if not turn.audio:
                raise TurnValidationError("empty audio upload")
            try:
                with observation.span(
                    "speech transcription",
                    **{"$ai_provider": "elevenlabs", "$ai_model": _STT_MODEL},
                ):
                    text = await _transcribe(turn.audio, turn.content_type)
                    if not text or not text.strip():
                        raise TurnValidationError("could not hear anything — try again")
            except TurnValidationError:
                raise
            except httpx.HTTPStatusError as exc:
                raise TranscriptionError(
                    f"speech-to-text failed ({exc.response.status_code})"
                ) from exc
            except Exception as exc:
                raise TranscriptionError("speech-to-text failed") from exc
        else:
            if not isinstance(turn.text, str) or not turn.text.strip():
                raise TurnValidationError("provide `text` (JSON) or an `audio` file")
            text = turn.text.strip()
    except BaseException as exc:
        observation.finish(exc.__cause__ or exc)
        raise
    stream = TurnStream(observation)
    stream._events = _events(turn, text.strip(), conversation_id, observation)
    return stream


# The greeting never changes between calls, so it is synthesized once per process.
_greeting_audio: dict[tuple[str, str | None, str, float], bytes] = {}
_greeting_locks: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, str | None, str, float], asyncio.Lock]
] = WeakKeyDictionary()


async def start_greeting(turn: GreetingTurn) -> TurnStream:
    """Open a conversation with the rep's spoken greeting (no transcription, no model)."""
    conversation_id = hireme_agent.prepare_conversation(None)
    observation = TurnObservation(
        conversation_id,
        turn.distinct_id,
        turn.session_id,
        "voice",
        hireme_agent.PROMPT_VERSION,
        trace_id=conversation_id,
    )
    observation.input_state = "(call started)"
    stream = TurnStream(observation)
    stream._events = _greeting_events(conversation_id, observation)
    return stream


async def _greeting_events(
    conversation_id: str, observation: TurnObservation
) -> AsyncGenerator[TurnEvent, None]:
    text = hireme_agent.commit_greeting(conversation_id)
    observation.output_state = text
    key = (text, os.environ.get("ELEVENLABS_VOICE_ID"), _TTS_MODEL, _tts_speed())
    audio = _greeting_audio.get(key)
    try:
        with observation.span(
            "speech synthesis",
            **{
                "$ai_provider": "elevenlabs",
                "$ai_model": _TTS_MODEL,
                "cache_hit": audio is not None,
            },
        ):
            if audio is None:
                # Locks belong to the serving loop, including separate test lifespans.
                locks = _greeting_locks.setdefault(asyncio.get_running_loop(), {})
                lock = locks.setdefault(key, asyncio.Lock())
                async with lock:
                    audio = _greeting_audio.get(key)
                    if audio is None:
                        audio = await _synthesize(_spoken_text(text))
                        _greeting_audio[key] = audio
    except Exception as exc:
        # The call still opens: the caption shows the greeting and the rep then listens.
        logger.warning("greeting speech failed", exc_info=True)
        observation.error = exc
        audio = b""
    yield {"type": "sentence", "text": text, "audio": base64.b64encode(audio).decode("ascii")}
    yield {"type": "done", "conversation_id": conversation_id, "trace_id": observation.trace_id}


async def _sentence(text: str, observation: TurnObservation) -> SentenceEvent:
    with observation.span(
        "speech synthesis", **{"$ai_provider": "elevenlabs", "$ai_model": _TTS_MODEL}
    ):
        audio = await _synthesize(_spoken_text(text))
    return {"type": "sentence", "text": text, "audio": base64.b64encode(audio).decode("ascii")}


async def _events(
    turn: TextTurn | VoiceTurn, text: str, conversation_id: str, observation: TurnObservation
) -> AsyncGenerator[TurnEvent, None]:
    voice = isinstance(turn, VoiceTurn)
    if voice:
        yield {"type": "transcript", "text": text}
    buffer = ""
    try:
        async with aclosing(hireme_agent.start_text(text, conversation_id, observation)) as tokens:
            async for delta in tokens:
                if voice:
                    buffer += delta
                    sentences, buffer = _drain_sentences(buffer)
                    for sentence in sentences:
                        # Await/yield each clip before requesting any later model tokens.
                        yield await _sentence(sentence, observation)
                else:
                    yield {"type": "token", "text": delta}
        if voice and buffer.strip():
            yield await _sentence(buffer.strip(), observation)
    except Exception as exc:
        if observation.error is None:
            observation.error = exc
        logger.exception(
            "%s turn failed (conversation %s)", "voice" if voice else "text", conversation_id
        )
        if voice and _is_transient_provider_error(exc):
            # Answer out loud instead of leaving a silent line; the turn is not committed
            # so history is not polluted by the fallback.
            try:
                observation.output_state = _BUSY_REPLY
                yield await _sentence(_BUSY_REPLY, observation)
                yield {
                    "type": "done",
                    "conversation_id": conversation_id,
                    "trace_id": observation.trace_id,
                }
                return
            except Exception:
                logger.warning("busy fallback speech failed", exc_info=True)
        yield {"type": "error", "text": "the reply failed — please try again"}
        return
    tokens.commit()
    yield {"type": "done", "conversation_id": conversation_id, "trace_id": observation.trace_id}
