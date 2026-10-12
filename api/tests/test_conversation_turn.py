"""Conversation Turn contracts using production events and offline provider seams."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from hireme_agent import runtime as agent_runtime

import conversation_turn as turns
from conversation_turn import TextTurn, TranscriptionError, TurnValidationError, VoiceTurn, start
from tests.conftest import make_acompletion_stream, provider


async def collect(turn):
    return [event async for event in await start(turn)]


def mock_voice(monkeypatch, *deltas):
    monkeypatch.setattr(turns, "_transcribe", AsyncMock(return_value="voice question"))
    monkeypatch.setattr(turns, "_synthesize", AsyncMock(return_value=b"audio"))
    model = make_acompletion_stream(*deltas)
    monkeypatch.setattr(provider, "complete", model)
    return model


@pytest.mark.parametrize("text", ["", "  ", None, 123])
async def test_invalid_text_is_rejected_before_allocation_or_model(monkeypatch, text):
    model = AsyncMock()
    monkeypatch.setattr(provider, "complete", model)
    with pytest.raises(TurnValidationError):
        await start(TextTurn(text))
    assert agent_runtime._conversations == {}
    model.assert_not_awaited()


async def test_empty_audio_rejected_before_transcription(monkeypatch):
    transcribe = AsyncMock()
    monkeypatch.setattr(turns, "_transcribe", transcribe)
    with pytest.raises(TurnValidationError, match="empty audio"):
        await start(VoiceTurn(b""))
    transcribe.assert_not_awaited()
    assert agent_runtime._conversations == {}


async def test_transcription_finishes_before_iterator_is_returned(monkeypatch):
    model = mock_voice(monkeypatch, "Answer.")
    events = await start(VoiceTurn(b"recording", content_type="audio/ogg"))
    turns._transcribe.assert_awaited_once_with(b"recording", "audio/ogg")
    model.assert_not_awaited()
    assert agent_runtime._conversations == {}
    assert await anext(events) == {"type": "transcript", "text": "voice question"}
    model.assert_not_awaited()
    await events.aclose()


@pytest.mark.parametrize("transcript", ["", "  "])
async def test_empty_transcript_rejected_before_allocation(monkeypatch, transcript):
    model = mock_voice(monkeypatch, "unused")
    monkeypatch.setattr(turns, "_transcribe", AsyncMock(return_value=transcript))
    with pytest.raises(TurnValidationError, match="could not hear"):
        await start(VoiceTurn(b"silence"))
    model.assert_not_awaited()
    assert agent_runtime._conversations == {}


@pytest.mark.parametrize("failure", [RuntimeError("missing key"), httpx.ConnectError("offline")])
async def test_transcription_failure_is_pre_stream(monkeypatch, failure):
    monkeypatch.setattr(turns, "_transcribe", AsyncMock(side_effect=failure))
    with pytest.raises(TranscriptionError, match="speech-to-text failed"):
        await start(VoiceTurn(b"recording"))
    assert agent_runtime._conversations == {}


async def test_text_order_metadata_and_atomic_pair_commit(monkeypatch):
    model = make_acompletion_stream(" Hello", None, " there. ")
    monkeypatch.setattr(provider, "complete", model)
    stream = await start(TextTurn(" question ", distinct_id="person", session_id="visit"))
    assert await anext(stream) == {"type": "token", "text": " Hello"}
    assert agent_runtime._conversations == {}
    assert await anext(stream) == {"type": "token", "text": " there. "}
    assert agent_runtime._conversations == {}
    done = await anext(stream)
    assert done["type"] == "done"
    cid = done["conversation_id"]
    assert agent_runtime._conversations[cid] == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "Hello there."},
    ]
    kwargs = model.await_args.kwargs
    assert kwargs["model"] == agent_runtime.MODEL
    assert kwargs["stream"] is True
    assert kwargs["trace_id"] == cid  # one trace per call
    assert kwargs["metadata"]["user_id"] == "person"
    assert kwargs["metadata"]["conversation_id"] == cid
    assert kwargs["metadata"]["$ai_session_id"] == cid
    assert kwargs["metadata"]["$session_id"] == "visit"
    assert kwargs["metadata"]["parent_run_id"] == kwargs["trace_id"]
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_unknown_id_creates_fresh_conversation_and_anonymous_trace(monkeypatch):
    model = make_acompletion_stream("ok")
    monkeypatch.setattr(provider, "complete", model)
    events = await collect(TextTurn("hello", conversation_id="unknown"))
    cid = events[-1]["conversation_id"]
    assert cid != "unknown"
    assert model.await_args.kwargs["metadata"]["user_id"] == cid
    assert model.await_args.kwargs["metadata"]["conversation_id"] == cid


async def test_history_is_sent_without_mutation_and_pairs_are_bounded(monkeypatch):
    model = make_acompletion_stream("ok")
    monkeypatch.setattr(provider, "complete", model)
    cid = (await collect(TextTurn("first")))[-1]["conversation_id"]
    original = [message.copy() for message in agent_runtime._conversations[cid]]
    stream = await start(TextTurn("second", conversation_id=cid))
    await anext(stream)
    sent = model.await_args.kwargs["messages"]
    assert sent == [
        {"role": "system", "content": agent_runtime._SYSTEM_PROMPT},
        *original,
        {"role": "user", "content": "second"},
    ]
    assert agent_runtime._conversations[cid] == original
    # The provider cannot change stored messages via the assembled request.
    sent[1]["content"] = "mutated by provider"
    assert agent_runtime._conversations[cid] == original
    assert (await anext(stream))["conversation_id"] == cid
    for i in range(25):
        await collect(TextTurn(f"turn {i}", conversation_id=cid))
    assert len(agent_runtime._conversations[cid]) == 40
    assert [m["role"] for m in agent_runtime._conversations[cid]] == ["user", "assistant"] * 20
    assert agent_runtime._conversations[cid][-2]["content"] == "turn 24"


async def test_conversation_limit_evicts_oldest_only_on_success(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_MAX_CONVERSATIONS", 3)
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("ok"))
    first = (await collect(TextTurn("first")))[-1]["conversation_id"]
    await collect(TextTurn("second"))
    await collect(TextTurn("third"))
    monkeypatch.setattr(provider, "complete", AsyncMock(side_effect=RuntimeError("down")))
    assert (await collect(TextTurn("failed")))[-1]["type"] == "error"
    assert len(agent_runtime._conversations) == 3
    assert first in agent_runtime._conversations
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("ok"))
    await collect(TextTurn("fourth"))
    assert len(agent_runtime._conversations) == 3
    assert first not in agent_runtime._conversations


async def test_shared_prompt_and_voice_metadata(monkeypatch):
    model = mock_voice(monkeypatch, "Answer.")
    await collect(TextTurn("text"))
    text_prompt = model.await_args.kwargs["messages"][0]
    events = await collect(VoiceTurn(b"audio", distinct_id="p", session_id="s"))
    kwargs = model.await_args.kwargs
    assert (
        kwargs["messages"][0]
        == text_prompt
        == {"role": "system", "content": agent_runtime._SYSTEM_PROMPT}
    )
    assert kwargs["metadata"]["user_id"] == "p"
    assert kwargs["metadata"]["$session_id"] == "s"
    assert kwargs["metadata"]["conversation_id"] == events[-1]["conversation_id"]
    assert kwargs["trace_id"] == events[-1]["conversation_id"]


async def test_voice_order_captions_normalization_and_unterminated_tail(monkeypatch):
    mock_voice(monkeypatch, "Use **Python**.", " Visit [Alex](https://github.com/example).", " Bye")
    events = await collect(VoiceTurn(b"audio"))
    assert [e["type"] for e in events] == ["transcript", "sentence", "sentence", "sentence", "done"]
    assert [e["text"] for e in events[1:-1]] == [
        "Use **Python**.",
        "Visit [Alex](https://github.com/example).",
        "Bye",
    ]
    assert [base64.b64decode(e["audio"]) for e in events[1:-1]] == [b"audio"] * 3
    assert [call.args[0] for call in turns._synthesize.await_args_list] == [
        "Use Python.",
        "Visit Alex.",
        "Bye",
    ]
    assert agent_runtime._conversations[events[-1]["conversation_id"]][-1]["content"] == (
        "Use **Python**. Visit [Alex](https://github.com/example). Bye"
    )


async def test_first_sentence_audio_is_yielded_before_later_model_tokens(monkeypatch):
    mock_voice(monkeypatch)
    later_requested = False

    async def tokens(**kwargs):
        async def stream():
            nonlocal later_requested
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="First."))]
            )
            later_requested = True
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=" Second."))]
            )

        return stream()

    monkeypatch.setattr(provider, "complete", tokens)
    events = await start(VoiceTurn(b"audio"))
    assert (await anext(events))["type"] == "transcript"
    assert (await anext(events)) == {"type": "sentence", "text": "First.", "audio": "YXVkaW8="}
    assert not later_requested
    assert agent_runtime._conversations == {}
    assert (await anext(events))["text"] == "Second."
    assert later_requested
    assert agent_runtime._conversations == {}
    assert (await anext(events))["type"] == "done"


@pytest.mark.parametrize("voice", [False, True])
async def test_stream_failure_has_one_error_no_done_and_leaves_prior_history(monkeypatch, voice):
    mock_voice(monkeypatch, "ok")
    agent_runtime._conversations["existing"] = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer"},
    ]
    original = [m.copy() for m in agent_runtime._conversations["existing"]]

    async def broken(**kwargs):
        async def stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="Partial."))]
            )
            raise RuntimeError("generation failed")

        return stream()

    monkeypatch.setattr(provider, "complete", broken)
    turn = (
        VoiceTurn(b"audio", conversation_id="existing")
        if voice
        else TextTurn("new", conversation_id="existing")
    )
    events = await collect(turn)
    assert [e["type"] for e in events].count("error") == 1
    assert events[-1] == {"type": "error", "text": "the reply failed — please try again"}
    assert "done" not in [e["type"] for e in events]
    assert agent_runtime._conversations["existing"] == original


@pytest.mark.parametrize("fail_at", [1, 2, 3])
async def test_synthesis_failure_including_tail_does_not_commit(monkeypatch, fail_at):
    mock_voice(monkeypatch, "First. Second. tail")
    results = [b"audio"] * (fail_at - 1) + [RuntimeError("tts down")]
    monkeypatch.setattr(turns, "_synthesize", AsyncMock(side_effect=results))
    events = await collect(VoiceTurn(b"audio"))
    assert [e["type"] for e in events] == ["transcript"] + ["sentence"] * (fail_at - 1) + ["error"]
    assert agent_runtime._conversations == {}


@pytest.mark.parametrize("voice", [False, True])
async def test_closing_stream_before_done_leaves_prior_history(monkeypatch, voice):
    mock_voice(monkeypatch, "First. Second.")
    agent_runtime._conversations["existing"] = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
    ]
    original = agent_runtime._conversations["existing"].copy()
    turn = (
        VoiceTurn(b"audio", conversation_id="existing")
        if voice
        else TextTurn("new", conversation_id="existing")
    )
    stream = await start(turn)
    if voice:
        assert (await anext(stream))["type"] == "transcript"
    await anext(stream)
    await stream.aclose()
    assert agent_runtime._conversations["existing"] == original


@pytest.mark.parametrize("voice", [False, True])
async def test_cancellation_during_generation_or_synthesis_leaves_history(monkeypatch, voice):
    mock_voice(monkeypatch, "First.")
    agent_runtime._conversations["existing"] = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
    ]
    original = agent_runtime._conversations["existing"].copy()
    waiting = asyncio.Event()

    async def blocked(*args, **kwargs):
        waiting.set()
        await asyncio.Future()

    if voice:
        monkeypatch.setattr(turns, "_synthesize", blocked)
    else:
        monkeypatch.setattr(provider, "complete", blocked)
    turn = (
        VoiceTurn(b"audio", conversation_id="existing")
        if voice
        else TextTurn("new", conversation_id="existing")
    )
    stream = await start(turn)
    if voice:
        await anext(stream)
    task = asyncio.create_task(anext(stream))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent_runtime._conversations["existing"] == original
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_cancelled_voice_turn_closes_model_connection_and_preserves_prior_history(
    monkeypatch,
):
    mock_voice(monkeypatch)
    closed = False

    async def model(**kwargs):
        async def tokens():
            nonlocal closed
            try:
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="First."))]
                )
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=" Second."))]
                )
            finally:
                closed = True

        return tokens()

    monkeypatch.setattr(provider, "complete", model)
    agent_runtime._conversations["existing"] = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
    ]
    original = agent_runtime._conversations["existing"].copy()
    stream = await start(VoiceTurn(b"audio", conversation_id="existing"))
    await anext(stream)
    assert (await anext(stream))["type"] == "sentence"
    assert not closed
    await stream.aclose()
    assert closed
    assert agent_runtime._conversations["existing"] == original


async def test_rate_limited_voice_turn_speaks_a_busy_reply(monkeypatch):
    from hireme_agent.observability import TurnObservation

    class RateLimited(Exception):
        status_code = 429

    class Tokens:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RateLimited("429")

        async def aclose(self):
            pass

    async def synth(text):
        return b"audio"

    monkeypatch.setattr(turns, "_synthesize", synth)
    monkeypatch.setattr(turns.hireme_agent, "start_text", lambda *a: Tokens())
    obs = TurnObservation("c", None, None, "voice", "v")
    turn = turns.VoiceTurn(audio=b"x")
    events = [e async for e in turns._events(turn, "hi", "c", obs)]
    assert [e["type"] for e in events] == ["transcript", "sentence", "done"]
    assert "busy" in events[1]["text"]
    assert events[2]["trace_id"] == obs.trace_id
    assert turns._is_transient_provider_error(RateLimited())


async def test_failed_first_turn_keeps_the_same_call_id(monkeypatch):
    """A busy first turn is not committed, but the id it returned is still the call's id."""

    class RateLimited(Exception):
        status_code = 429

    mock_voice(monkeypatch, "Hello.")
    failing = AsyncMock(side_effect=RateLimited("429"))
    monkeypatch.setattr(provider, "complete", failing)
    first = await collect(VoiceTurn(b"audio"))
    assert "busy" in first[1]["text"]
    call_id = first[-1]["conversation_id"]
    assert first[-1]["trace_id"] == call_id
    assert agent_runtime._conversations == {}

    monkeypatch.setattr(provider, "complete", make_acompletion_stream("Hello."))
    second = await collect(VoiceTurn(b"audio", conversation_id=call_id))
    assert second[-1]["conversation_id"] == call_id
    assert second[-1]["trace_id"] == call_id
    assert list(agent_runtime._conversations) == [call_id]


