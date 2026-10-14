"""Turn grouping, provider failures, isolation and SDK/callback contract checks."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, create_autospec

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from hireme_agent import observability as obs
from hireme_agent import runtime as agent_runtime
from posthog import Posthog

import app as runtime
import conversation_turn as turns
from tests.conftest import make_acompletion_stream, provider


@pytest.fixture
def events(monkeypatch):
    captured = []
    monkeypatch.setattr(
        obs,
        "capture",
        lambda event, person, props, timestamp: captured.append((event, person, props, timestamp)),
    )
    agent_runtime._conversations.clear()
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "test")
    monkeypatch.delenv("API_SHARED_SECRET", raising=False)
    return captured


def test_two_text_turns_share_one_trace_and_ai_session(monkeypatch, events):
    client = TestClient(runtime.app)
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return await make_acompletion_stream("Hello.")()

    monkeypatch.setattr(provider, "complete", complete)
    payload = {"text": "hi", "distinct_id": "visitor", "session_id": "browser"}
    first = client.post("/turn", json=payload)
    from tests.test_app import parse_sse

    conversation = parse_sse(first.text)[-1]["conversation_id"]
    second = client.post("/turn", json={**payload, "conversation_id": conversation})
    assert second.status_code == 200
    # One trace per call: the second turn joins the first turn's trace as another turn span.
    assert calls[0]["trace_id"] == calls[1]["trace_id"] == conversation
    assert parse_sse(first.text)[-1]["trace_id"] == conversation
    assert parse_sse(second.text)[-1]["trace_id"] == conversation
    generations = [e for e in events if e[0] == "$ai_generation"]
    assert len(generations) == 2
    turn_spans = [
        e[2] for e in events if e[0] == "$ai_span" and e[2]["$ai_span_name"] == obs.TURN_SPAN_NAME
    ]
    assert len(turn_spans) == 2
    assert turn_spans[0]["$ai_span_id"] != turn_spans[1]["$ai_span_id"]
    for turn_span, generation in zip(turn_spans, generations, strict=True):
        assert turn_span["$ai_parent_id"] == conversation
        assert generation[2]["$ai_parent_id"] == turn_span["$ai_span_id"]
        assert turn_span["$ai_input_state"] == "hi"
    traces = [e for e in events if e[0] == "$ai_trace"]
    for call, (event, person, props, _) in zip(calls, traces, strict=True):
        assert event == "$ai_trace"
        assert person == "visitor"
        assert props["$ai_trace_id"] == call["trace_id"]
        assert props["$ai_session_id"] == conversation
        assert props["$session_id"] == "browser"
        assert props["interaction_mode"] == "text"
        assert props["deployment_environment"] == "test"
        assert props["$ai_prompt_version"] == agent_runtime._PROMPT_VERSION
        assert props["$ai_prompt_name"] == "hireme-screening-rep"
        assert props["$geoip_disable"] is True
        assert props["$ai_latency"] >= 0
        assert props["$ai_is_error"] is False
        assert call["metadata"]["parent_run_id"] == props["$ai_span_id"]


@pytest.mark.parametrize("failure", [None, "stt", "empty", "tts", "llm", "network"])
def test_voice_success_and_failures_keep_identity(monkeypatch, events, failure):
    error = httpx.HTTPStatusError(
        "provider error",
        request=httpx.Request("POST", "https://voice"),
        response=httpx.Response(402),
    )
    stt = AsyncMock(return_value="" if failure == "empty" else "question")
    tts = AsyncMock(return_value=b"audio")
    llm = make_acompletion_stream("One.", " Two.")
    if failure == "stt":
        stt.side_effect = error
    if failure == "network":
        stt.side_effect = httpx.ConnectError("offline")
    if failure == "tts":
        tts.side_effect = error
    if failure == "llm":
        llm.side_effect = RuntimeError("model down")
    monkeypatch.setattr(turns, "_transcribe", stt)
    monkeypatch.setattr(turns, "_synthesize", tts)
    monkeypatch.setattr(provider, "complete", llm)
    client = TestClient(runtime.app, raise_server_exceptions=False)
    response = client.post(
        "/turn",
        data={"distinct_id": "visitor", "session_id": "browser"},
        files={"audio": ("clip.webm", b"audio", "audio/webm")},
    )
    assert response.status_code == {"stt": 502, "empty": 400, "network": 502}.get(failure, 200)
    traces = [props for event, _, props, _ in events if event == "$ai_trace"]
    assert len(traces) == 1
    root = traces[0]
    assert root["$ai_is_error"] is (failure is not None)
    assert root["$ai_latency"] >= 0
    all_spans = [props for event, _, props, _ in events if event == "$ai_span"]
    turn_spans = [s for s in all_spans if s["$ai_span_name"] == obs.TURN_SPAN_NAME]
    assert len(turn_spans) == 1
    assert turn_spans[0]["$ai_parent_id"] == root["$ai_trace_id"]
    spans = [s for s in all_spans if s["$ai_span_name"] != obs.TURN_SPAN_NAME]
    assert spans[0]["$ai_span_name"] == "speech transcription"
    if failure is None:
        from tests.test_app import parse_sse

        assert parse_sse(response.text)[-1]["trace_id"] == root["$ai_trace_id"]
        assert len(spans) == 3
    if failure == "empty":
        assert spans[0]["$ai_is_error"] is True
    if failure == "tts":
        assert spans[-1]["$ai_is_error"] is True
    for _, person, props, _ in events:
        assert person == "visitor"
        assert props["$session_id"] == "browser"
        assert props["$ai_trace_id"] == root["$ai_trace_id"]
        assert props["$ai_session_id"] == root["$ai_session_id"]
        assert props["interaction_mode"] == "voice"
        assert props["$ai_latency"] >= 0
    for span in spans:
        assert span["$ai_provider"] == "elevenlabs"
        assert span["$ai_model"] == (
            turns._STT_MODEL
            if span["$ai_span_name"] == "speech transcription"
            else turns._TTS_MODEL
        )
        assert span["$ai_parent_id"] == turn_spans[0]["$ai_span_id"]
    if failure not in ("stt", "empty", "network"):
        assert llm.await_args.kwargs["trace_id"] == root["$ai_trace_id"]
        assert llm.await_args.kwargs["metadata"]["interaction_mode"] == "voice"


def test_text_error_and_broken_analytics_do_not_break_sse(monkeypatch, events):
    from tests.test_app import parse_sse

    monkeypatch.setattr(provider, "complete", AsyncMock(side_effect=RuntimeError("down")))
    client = TestClient(runtime.app)
    response = client.post("/turn", json={"text": "hi"})
    assert parse_sse(response.text)[-1]["type"] == "error"
    assert events[-1][2]["$ai_is_error"] is True
    monkeypatch.setattr(obs, "capture", Mock(side_effect=RuntimeError("analytics down")))
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("OK."))
    response = client.post("/turn", json={"text": "hi"})
    assert parse_sse(response.text)[-1]["type"] == "done"


@pytest.mark.parametrize("voice", [False, True])
def test_done_trace_id_is_the_call_id_with_backend_capture_disabled(monkeypatch, voice):
    from tests.test_app import parse_sse

    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    factory = Mock()
    monkeypatch.setattr(obs, "Posthog", factory)
    monkeypatch.setattr(obs, "_client", None)
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("Answer."))
    monkeypatch.setattr(turns, "_transcribe", AsyncMock(return_value="question"))
    monkeypatch.setattr(turns, "_synthesize", AsyncMock(return_value=b"audio"))
    client = TestClient(runtime.app)

    def send(conversation=None):
        if voice:
            data = {"conversation_id": conversation} if conversation else {}
            response = client.post("/turn", data=data, files={"audio": ("call.webm", b"audio")})
        else:
            response = client.post(
                "/turn", json={"text": "question", "conversation_id": conversation}
            )
        assert response.status_code == 200
        return parse_sse(response.text)[-1]

    first = send()
    second = send(first["conversation_id"])
    assert first["trace_id"] and second["trace_id"]
    assert first["trace_id"] == second["trace_id"] == first["conversation_id"]
    assert first["conversation_id"] == second["conversation_id"]
    factory.assert_not_called()


def test_capture_uses_installed_sdk_contract_and_disable_switch(monkeypatch):
    client = create_autospec(Posthog, instance=True)
    factory = Mock(return_value=client)
    monkeypatch.setattr(obs, "Posthog", factory)
    monkeypatch.setattr(obs, "_client", None)
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    obs.capture("$ai_trace", "visitor", {}, datetime.now(UTC))
    factory.assert_not_called()
    monkeypatch.setenv("POSTHOG_API_KEY", "project-key")
    obs.capture("$ai_trace", "visitor", {}, datetime.now(UTC))
    assert client.capture.call_args.args == ("$ai_trace",)
    assert client.capture.call_args.kwargs["distinct_id"] == "visitor"
    assert factory.call_args.kwargs["disable_geoip"] is True
    client.capture.side_effect = RuntimeError("queue failed")
    obs.capture("$ai_trace", "visitor", {}, datetime.now(UTC))
    monkeypatch.setenv("POSTHOG_API_KEY", "")
    previous = client.capture.call_count
    obs.capture("$ai_trace", "visitor", {}, datetime.now(UTC))
    assert client.capture.call_count == previous


async def invoke_turn(monkeypatch, mode, *, spec="2.3", send, receive_disconnect=None):
    """Exercise the real endpoint/response without a buffering HTTP test client."""
    request = (
        httpx.Request(
            "POST",
            "http://test/turn",
            json={
                "text": "hi",
                "distinct_id": "visitor",
                "session_id": "browser",
            },
        )
        if mode == "text"
        else httpx.Request(
            "POST",
            "http://test/turn",
            data={
                "distinct_id": "visitor",
                "session_id": "browser",
            },
            files={"audio": ("clip.webm", b"audio", "audio/webm")},
        )
    )
    body = request.read()
    requested = False
    responses = []
    original = runtime._sse_response

    def retain_response(*args):
        response = original(*args)
        responses.append(response)
        return response

    monkeypatch.setattr(runtime, "_sse_response", retain_response)

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": body, "more_body": False}
        if receive_disconnect is not None:
            await receive_disconnect.wait()
            return {"type": "http.disconnect"}
        await asyncio.Future()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/turn",
        "raw_path": b"/turn",
        "query_string": b"",
        "headers": [(name.lower(), value) for name, value in request.headers.raw],
        "server": ("test", 80),
        "client": ("test", 123),
    }
    try:
        await runtime.app(scope, receive, send)
    finally:
        # Response cleanup must have happened before ASGI returns or raises.
        assert len(responses) == 1
        assert responses[0].body_iterator.ag_frame is None


def assert_turn_trace(events, mode, error):
    roots = [props for event, _, props, _ in events if event == "$ai_trace"]
    assert len(roots) == 1
    root = roots[0]
    if error == "CancelledError":
        # A caller interrupting or hanging up is recorded as cancelled, not as a failure.
        assert root["$ai_is_error"] is False and root["$ai_cancelled"] is True
        assert "$ai_error" not in root
        error = None
    assert root["$ai_is_error"] is (error is not None)
    if error:
        assert root["$ai_error"] == error
    for _, person, props, _ in events:
        assert person == "visitor"
        assert props["$ai_trace_id"] == root["$ai_trace_id"]
        assert props["$ai_session_id"] == root["$ai_session_id"]
        assert props["$session_id"] == "browser"
        assert props["interaction_mode"] == mode
    if mode == "voice":
        assert events[0][2]["$ai_span_name"] == "speech transcription"
    return root


@pytest.mark.parametrize("mode", ["text", "voice"])
@pytest.mark.parametrize("stage", ["start", "event", "done", "end"])
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_asgi_disconnect_finishes_trace(monkeypatch, events, mode, stage, spec):
    from starlette.requests import ClientDisconnect

    monkeypatch.setattr(turns, "_transcribe", AsyncMock(return_value="question"))
    monkeypatch.setattr(turns, "_synthesize", AsyncMock(return_value=b"audio"))
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("Hello."))
    disconnect = asyncio.Event()

    async def send(message):
        body = message.get("body", b"")
        target = {
            "start": message["type"] == "http.response.start",
            "event": bool(body) and b'"type": "done"' not in body,
            "done": b'"type": "done"' in body,
            "end": message["type"] == "http.response.body" and not message.get("more_body", True),
        }[stage]
        if target:
            if spec == "2.4":
                raise OSError("connection gone")
            disconnect.set()
            await asyncio.Future()  # Disconnect cancels a blocked transport send.

    if spec == "2.4":
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(invoke_turn(monkeypatch, mode, spec=spec, send=send), 2)
    else:
        await asyncio.wait_for(
            invoke_turn(
                monkeypatch,
                mode,
                spec=spec,
                send=send,
                receive_disconnect=disconnect,
            ),
            2,
        )
    assert_turn_trace(events, mode, "OSError" if spec == "2.4" else "CancelledError")


@pytest.mark.parametrize("mode,work", [("text", "model"), ("voice", "model"), ("voice", "tts")])
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_asgi_cancel_during_provider_work(monkeypatch, events, mode, work, spec):
    started = asyncio.Event()
    cleaned = asyncio.Event()
    monkeypatch.setattr(turns, "_transcribe", AsyncMock(return_value="question"))

    async def blocked():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()

    if work == "model":

        async def complete(**kwargs):
            await blocked()

        monkeypatch.setattr(provider, "complete", complete)
    else:
        monkeypatch.setattr(provider, "complete", make_acompletion_stream("Hello."))

        async def synthesize(text):
            await blocked()

        monkeypatch.setattr(turns, "_synthesize", synthesize)

    async def send(message):
        pass

    task = asyncio.create_task(invoke_turn(monkeypatch, mode, spec=spec, send=send))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert_turn_trace(events, mode, "CancelledError")


@pytest.mark.parametrize("mode", ["text", "voice"])
async def test_response_shields_nested_iterator_cleanup(monkeypatch, events, mode):
    monkeypatch.setattr(turns, "_transcribe", AsyncMock(return_value="question"))
    monkeypatch.setattr(turns, "_synthesize", AsyncMock(return_value=b"audio"))
    closed = False

    async def stream(*args):
        nonlocal closed
        try:
            yield "Hello."
        finally:
            await anyio.sleep(0)  # Cleanup must survive the enclosing cancelled scope.
            closed = True

    monkeypatch.setattr(agent_runtime, "_model_tokens", stream)
    with anyio.CancelScope() as cancel_scope:

        async def send(message):
            body = message.get("body", b"")
            if b'"type": "token"' in body or b'"type": "sentence"' in body:
                cancel_scope.cancel()
                await anyio.sleep(0)

        await invoke_turn(monkeypatch, mode, spec="2.4", send=send)
    assert closed
    assert_turn_trace(events, mode, "CancelledError")


@pytest.mark.parametrize(
    "mode,failure",
    [
        ("text", None),
        ("text", "llm"),
        ("voice", None),
        ("voice", "llm"),
        ("voice", "tts"),
    ],
)
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_asgi_completion_and_provider_error(monkeypatch, events, mode, failure, spec):
    monkeypatch.setattr(turns, "_transcribe", AsyncMock(return_value="question"))
    monkeypatch.setattr(
        turns,
        "_synthesize",
        AsyncMock(side_effect=RuntimeError("down"))
        if failure == "tts"
        else AsyncMock(return_value=b"audio"),
    )
    llm = (
        AsyncMock(side_effect=RuntimeError("down"))
        if failure == "llm"
        else make_acompletion_stream("Hello.")
    )
    monkeypatch.setattr(provider, "complete", llm)
    bodies = []

    async def send(message):
        # Root traces must wait for transport completion, even for done/error.
        assert not any(event == "$ai_trace" for event, *_ in events)
        bodies.append(message.get("body", b""))

    await invoke_turn(monkeypatch, mode, spec=spec, send=send)
    root = assert_turn_trace(events, mode, "RuntimeError" if failure else None)
    assert llm.await_args.kwargs["trace_id"] == root["$ai_trace_id"]
    assert llm.await_args.kwargs["metadata"]["parent_run_id"] == root["$ai_span_id"]
    assert (b'"type": "error"' if failure else b'"type": "done"') in b"".join(bodies)


@pytest.mark.parametrize("mode", ["text", "voice"])
async def test_disconnect_closes_model_connection(monkeypatch, events, mode):
    from types import SimpleNamespace

    monkeypatch.setattr(turns, "_transcribe", AsyncMock(return_value="question"))
    monkeypatch.setattr(turns, "_synthesize", AsyncMock(return_value=b"audio"))
    closed = False

    async def complete(**kwargs):
        async def tokens():
            nonlocal closed
            try:
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello."))]
                )
                await asyncio.Future()
            finally:
                await anyio.sleep(0)
                closed = True

        return tokens()

    monkeypatch.setattr(provider, "complete", complete)
    with anyio.CancelScope() as cancel_scope:

        async def send(message):
            body = message.get("body", b"")
            if b'"type": "token"' in body or b'"type": "sentence"' in body:
                cancel_scope.cancel()
                await anyio.sleep(0)

        await invoke_turn(monkeypatch, mode, spec="2.4", send=send)
        assert closed
    assert closed
    assert_turn_trace(events, mode, "CancelledError")


@pytest.mark.parametrize("mode", ["text", "voice"])
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_provider_failure_survives_transport_failure(monkeypatch, events, mode, spec):
    from starlette.requests import ClientDisconnect

    monkeypatch.setattr(turns, "_transcribe", AsyncMock(return_value="question"))
    monkeypatch.setattr(provider, "complete", AsyncMock(side_effect=ValueError("down")))
    disconnect = asyncio.Event()

    async def send(message):
        if b'"type": "error"' in message.get("body", b""):
            if spec == "2.4":
                raise OSError("gone")
            disconnect.set()
            await asyncio.Future()

    if spec == "2.4":
        with pytest.raises(ClientDisconnect):
            await invoke_turn(monkeypatch, mode, spec=spec, send=send)
    else:
        await invoke_turn(monkeypatch, mode, spec=spec, send=send, receive_disconnect=disconnect)
    assert_turn_trace(events, mode, "ValueError")


async def test_turn_finish_is_idempotent_and_observation_private(monkeypatch, events):
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("ok"))
    stream = await turns.start(turns.TextTurn("hi"))
    assert not hasattr(stream, "observation")
    await stream.aclose()
    stream.finish(OSError("gone"))
    stream.finish()
    roots = [props for event, _, props, _ in events if event == "$ai_trace"]
    assert len(roots) == 1
    assert roots[0]["$ai_error"] == "OSError"


async def test_model_error_survives_connection_cleanup_error(monkeypatch, events):
    class BrokenConnection:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ValueError("provider failed")

        async def aclose(self):
            raise OSError("cleanup failed")

    monkeypatch.setattr(provider, "complete", AsyncMock(return_value=BrokenConnection()))
    response = TestClient(runtime.app).post("/turn", json={"text": "hi"})
    assert '"type": "error"' in response.text
    roots = [props for event, _, props, _ in events if event == "$ai_trace"]
    assert len(roots) == 1
    assert roots[0]["$ai_error"] == "ValueError"


def test_application_shutdown_flushes_telemetry_off_event_loop(monkeypatch):
    import threading

    loop_threads = []
    shutdown_threads = []

    async def health():
        loop_threads.append(threading.get_ident())
        return {}

    monkeypatch.setattr(runtime, "shutdown", lambda: shutdown_threads.append(threading.get_ident()))
    runtime.app.add_api_route("/shutdown-test", health)
    try:
        with TestClient(runtime.app) as client:
            assert client.get("/shutdown-test").status_code == 200
        assert len(shutdown_threads) == 1
        assert shutdown_threads[0] != loop_threads[0]
    finally:
        runtime.app.router.routes.pop()


def test_trace_event_carries_input_and_output_state(monkeypatch, events):
    client = TestClient(runtime.app)

    async def complete(**kwargs):
        return await make_acompletion_stream("Hello there.")()

    monkeypatch.setattr(provider, "complete", complete)
    response = client.post("/turn", json={"text": "hi", "distinct_id": "visitor"})
    assert response.status_code == 200
    (trace,) = [e for e in events if e[0] == "$ai_trace"]
    assert trace[2]["$ai_input_state"] == "hi"
    assert "Hello there." in trace[2]["$ai_output_state"]


def test_http_status_is_recorded_for_httpx_errors(events):
    observation = obs.TurnObservation("call", "visitor", None, "voice", "v")
    request = httpx.Request("POST", "https://api.elevenlabs.io/v1/text-to-speech/v")
    error = httpx.HTTPStatusError(
        "rate limited", request=request, response=httpx.Response(429, request=request)
    )
    with pytest.raises(httpx.HTTPStatusError), observation.span("speech synthesis"):
        raise error
    (span,) = [props for event, _, props, _ in events if event == "$ai_span"]
    assert span["$ai_error"] == "HTTPStatusError" and span["$ai_http_status"] == 429


def test_turn_span_counts_removed_links(events):
    observation = obs.TurnObservation("call", "visitor", None, "text", "v")
    observation.unverified_links = 2
    observation.finish()
    (turn,) = [props for event, _, props, _ in events if event == "$ai_span"]
    assert turn["unverified_link_count"] == 2


def test_trace_state_is_bounded():
    states = obs._states("q" * 5000, "a" * 5000)
    assert len(states["$ai_input_state"]) == obs._STATE_LIMIT
    assert len(states["$ai_output_state"]) == obs._STATE_LIMIT
    assert obs._states(None, "") == {}


def _voice_turn(monkeypatch, **fields):
    monkeypatch.setattr(turns, "_transcribe", AsyncMock(return_value="question"))
    monkeypatch.setattr(turns, "_synthesize", AsyncMock(return_value=b"audio"))
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("One.", " Two."))
    return TestClient(runtime.app).post(
        "/turn",
        data={"distinct_id": "visitor", **fields},
        files={"audio": ("clip.webm", b"audio", "audio/webm")},
    )


def test_voice_turn_carries_call_id_turn_index_and_timings(monkeypatch, events):
    response = _voice_turn(monkeypatch, call_id="call-1", turn_index="3")
    assert response.status_code == 200
    assert all(props["call_id"] == "call-1" for _, _, props, _ in events)
    (turn,) = [
        props
        for event, _, props, _ in events
        if event == "$ai_span" and props["$ai_span_name"] == obs.TURN_SPAN_NAME
    ]
    assert turn["turn_index"] == 3
    assert turn["first_audio_seconds"] >= 0
    (generation,) = [props for event, _, props, _ in events if event == "$ai_generation"]
    assert generation["$ai_stream"] is True
    assert 0 <= generation["$ai_time_to_first_token"] <= generation["$ai_latency"]


@pytest.mark.parametrize("turn_index", ["abc", "-1", "1.5", ""])
def test_malformed_turn_index_is_dropped_not_rejected(monkeypatch, events, turn_index):
    assert _voice_turn(monkeypatch, turn_index=turn_index).status_code == 200
    spans = [props for event, _, props, _ in events if event == "$ai_span"]
    assert all("turn_index" not in props and "call_id" not in props for props in spans)


def test_greeting_is_turn_zero_of_the_call(monkeypatch, events):
    turns._greeting_audio.clear()
    monkeypatch.setattr(turns, "_synthesize", AsyncMock(return_value=b"audio"))
    response = TestClient(runtime.app).post("/greeting", json={"call_id": "call-1"})
    assert response.status_code == 200
    (turn,) = [
        props
        for event, _, props, _ in events
        if event == "$ai_span" and props["$ai_span_name"] == obs.TURN_SPAN_NAME
    ]
    assert turn["call_id"] == "call-1"
    assert turn["turn_index"] == 0
    assert turn["first_audio_seconds"] >= 0
