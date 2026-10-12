"""Opt-in integration tests that hit the real LLM / ElevenLabs APIs.

Skipped unless RUN_INTEGRATION=1. These cost credits and are non-deterministic, so
assertions are deliberately loose. Run with the project `.env` loaded, e.g.:

    set -a; . ../.env; set +a
    RUN_INTEGRATION=1 .venv/bin/pytest -q -m integration

The live project runs on Groq; GROQ_API_KEY must be set.
"""

from __future__ import annotations

import os

import pytest

import conversation_turn as turn_module
from conversation_turn import TextTurn, start

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 to run real-API integration tests",
    ),
]


def _has_llm_key() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


async def _text_reply(text: str) -> tuple[str, str]:
    events = [event async for event in await start(TextTurn(text))]
    assert events[-1]["type"] == "done", events
    return "".join(event["text"] for event in events if event["type"] == "token"), events[-1][
        "conversation_id"
    ]


async def test_text_turn_grounded_smoke() -> None:
    if not _has_llm_key():
        pytest.skip("GROQ_API_KEY is not set")
    reply, conversation_id = await _text_reply("Tell me about Alex's experience.")
    assert reply.strip()
    assert conversation_id
    assert "Example Corp" in reply


@pytest.mark.parametrize(
    "question",
    [
        "Give me a concise career summary.",
        "What did Alex own at Sample Labs?",
        "What impact did the payments platform work have?",
        "How technical is he?",
        "What is his leadership experience?",
        "How does he make product decisions?",
        "Can you show me public evidence?",
        "Has he run Kubernetes in production?",
        "What compensation does he expect?",
        "I'd like to speak with Alex. What is the next step?",
        "Please take my details and tell Alex to follow up.",
        "[unclear audio] ruido sem pergunta clara",
    ],
)
async def test_simulated_recruiter_scenarios_are_concise(question: str) -> None:
    if not _has_llm_key():
        pytest.skip("GROQ_API_KEY is not set")
    reply, _ = await _text_reply(question)
    assert reply.strip()
    assert len(reply.split()) <= 100


async def test_tts_then_stt_roundtrip() -> None:
    if not (os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("ELEVENLABS_VOICE_ID")):
        pytest.skip("ElevenLabs credentials not set")
    audio = await turn_module._synthesize("Hello, this is a test.")
    assert audio[:3] == b"ID3"
    transcript = await turn_module._transcribe(audio, "audio/mpeg")
    assert transcript.strip()
