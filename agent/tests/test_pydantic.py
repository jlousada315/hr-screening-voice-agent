import asyncio
from dataclasses import replace

import pytest
from hireme_agent import observability as obs
from hireme_agent import runtime
from pydantic_ai import Agent
from pydantic_ai.messages import SystemPromptPart, ToolReturnPart
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel


def observation():
    return obs.TurnObservation("conversation", "person", "session", "text", runtime.PROMPT_VERSION)


async def test_one_request_and_grounding_on_followup(monkeypatch):
    requests = []

    async def stream(messages, info):
        requests.append(messages)
        assert any(isinstance(p, SystemPromptPart) for m in messages for p in m.parts)
        yield "Hello."

    agent = Agent(FunctionModel(stream_function=stream), system_prompt=runtime._SYSTEM_PROMPT)
    monkeypatch.setattr(runtime, "_create_agent", lambda _: agent)
    monkeypatch.setattr(runtime, "_load_system_prompt", lambda *a: pytest.fail("per-turn loading"))
    for question in ["first", "followup"]:
        turn = runtime.start_text(question, "conversation", observation())
        assert [t async for t in turn] == ["Hello."]
        turn.commit()
        await turn.aclose()
    assert len(requests) == 2


async def test_native_tool_stream_continues_and_captures_each_request(monkeypatch):
    captured = []
    monkeypatch.setattr(obs, "capture", lambda *args: captured.append(args))
    requests = 0
    tools = 0

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "The answer."
        else:
            yield {0: DeltaToolCall(name="lookup", json_args='{"timezone":"Europe/Lisbon"}')}

    agent = Agent(FunctionModel(stream_function=stream), system_prompt=runtime._SYSTEM_PROMPT)

    @agent.tool_plain
    def lookup(timezone: str) -> str:
        nonlocal tools
        tools += 1
        return "available"

    monkeypatch.setattr(runtime, "_create_agent", lambda _: agent)
    watched = observation()
    turn = runtime.start_text("schedule", "conversation", watched)
    # First answers after a tool call are prefixed in code when the model omits the disclosure.
    assert "".join([t async for t in turn]) == "I'm Alex's AI representative. The answer."
    generations = [e for e in captured if e[0] == "$ai_generation"]
    assert requests == 2 and tools == 1 and len(generations) == 2
    for _, person, props, _ in generations:
        assert person == "person"
        assert props["$ai_trace_id"] == watched.trace_id
        assert props["$ai_parent_id"] == watched.turn_span_id
        assert props["$session_id"] == "session"
        assert props["$ai_input_tokens"] >= 0
        assert props["$ai_latency"] >= 0


async def test_reasoning_is_never_delivered(monkeypatch):
    async def stream(messages, info):
        yield {0: DeltaThinkingPart(content="private reasoning")}
        yield "Public answer."

    monkeypatch.setattr(
        runtime, "_create_agent", lambda _: Agent(FunctionModel(stream_function=stream))
    )
    turn = runtime.start_text("question", "conversation", observation())
    assert "".join([t async for t in turn]) == "Public answer."


