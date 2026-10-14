"""Best-effort turn/span telemetry; generation capture is per model request."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter

from posthog import Posthog

logger = logging.getLogger("hireme")
_client: Posthog | None = None


def capture(event: str, distinct_id: str, properties: dict, timestamp: datetime) -> None:
    """Enqueue only: network delivery happens on the SDK worker thread."""
    global _client
    key = os.environ.get("POSTHOG_API_KEY")
    if not key:
        return
    try:
        if _client is None:
            _client = Posthog(
                key,
                host=os.environ.get("POSTHOG_API_URL", "https://eu.i.posthog.com"),
                disable_geoip=True,
                enable_exception_autocapture=False,
            )
        _client.capture(event, distinct_id=distinct_id, properties=properties, timestamp=timestamp)
    except Exception:
        logger.warning("AI telemetry enqueue failed", exc_info=True)


def shutdown() -> None:
    global _client
    if _client is not None:
        try:
            _client.shutdown()
        except Exception:
            logger.warning("AI telemetry shutdown failed", exc_info=True)
        finally:
            _client = None


def _retry_after(error: BaseException) -> float | None:
    """Seconds Groq asked us to wait (429 `retry-after`), if the error carries it."""
    body = getattr(error, "body", None)
    response = getattr(error, "response", None) or getattr(body, "response", None)
    headers = getattr(response, "headers", None)
    try:
        return float(headers["retry-after"]) if headers and "retry-after" in headers else None
    except (TypeError, ValueError):
        return None


_STATE_LIMIT = 2000


def _states(input_state: str | None, output_state: str | None) -> dict:
    """Trace-level conversation state, bounded so one long reply cannot bloat an event."""
    states = {}
    if input_state:
        states["$ai_input_state"] = input_state[:_STATE_LIMIT]
    if output_state:
        states["$ai_output_state"] = output_state[:_STATE_LIMIT]
    return states


TURN_SPAN_NAME = "HireMe screening turn"
CALL_TRACE_NAME = "HireMe screening call"


@dataclass
class TurnObservation:
    """One turn of a call. Every turn of a call shares the trace, so PostHog shows one
    trace per call; each turn is its own span under it, and its work hangs off that span."""

    conversation_id: str
    distinct_id: str | None
    session_id: str | None
    mode: str
    prompt_version: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    turn_span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started: float = field(default_factory=perf_counter)
    finished: bool = False
    error: BaseException | None = None
    # What the caller said and what was answered, so a trace reads end to end.
    input_state: str | None = None
    output_state: str | None = None
    unverified_links: int = 0  # invented URLs the link guard removed from the reply
    # The browser's call id and turn number (greeting = 0), so traces join the call events.
    call_id: str | None = None
    turn_index: int | None = None
    first_audio_seconds: float | None = None  # turn start → first reply sentence synthesized

    def mark_first_audio(self) -> None:
        if self.first_audio_seconds is None:
            self.first_audio_seconds = perf_counter() - self.started

    @property
    def metadata(self) -> dict:
        meta = {
            "user_id": self.distinct_id or self.conversation_id,
            "$ai_session_id": self.conversation_id,
            "$ai_trace_name": CALL_TRACE_NAME,
            "$ai_prompt_name": "hireme-screening-rep",
            "$ai_prompt_version": self.prompt_version,
            "$geoip_disable": True,
            "conversation_id": self.conversation_id,
            "interaction_mode": self.mode,
            "deployment_environment": os.environ.get("DEPLOYMENT_ENVIRONMENT", "development"),
            "app_name": "hireme-agent",
            "parent_run_id": self.trace_id,
        }
        if self.session_id:
            meta["$session_id"] = self.session_id
        if self.call_id:
            meta["call_id"] = self.call_id
        return meta

    def _emit(
        self,
        event: str,
        span_id: str,
        name: str,
        start: datetime,
        duration: float,
        error: BaseException | None,
        **extra,
    ) -> None:
        # A caller interrupting or hanging up cancels the turn; that is not a failure.
        cancelled = isinstance(error, asyncio.CancelledError)
        if cancelled:
            error = None
        meta = self.metadata
        distinct_id = meta.pop("user_id")
        meta.pop("parent_run_id")
        properties = {
            **meta,
            "$ai_trace_id": self.trace_id,
            "$ai_span_id": span_id,
            "$ai_span_name": name,
            "$ai_latency": duration,
            "$ai_is_error": error is not None,
            "$ai_start_time": start.isoformat(),
            "$ai_end_time": datetime.now(UTC).isoformat(),
            **extra,
        }
        if cancelled:
            properties["$ai_cancelled"] = True
        if error is not None:
            # Provider bodies can contain secrets; retain a safe, useful classification.
            properties["$ai_error"] = type(error).__name__
            # Groq errors carry status_code; httpx (ElevenLabs) errors carry it on response.
            status = getattr(error, "status_code", None)
            if status is None:
                status = getattr(getattr(error, "response", None), "status_code", None)
            if isinstance(status, int):
                properties["$ai_http_status"] = status
            retry_after = _retry_after(error)
            if retry_after is not None:
                properties["$ai_retry_after_seconds"] = retry_after
        try:
            capture(event, distinct_id, properties, start)
        except Exception:
            # Also isolate a replaced/broken capture adapter from request handling.
            logger.warning("AI telemetry capture failed", exc_info=True)

    def finish(self, error: BaseException | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        failure = error if error is not None else self.error
        duration = perf_counter() - self.started
        states = _states(self.input_state, self.output_state)
        turn = {}
        if self.turn_index is not None:
            turn["turn_index"] = self.turn_index
        if self.first_audio_seconds is not None:
            turn["first_audio_seconds"] = self.first_audio_seconds
        if self.unverified_links:
            turn["unverified_link_count"] = self.unverified_links
        self._emit(
            "$ai_span",
            self.turn_span_id,
            TURN_SPAN_NAME,
            self.started_at,
            duration,
            failure,
            **{"$ai_parent_id": self.trace_id, **states, **turn},
        )
        # Carries the latest turn's states, so the call's row reads as its last exchange.
        self._emit(
            "$ai_trace",
            self.trace_id,
            CALL_TRACE_NAME,
            self.started_at,
            duration,
            failure,
            **states,
        )

    @contextmanager
    def span(self, name: str, **properties) -> Iterator[None]:
        start, clock = datetime.now(UTC), perf_counter()
        error = None
        try:
            yield
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._emit(
                "$ai_span",
                uuid.uuid4().hex,
                name,
                start,
                perf_counter() - clock,
                error,
                **{"$ai_parent_id": self.turn_span_id, **properties},
            )

    @contextmanager
    def generation(self, properties: dict) -> Iterator[None]:
        """Capture exactly one event even for request setup/stream failures."""
        start, clock = datetime.now(UTC), perf_counter()
        error = None
        try:
            yield
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._emit(
                "$ai_generation",
                uuid.uuid4().hex,
                "Groq generation",
                start,
                perf_counter() - clock,
                error,
                **{"$ai_parent_id": self.turn_span_id, **properties},
            )