async def test_unissued_id_is_never_adopted(monkeypatch):
    monkeypatch.setattr(provider, "complete", make_acompletion_stream("ok"))
    events = await collect(TextTurn("hello", conversation_id="chosen-by-client"))
    assert events[-1]["conversation_id"] != "chosen-by-client"


# ── Greeting ─────────────────────────────────────────────────────────────────


async def test_greeting_opens_a_conversation_without_a_model_call(monkeypatch):
    turns._greeting_audio.clear()
    synth = AsyncMock(return_value=b"greeting-audio")
    model = AsyncMock()
    monkeypatch.setattr(turns, "_synthesize", synth)
    monkeypatch.setattr(provider, "complete", model)
    stream = await turns.start_greeting(turns.GreetingTurn(distinct_id="p", session_id="s"))
    events = [event async for event in stream]
    assert [e["type"] for e in events] == ["sentence", "done"]
    text = agent_runtime.greeting()
    assert events[0]["text"] == text
    assert text.startswith("Hi, I'm Alex's AI representative.")
    assert base64.b64decode(events[0]["audio"]) == b"greeting-audio"
    call_id = events[1]["conversation_id"]
    assert events[1]["trace_id"] == call_id
    assert agent_runtime._conversations[call_id] == [{"role": "assistant", "content": text}]
    model.assert_not_awaited()
    synth.assert_awaited_once()
    # Speech sees plain words, never Markdown or placeholders.
    assert "**" not in synth.await_args.args[0]


