"""HTTP adapter and `/turn` wire contracts (offline — provider seams mocked)."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from hireme_agent import runtime as agent_runtime

import app as app_module
import conversation_turn as turn_module
from tests.conftest import make_acompletion_stream, provider

client = TestClient(app_module.app)


def test_api_startup_rejects_invalid_gpt_oss_effort(monkeypatch):
    monkeypatch.setattr(agent_runtime, "MODEL", "groq/openai/gpt-oss-120b")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "extreme")
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"), TestClient(app_module.app):
        pytest.fail("API started with invalid reasoning effort")


def parse_sse(raw: str) -> list[dict]:
    """Parse `data: {json}` SSE lines into a list of event dicts."""
    return [
        json.loads(line[len("data:") :].strip())
        for line in raw.splitlines()
        if line.startswith("data:")
    ]


def test_health() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["model"] == agent_runtime.MODEL
    assert body["provider"] == "groq"
    assert body["provider_key"] == "GROQ_API_KEY"
    assert isinstance(body["provider_key_set"], bool)


def test_health_provider_key_set_reflects_env(monkeypatch) -> None:
    # /health reports whether the Groq key is present, so a
    # misconfigured .env is visible with one curl (no app turn needed).
    monkeypatch.setattr(app_module, "MODEL", "groq/openai/gpt-oss-120b")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
    assert client.get("/health").json()["provider_key_set"] is True
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert client.get("/health").json()["provider_key_set"] is False


def test_turn_text_streams_tokens_and_done(monkeypatch) -> None:
    agent_runtime._conversations.clear()
    monkeypatch.setattr(
        provider,
        "complete",
        make_acompletion_stream("Hello", ", ", None, "world"),
    )
    resp = client.post("/turn", json={"text": "Tell me about him"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(resp.text)
    tokens = [e["text"] for e in events if e["type"] == "token"]
    assert tokens == ["Hello", ", ", "world"]  # None delta is dropped

    done = events[-1]
    assert done["type"] == "done"
    conversation_id = done["conversation_id"]
    assert conversation_id

    # Conversation state updated: user turn + accumulated assistant reply persisted.
    history = agent_runtime._conversations[conversation_id]
    assert history[0] == {"role": "user", "content": "Tell me about him"}
    assert history[1] == {"role": "assistant", "content": "Hello, world"}


def test_turn_text_threads_conversation_id(monkeypatch) -> None:
    agent_runtime._conversations.clear()
    agent_runtime._conversations["prev"] = []
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("ok"))
    resp = client.post("/turn", json={"text": "hi", "conversation_id": "prev"})
    events = parse_sse(resp.text)
    assert events[-1]["type"] == "done"
    assert events[-1]["conversation_id"] == "prev"
    assert events[-1]["trace_id"]
    assert agent_runtime._conversations["prev"][-1] == {"role": "assistant", "content": "ok"}


def test_turn_empty_text_400(monkeypatch) -> None:
    monkeypatch.setattr(provider, "complete", AsyncMock())
    resp = client.post("/turn", json={"text": "   "})
    assert resp.status_code == 400


def test_turn_voice_streams_transcript_then_sentence_audio(monkeypatch) -> None:
    agent_runtime._conversations.clear()
    monkeypatch.setattr(turn_module, "_transcribe", AsyncMock(return_value="what I heard"))
    monkeypatch.setattr(
        provider,
        "complete",
        make_acompletion_stream("Hi there.", " How are you?"),
    )
    monkeypatch.setattr(turn_module, "_synthesize", AsyncMock(return_value=b"ID3audio"))
    resp = client.post("/turn", files={"audio": ("clip.webm", b"rawaudio", "audio/webm")})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)

    # First event is the caller's transcript (so the UI shows the question early).
    assert events[0] == {"type": "transcript", "text": "what I heard"}

    # One sentence event per sentence, terminal punctuation kept, audio attached.
    sentences = [e for e in events if e["type"] == "sentence"]
    assert [s["text"] for s in sentences] == ["Hi there.", "How are you?"]
    for s in sentences:
        assert base64.b64decode(s["audio"]) == b"ID3audio"

    done = events[-1]
    assert done["type"] == "done"
    conversation_id = done["conversation_id"]
    assert conversation_id

    # Conversation state: user turn + accumulated assistant reply persisted.
    history = agent_runtime._conversations[conversation_id]
    assert history[0] == {"role": "user", "content": "what I heard"}
    assert history[1] == {"role": "assistant", "content": "Hi there. How are you?"}


def test_voice_keeps_original_caption_but_normalizes_tts_and_event_order(monkeypatch) -> None:
    agent_runtime._conversations.clear()
    monkeypatch.setattr(turn_module, "_transcribe", AsyncMock(return_value="book time"))
    monkeypatch.setattr(
        provider,
        "complete",
        make_acompletion_stream("Use [Alex's calendar](https://github.com/example)."),
    )
    synthesize = AsyncMock(return_value=b"audio")
    monkeypatch.setattr(turn_module, "_synthesize", synthesize)

    resp = client.post("/turn", files={"audio": ("clip.webm", b"rawaudio", "audio/webm")})
    events = parse_sse(resp.text)

    assert [event["type"] for event in events] == ["transcript", "sentence", "done"]
    assert events[1]["text"] == "Use [Alex's calendar](https://github.com/example)."
    synthesize.assert_awaited_once_with("Use Alex's calendar.")


def test_turn_voice_missing_file_400() -> None:
    # multipart form with no `audio` field
    resp = client.post("/turn", data={"session_id": "x"}, files={"notaudio": ("f", b"y")})
    assert resp.status_code == 400


def test_turn_voice_empty_audio_400() -> None:
    resp = client.post("/turn", files={"audio": ("clip.webm", b"", "audio/webm")})
    assert resp.status_code == 400


def test_shared_secret_enforced(monkeypatch) -> None:
    monkeypatch.setenv("API_SHARED_SECRET", "topsecret")
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("r"))

    # missing token
    assert client.post("/turn", json={"text": "hi"}).status_code == 401
    # wrong token
    bad = client.post("/turn", json={"text": "hi"}, headers={"authorization": "Bearer nope"})
    assert bad.status_code == 401
    # correct token
    ok = client.post("/turn", json={"text": "hi"}, headers={"authorization": "Bearer topsecret"})
    assert ok.status_code == 200


def test_drain_sentences_keeps_punctuation_and_holds_tail() -> None:
    # Keeps terminal punctuation (so TTS gets ?/! intonation) and holds the tail.
    sentences, rest = turn_module._drain_sentences("Hi there! How are you? I'm")
    assert sentences == ["Hi there!", "How are you?"]
    assert rest == " I'm"


def test_drain_sentences_does_not_split_decimals() -> None:
    # A period followed by a digit is not a boundary, so "3.5" stays intact.
    sentences, rest = turn_module._drain_sentences("It costs 3.5 dollars")
    assert sentences == []
    assert rest == "It costs 3.5 dollars"


def test_drain_sentences_empty_when_no_terminator() -> None:
    sentences, rest = turn_module._drain_sentences("still going")
    assert sentences == []
    assert rest == "still going"


def test_turn_malformed_json_400() -> None:
    # A body that is not valid JSON is a client error, not a 500 (R5.2).
    resp = client.post("/turn", content="{not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400


def test_turn_wrong_content_type_400() -> None:
    # A non-multipart, non-JSON body cannot be parsed as a turn (R5.3).
    resp = client.post("/turn", content="hello", headers={"content-type": "text/plain"})
    assert resp.status_code == 400


def test_turn_voice_empty_transcript_400(monkeypatch) -> None:
    # Silence in, nothing transcribed: reject before the LLM runs (R3.3 / R4.3).
    agent_runtime._conversations.clear()
    monkeypatch.setattr(turn_module, "_transcribe", AsyncMock(return_value="   "))
    llm = AsyncMock()
    monkeypatch.setattr(provider, "complete", llm)
    resp = client.post("/turn", files={"audio": ("clip.webm", b"rawaudio", "audio/webm")})
    assert resp.status_code == 400
    assert llm.await_count == 0
    assert agent_runtime._conversations == {}


def test_turn_text_emits_error_event_when_llm_fails(monkeypatch) -> None:
    # A mid-stream LLM failure ends the stream with an `error` event, not silence (R4.4).
    agent_runtime._conversations.clear()
    monkeypatch.setattr(provider, "complete", AsyncMock(side_effect=RuntimeError("boom")))
    resp = client.post("/turn", json={"text": "hi"})
    assert resp.status_code == 200  # the stream opened before the failure
    events = parse_sse(resp.text)
    assert events[-1]["type"] == "error"
    assert "done" not in [e["type"] for e in events]


def test_turn_voice_emits_error_event_when_tts_fails(monkeypatch) -> None:
    # A TTS failure after the transcript ends the stream with an `error` event (R4.4).
    agent_runtime._conversations.clear()
    monkeypatch.setattr(turn_module, "_transcribe", AsyncMock(return_value="question"))
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("Answer one."))
    monkeypatch.setattr(turn_module, "_synthesize", AsyncMock(side_effect=RuntimeError("tts down")))
    resp = client.post("/turn", files={"audio": ("clip.webm", b"rawaudio", "audio/webm")})
    events = parse_sse(resp.text)
    assert events[0] == {"type": "transcript", "text": "question"}
    assert events[-1]["type"] == "error"


def test_conversation_count_is_bounded(monkeypatch) -> None:
    # Many one-off conversations evict the oldest instead of growing forever (R3.4).
    agent_runtime._conversations.clear()
    monkeypatch.setattr(agent_runtime, "_MAX_CONVERSATIONS", 3)
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("ok"))
    first = None
    for _ in range(5):
        events = parse_sse(client.post("/turn", json={"text": "hi"}).text)
        cid = events[-1]["conversation_id"]
        first = first or cid
    assert len(agent_runtime._conversations) == 3
    assert first not in agent_runtime._conversations  # oldest evicted


def test_turn_voice_stt_failure_returns_502(monkeypatch) -> None:
    # An ElevenLabs STT failure surfaces as a clean 502, not an unhandled 500 (R4.2).
    agent_runtime._conversations.clear()
    req = httpx.Request("POST", "https://api.elevenlabs.io/v1/speech-to-text")
    resp = httpx.Response(400, json={"detail": "bad request"}, request=req)
    err = httpx.HTTPStatusError("400", request=req, response=resp)
    monkeypatch.setattr(turn_module, "_transcribe", AsyncMock(side_effect=err))
    out = client.post("/turn", files={"audio": ("clip.webm", b"rawaudio", "audio/webm")})
    assert out.status_code == 502


def test_drain_sentences_handles_abbreviation_like_prefix() -> None:
    # "Mr." mid-sentence: the period is followed by a space, so it would split —
    # document the current behavior (split on ". ") so a future fix is deliberate.
    sentences, rest = turn_module._drain_sentences("He is based in Lisbon. He likes dbt.")
    assert sentences == ["He is based in Lisbon.", "He likes dbt."]
    assert rest == ""


def test_drain_sentences_multiple_terminators_collapse() -> None:
    # "Wait… really?!" — a run of terminators stays with its sentence.
    sentences, rest = turn_module._drain_sentences("Really?! Yes.")
    assert sentences == ["Really?!", "Yes."]
    assert rest == ""


@pytest.mark.parametrize("text", [123, ["bad"], {"bad": "value"}, None])
def test_non_string_text_returns_400(monkeypatch, text):
    model = AsyncMock()
    monkeypatch.setattr(provider, "complete", model)
    assert client.post("/turn", json={"text": text}).status_code == 400
    model.assert_not_awaited()
    assert agent_runtime._conversations == {}


@pytest.mark.parametrize("failure", [httpx.ConnectError("offline"), RuntimeError("no key")])
def test_transcription_transport_and_configuration_errors_return_502(monkeypatch, failure):
    monkeypatch.setattr(turn_module, "_transcribe", AsyncMock(side_effect=failure))
    response = client.post("/turn", files={"audio": ("turn.webm", b"audio", "audio/webm")})
    assert response.status_code == 502
    assert response.json()["detail"] == "speech-to-text failed"
    assert agent_runtime._conversations == {}


@pytest.mark.parametrize("field", ["conversation_id", "distinct_id", "session_id"])
def test_non_string_identifiers_return_400(field):
    assert client.post("/turn", json={"text": "question", field: ["invalid"]}).status_code == 400


def test_adapter_encodes_module_events_without_changing_payload(monkeypatch):
    expected = [
        {"type": "token", "text": "Hello"},
        {"type": "done", "conversation_id": "conversation"},
    ]

    async def events():
        for event in expected:
            yield event

    from hireme_agent.observability import TurnObservation

    stream = turn_module.TurnStream(
        TurnObservation("conversation", "person", "visit", "text", "test")
    )
    stream._events = events()
    start = AsyncMock(return_value=stream)
    monkeypatch.setattr(turn_module, "start", start)
    response = client.post(
        "/turn",
        json={
            "text": "question",
            "conversation_id": "conversation",
            "distinct_id": "person",
            "session_id": "visit",
        },
    )
    start.assert_awaited_once_with(
        turn_module.TextTurn("question", "conversation", "person", "visit")
    )
    assert parse_sse(response.text) == expected
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


async def test_closing_http_event_iterator_preserves_history_and_closes_turn(monkeypatch):
    original = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "reply"}]
    agent_runtime._conversations["existing"] = original.copy()
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("partial", " tail"))
    turn = await turn_module.start(turn_module.TextTurn("new", conversation_id="existing"))
    encoded = app_module._sse(turn)
    assert parse_sse(await anext(encoded)) == [{"type": "token", "text": "partial"}]
    await encoded.aclose()
    assert agent_runtime._conversations["existing"] == original
    with pytest.raises(StopAsyncIteration):
        await anext(turn)


def test_greeting_streams_sentence_then_done(monkeypatch) -> None:
    turn_module._greeting_audio.clear()
    monkeypatch.setattr(turn_module, "_synthesize", AsyncMock(return_value=b"audio"))
    monkeypatch.delenv("API_SHARED_SECRET", raising=False)
    resp = client.post("/greeting", json={"distinct_id": "visitor", "session_id": "browser"})
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert [e["type"] for e in events] == ["sentence", "done"]
    assert events[0]["text"] == agent_runtime.greeting()
    assert base64.b64decode(events[0]["audio"]) == b"audio"
    assert events[1]["conversation_id"] in agent_runtime._conversations


def test_greeting_accepts_an_empty_body_and_rejects_bad_json(monkeypatch) -> None:
    turn_module._greeting_audio.clear()
    monkeypatch.setattr(turn_module, "_synthesize", AsyncMock(return_value=b"audio"))
    monkeypatch.delenv("API_SHARED_SECRET", raising=False)
    assert client.post("/greeting").status_code == 200
    assert client.post("/greeting", content=b"[1]").status_code == 400
    assert client.post("/greeting", content=b"nope").status_code == 400


def test_greeting_requires_the_shared_secret(monkeypatch) -> None:
    turn_module._greeting_audio.clear()
    monkeypatch.setattr(turn_module, "_synthesize", AsyncMock(return_value=b"audio"))
    monkeypatch.setenv("API_SHARED_SECRET", "s3cret")
    assert client.post("/greeting").status_code == 401
    ok = client.post("/greeting", headers={"authorization": "Bearer s3cret"})
    assert ok.status_code == 200
