"""Unit tests for the ElevenLabs STT/TTS helpers (offline — httpx mocked via respx)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import conversation_turn as turn_module

STT_URL = f"{turn_module._EL_BASE}/speech-to-text"


def test_api_key_missing_raises(monkeypatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        turn_module._api_key()


def test_api_key_present(monkeypatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k-123")
    assert turn_module._api_key() == "k-123"


@respx.mock
async def test_transcribe_returns_stripped_text(monkeypatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k-123")
    route = respx.post(STT_URL).mock(
        return_value=httpx.Response(200, json={"text": "  hi there  "})
    )
    out = await turn_module._transcribe(b"audiobytes", "audio/webm")
    assert out == "hi there"
    assert route.called
    sent = route.calls.last.request
    body = sent.content
    assert b"model_id" in body  # multipart form carried the STT model
    assert b"audiobytes" in body  # and the audio file


@respx.mock
async def test_transcribe_raises_on_http_error(monkeypatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k-123")
    respx.post(STT_URL).mock(return_value=httpx.Response(402, json={"detail": "nope"}))
    with pytest.raises(httpx.HTTPStatusError):
        await turn_module._transcribe(b"x")


def test_synthesize_missing_voice_id_raises(monkeypatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k-123")
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    import asyncio

    with pytest.raises(RuntimeError, match="ELEVENLABS_VOICE_ID"):
        asyncio.run(turn_module._synthesize("hello"))


@respx.mock
async def test_synthesize_posts_to_voice_and_returns_bytes(monkeypatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k-123")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-xyz")
    url = f"{turn_module._EL_BASE}/text-to-speech/voice-xyz"
    route = respx.post(url).mock(return_value=httpx.Response(200, content=b"ID3mp3"))

    out = await turn_module._synthesize("speak this")

    assert out == b"ID3mp3"
    assert route.called
    sent = route.calls.last.request
    payload = json.loads(sent.content)
    assert payload["text"] == "speak this"
    assert payload["model_id"] == turn_module._TTS_MODEL
    assert payload["voice_settings"]["speed"] == 1.12


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("0.1", 0.7), ("0.9", 0.9), ("2", 1.2), ("not-a-number", 1.12)],
)
def test_tts_speed_is_safely_constrained(monkeypatch, configured, expected) -> None:
    monkeypatch.setenv("ELEVENLABS_TTS_SPEED", configured)
    assert turn_module._tts_speed() == expected


def test_spoken_text_normalizes_markdown_and_urls_without_changing_caption() -> None:
    caption = "## Next step\n- Book **Alex** at [his calendar](https://cal.example.com/alex)."
    assert turn_module._spoken_text(caption) == "Next step Book Alex at his calendar."
    assert (
        caption == "## Next step\n- Book **Alex** at [his calendar](https://cal.example.com/alex)."
    )


def test_spoken_text_makes_bare_url_pronounceable() -> None:
    assert turn_module._spoken_text("Visit https://example.com/path?q=1") == "Visit example dot com"


@respx.mock
async def test_synthesize_raises_on_http_error_without_retrying(monkeypatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k-123")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-xyz")
    url = f"{turn_module._EL_BASE}/text-to-speech/voice-xyz"
    route = respx.post(url).mock(return_value=httpx.Response(402, json={"detail": "paid"}))
    with pytest.raises(httpx.HTTPStatusError):
        await turn_module._synthesize("hi")
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize("first", [httpx.Response(429), httpx.Response(503)])
async def test_synthesize_retries_a_transient_failure_once(monkeypatch, first) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k-123")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-xyz")
    monkeypatch.setattr(turn_module, "_TTS_RETRY_DELAY", 0)
    url = f"{turn_module._EL_BASE}/text-to-speech/voice-xyz"
    route = respx.post(url).mock(side_effect=[first, httpx.Response(200, content=b"mp3")])
    assert await turn_module._synthesize("hi") == b"mp3"
    assert route.call_count == 2


@respx.mock
async def test_synthesize_gives_up_after_the_retry(monkeypatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k-123")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-xyz")
    monkeypatch.setattr(turn_module, "_TTS_RETRY_DELAY", 0)
    url = f"{turn_module._EL_BASE}/text-to-speech/voice-xyz"
    route = respx.post(url).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await turn_module._synthesize("hi")
    assert route.call_count == 2