async def test_greeting_audio_is_synthesized_once_per_process(monkeypatch):
    turns._greeting_audio.clear()
    synth = AsyncMock(return_value=b"audio")
    monkeypatch.setattr(turns, "_synthesize", synth)
    for _ in range(2):
        events = [e async for e in await turns.start_greeting(turns.GreetingTurn())]
        assert events[0]["audio"]
    assert synth.await_count == 1


async def test_concurrent_cold_greetings_share_synthesis(monkeypatch):
    turns._greeting_audio.clear()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def synthesize(text):
        entered.set()
        await release.wait()
        return b"shared"

    synth = AsyncMock(side_effect=synthesize)
    monkeypatch.setattr(turns, "_synthesize", synth)

    async def greet():
        return [e async for e in await turns.start_greeting(turns.GreetingTurn())]

    first = asyncio.create_task(greet())
    await entered.wait()
    second = asyncio.create_task(greet())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    synth.assert_awaited_once()
    assert [base64.b64decode(events[0]["audio"]) for events in results] == [b"shared"] * 2


async def test_greeting_failure_still_opens_the_call_with_text_only(monkeypatch):
    turns._greeting_audio.clear()
    monkeypatch.setattr(turns, "_synthesize", AsyncMock(side_effect=RuntimeError("tts down")))
    events = [e async for e in await turns.start_greeting(turns.GreetingTurn())]
    assert [e["type"] for e in events] == ["sentence", "done"]
    assert events[0]["audio"] == "" and events[0]["text"]
    assert agent_runtime._conversations[events[1]["conversation_id"]]


