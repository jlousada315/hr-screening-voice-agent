"""Booking-link scheduling and the other output-function replies."""

import pytest
from hireme_agent import observability, runtime
from hireme_agent.observability import TurnObservation
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

URL = "https://cal.com/owner/intro"


async def test_booking_link_output_function_ends_run_in_one_model_request(monkeypatch):
    monkeypatch.setattr(runtime, "_BOOKING_LINK", URL)
    monkeypatch.setattr(runtime, "_agent", None)
    requests = 0

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        assert [t.name for t in info.function_tools] == ["get_experience", "get_notes"]
        assert [t.name for t in info.output_tools] == ["get_online_presence", "show_booking_link"]
        yield {0: DeltaToolCall(name="show_booking_link", json_args="{}")}

    monkeypatch.setattr(runtime, "_create_model", lambda: FunctionModel(stream_function=stream))
    observation = TurnObservation("test", None, None, "voice", "version")
    turn = runtime.start_text("Can I schedule a meeting with him?", "test", observation)
    reply = "".join([t async for t in turn])
    assert URL in reply and "[Alex's calendar]" in reply
    assert requests == 1
    turn.commit()
    assert runtime._conversations["test"][-1]["content"] == reply
    await turn.aclose()


async def test_disclosure_not_duplicated_when_model_already_includes_it(monkeypatch):
    monkeypatch.setattr(runtime, "_BOOKING_LINK", None)
    monkeypatch.setattr(runtime, "_agent", None)

    async def stream(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "I\u2019m Alex\u2019s AI representative. He worked at Example Corp."
        else:
            yield {0: DeltaToolCall(name="get_experience", json_args="{}")}

    monkeypatch.setattr(runtime, "_create_model", lambda: FunctionModel(stream_function=stream))
    turn = runtime.start_text(
        "experience?", "test", TurnObservation("test", None, None, "text", "v")
    )
    reply = "".join([t async for t in turn])
    assert reply.count("AI representative") == 1
    await turn.aclose()


async def test_disclosure_not_added_on_later_answers(monkeypatch):
    monkeypatch.setattr(runtime, "_BOOKING_LINK", None)
    monkeypatch.setattr(runtime, "_agent", None)
    runtime._conversations["test"] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "I'm Alex's AI representative. Hello."},
    ]

    async def stream(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "He worked at Example Corp."
        else:
            yield {0: DeltaToolCall(name="get_experience", json_args="{}")}

    monkeypatch.setattr(runtime, "_create_model", lambda: FunctionModel(stream_function=stream))
    turn = runtime.start_text(
        "experience?", "test", TurnObservation("test", None, None, "text", "v")
    )
    assert "".join([t async for t in turn]) == "He worked at Example Corp."
    await turn.aclose()


async def test_text_reply_still_streams_when_scheduling_is_enabled(monkeypatch):
    monkeypatch.setattr(runtime, "_BOOKING_LINK", URL)
    monkeypatch.setattr(runtime, "_agent", None)

    async def stream(messages, info):
        yield "Happy "
        yield "to help."

    monkeypatch.setattr(runtime, "_create_model", lambda: FunctionModel(stream_function=stream))
    observation = TurnObservation("test", None, None, "text", "version")
    turn = runtime.start_text("Hello", "test", observation)
    assert [t async for t in turn] == ["Happy ", "to help."]
    await turn.aclose()


async def test_scheduling_tool_absent_when_disabled(monkeypatch):
    monkeypatch.setattr(runtime, "_BOOKING_LINK", None)
    monkeypatch.setattr(runtime, "_agent", None)
    seen = {}

    async def stream(messages, info):
        seen["output_tools"] = [t.name for t in info.output_tools]
        yield "ok"

    monkeypatch.setattr(runtime, "_create_model", lambda: FunctionModel(stream_function=stream))
    turn = runtime.start_text("hi", "test", TurnObservation("test", None, None, "text", "v"))
    [t async for t in turn]
    assert seen["output_tools"] == ["get_online_presence"]
    await turn.aclose()


async def test_experience_tool_returns_its_text_in_one_extra_request(monkeypatch):
    monkeypatch.setattr(runtime, "_BOOKING_LINK", None)
    monkeypatch.setattr(runtime, "_agent", None)
    requests = 0

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returned:
            assert "Sample Labs" in returned[0].content
            yield "Done."
        else:
            yield {0: DeltaToolCall(name="get_experience", json_args="{}")}

    monkeypatch.setattr(runtime, "_create_model", lambda: FunctionModel(stream_function=stream))
    observation = TurnObservation("test", None, None, "text", "v")
    turn = runtime.start_text("Tell me more", "test", observation)
    assert "".join([t async for t in turn]) == "I'm Alex's AI representative. Done."
    assert requests == 2
    await turn.aclose()


@pytest.mark.parametrize(
    ("args", "present", "absent"),
    [
        (
            '{"topic": "highlights"}',
            ["linkedin", "github.com/example)", "0000.00000"],
            ["example.com/blog"],
        ),
        ('{"topic": "papers"}', ["arxiv.org/abs/0000.00000"], ["linkedin"]),
        ('{"topic": "articles"}', ["example.com/blog"], ["arxiv", "github.com"]),
        ('{"topic": "code"}', ["github.com/example"], ["arxiv", "linkedin"]),
        ('{"topic": "profile"}', ["linkedin.com"], ["arxiv", "github.com"]),
        ("{}", ["linkedin", "arxiv", "example.com", "github.com"], []),  # no topic means everything
    ],
)
async def test_online_presence_replies_for_one_topic_in_a_single_request(
    monkeypatch, args, present, absent
):
    monkeypatch.setattr(runtime, "_BOOKING_LINK", None)
    monkeypatch.setattr(runtime, "_agent", None)
    requests = 0

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        yield {0: DeltaToolCall(name="get_online_presence", json_args=args)}

    monkeypatch.setattr(runtime, "_create_model", lambda: FunctionModel(stream_function=stream))
    observation = TurnObservation("test", None, None, "voice", "version")
    runtime._conversations.pop("test", None)
    turn = runtime.start_text("Any papers?", "test", observation)
    reply = "".join([t async for t in turn])
    assert requests == 1
    assert reply.startswith("I'm Alex's AI representative. ")  # first answer is still disclosed
    for needle in present:
        assert needle in reply
    for needle in absent:
        assert needle not in reply.lower()
    await turn.aclose()


async def test_tool_call_generations_record_time_to_first_token(monkeypatch):
    monkeypatch.setattr(runtime, "_BOOKING_LINK", None)
    monkeypatch.setattr(runtime, "_agent", None)
    captured = []
    monkeypatch.setattr(
        observability, "capture", lambda event, person, props, ts: captured.append((event, props))
    )

    async def stream(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "He worked at Example Corp."
        else:
            yield {0: DeltaToolCall(name="get_experience", json_args="{}")}

    monkeypatch.setattr(runtime, "_create_model", lambda: FunctionModel(stream_function=stream))
    turn = runtime.start_text(
        "experience?", "test", TurnObservation("test", None, None, "voice", "v")
    )
    assert "".join([t async for t in turn])
    await turn.aclose()
    generations = [props for event, props in captured if event == "$ai_generation"]
    assert len(generations) == 2  # the tool call, then the spoken answer
    for props in generations:
        assert 0 <= props["$ai_time_to_first_token"] <= props["$ai_latency"]