async def test_framework_test_model_and_best_effort_capture(monkeypatch):
    monkeypatch.setattr(obs, "capture", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(
        runtime, "_create_agent", lambda _: Agent(TestModel(custom_output_text="Grounded reply."))
    )
    turn = runtime.start_text("question", "conversation", observation())
    assert "".join([t async for t in turn]) == "Grounded reply."
    turn.commit()


async def test_close_awaits_provider_cleanup_without_reading_later_tokens(monkeypatch):
    cleaned = asyncio.Event()
    later = False

    async def stream(messages, info):
        nonlocal later
        try:
            yield "First."
            later = True
            yield "Second."
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(
        runtime, "_create_agent", lambda _: Agent(FunctionModel(stream_function=stream))
    )
    turn = runtime.start_text("question", "conversation", observation())
    assert await anext(turn) == "First."
    assert not later
    await turn.aclose()
    assert cleaned.is_set()
    assert not runtime._conversations


@pytest.mark.parametrize(
    ("text", "first", "expected"),
    [
        ("Hello there.", True, "I'm Alex's AI representative. Hello there."),
        ("I'm Alex's AI representative. Hi.", True, "I'm Alex's AI representative. Hi."),
        ("I’m Alex's AI representative. Hi.", True, "I’m Alex's AI representative. Hi."),
        ("I'm Alex's AI representative. He leads ML.", False, "He leads ML."),
        ("  I’m Alex's AI representative. He leads ML.", False, "He leads ML."),
        ("He leads ML.", False, "He leads ML."),
        (
            "As I'm Alex's AI representative, I can help.",
            False,
            "As I'm Alex's AI representative, I can help.",
        ),
    ],
)
def test_disclosure_is_prefixed_on_first_answer_and_stripped_afterwards(text, first, expected):
    assert runtime._apply_disclosure(text, first) == expected


async def test_repeated_disclosure_is_stripped_from_a_later_streamed_answer(monkeypatch):
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "Hi."
        else:
            yield "I'm Alex's AI "
            yield "representative. He works at Example Corp."

    monkeypatch.setattr(
        runtime, "_create_agent", lambda _: Agent(FunctionModel(stream_function=stream))
    )
    first = runtime.start_text("hello", "conversation", observation())
    assert "".join([t async for t in first]) == "Hi."
    first.commit()
    second = runtime.start_text("where does he work?", "conversation", observation())
    assert "".join([t async for t in second]) == "He works at Example Corp."
    second.commit()
    assert runtime._conversations["conversation"][-1]["content"] == "He works at Example Corp."


async def test_invented_links_are_removed_and_counted(monkeypatch):
    async def stream(messages, info):
        yield "I'm Alex's AI representative. See their [LinkedIn](https://www.linkedin.com/in/"
        yield "alexrivera) or [GitHub](https://github.com/example)."

    monkeypatch.setattr(
        runtime, "_create_agent", lambda _: Agent(FunctionModel(stream_function=stream))
    )
    turn_observation = observation()
    turn = runtime.start_text("links?", "conversation", turn_observation)
    assert "".join([t async for t in turn]) == (
        "I'm Alex's AI representative. See their LinkedIn or [GitHub](https://github.com/example)."
    )
    assert turn_observation.unverified_links == 1
    await turn.aclose()


async def test_notes_tool_quotes_the_owner_and_its_links_pass_the_guard(monkeypatch):
    talk = "https://example.com/talk"
    notes = {"About": f"Gave [a talk]({talk}) on payments."}
    monkeypatch.setattr(runtime, "OWNER", replace(runtime.OWNER, notes=notes))
    monkeypatch.setattr(runtime, "render_notes", lambda: notes["About"])

    async def stream(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            yield f"I'm Alex's AI representative. They gave [a talk]({talk})."
        else:
            yield {0: DeltaToolCall(name="get_notes", json_args="{}")}

    agent = Agent(FunctionModel(stream_function=stream), deps_type=obs.TurnObservation)
    agent.tool(runtime.get_notes)
    monkeypatch.setattr(runtime, "_create_agent", lambda _: agent)
    turn_observation = observation()
    turn = runtime.start_text("any talks?", "conversation", turn_observation)
    assert "".join([t async for t in turn]) == (
        f"I'm Alex's AI representative. They gave [a talk]({talk})."
    )
    assert turn_observation.unverified_links == 0
    await turn.aclose()


async def test_model_only_sees_the_latest_messages_and_system_prompt(monkeypatch):
    seen = []

    async def stream(messages, info):
        seen.append(messages)
        yield "ok."

    monkeypatch.setattr(
        runtime, "_create_agent", lambda _: Agent(FunctionModel(stream_function=stream))
    )
    for index in range(4):
        turn = runtime.start_text(f"question {index}", "conversation", observation())
        assert [t async for t in turn] == ["ok."]
        turn.commit()
        await turn.aclose()

    # Stored: 8 messages. Sent: latest 3 (assistant, user, assistant) plus the new question.
    turn = runtime.start_text("question 4", "conversation", observation())
    assert [t async for t in turn] == ["ok."]
    messages = seen[-1]
    users = [p.content for m in messages for p in m.parts if p.part_kind == "user-prompt"]
    assert users == ["question 3", "question 4"]
    assert any(isinstance(p, SystemPromptPart) for m in messages for p in m.parts)
    assert len(runtime._conversations["conversation"]) == 8


async def test_each_generation_records_the_request_it_actually_sent(monkeypatch):
    captured = []
    monkeypatch.setattr(obs, "capture", lambda *args: captured.append(args))
    runtime._conversations.clear()
    runtime._conversations["conversation"] = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]

    async def stream(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "The answer."
        else:
            yield {0: DeltaToolCall(name="lookup", json_args='{"timezone":"Europe/Lisbon"}')}

    agent = Agent(FunctionModel(stream_function=stream), system_prompt=runtime._SYSTEM_PROMPT)

    @agent.tool_plain
    def lookup(timezone: str) -> str:
        return "available at noon"

    monkeypatch.setattr(runtime, "_create_agent", lambda _: agent)
    turn = runtime.start_text("when is he free", "conversation", observation())
    [t async for t in turn]
    generations = [e[2] for e in captured if e[0] == "$ai_generation"]
    first, second = [g["$ai_input"] for g in generations]
    expected_effort = runtime._reasoning_effort(runtime.MODEL)
    assert all(g.get("$ai_effort") == expected_effort for g in generations)
    assert [m["role"] for m in first] == ["system", "user", "assistant", "user"]
    assert first[-1] == {"role": "user", "content": "when is he free"}
    # The second request replays the same context plus the tool call and its result.
    assert second[: len(first)] == first
    assert second[len(first)]["role"] == "assistant"
    assert second[len(first)]["content"].startswith("tool call: lookup(")
    assert second[-1] == {"role": "tool", "content": "available at noon"}
    runtime._conversations.clear()