@pytest.mark.parametrize("cancel", [False, True], ids=["failure", "cancellation"])
async def test_failed_greeting_synthesis_releases_waiting_retry(monkeypatch, cancel):
    turns._greeting_audio.clear()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail(text):
        entered.set()
        await release.wait()
        raise RuntimeError("tts down")

    synth = AsyncMock()

    async def synthesize(text):
        if synth.await_count == 1:
            return await fail(text)
        return b"retry"

    synth.side_effect = synthesize
    monkeypatch.setattr(turns, "_synthesize", synth)

    async def greet():
        return [e async for e in await turns.start_greeting(turns.GreetingTurn())]

    first = asyncio.create_task(greet())
    await entered.wait()
    retry = asyncio.create_task(greet())
    await asyncio.sleep(0)
    assert synth.await_count == 1
    if cancel:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    else:
        release.set()
        failed = await asyncio.wait_for(first, timeout=1)
        assert failed[0]["audio"] == ""
    events = await asyncio.wait_for(retry, timeout=1)
    assert base64.b64decode(events[0]["audio"]) == b"retry"
    cached = await greet()
    assert cached[0]["audio"] == events[0]["audio"]
    assert synth.await_count == 2


async def test_answer_after_greeting_is_not_a_first_answer(monkeypatch):
    turns._greeting_audio.clear()
    monkeypatch.setattr(turns, "_synthesize", AsyncMock(return_value=b"audio"))
    greeted = [e async for e in await turns.start_greeting(turns.GreetingTurn())]
    call_id = greeted[-1]["conversation_id"]
    monkeypatch.setattr(
        provider, "complete", make_acompletion_stream("I'm Alex's AI representative. Hi again.")
    )
    events = await collect(TextTurn("hello", conversation_id=call_id))
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Hi again."
    history = agent_runtime._conversations[call_id]
    assert [m["role"] for m in history] == ["assistant", "user", "assistant"]
