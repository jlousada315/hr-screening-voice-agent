import pytest
from hireme_agent import prepare_conversation, runtime, start_text
from hireme_agent.observability import TurnObservation


async def test_standalone_turn_commits_only_after_explicit_success(monkeypatch):
    async def tokens(*args):
        yield "Hello."

    monkeypatch.setattr(runtime, "_model_tokens", tokens)
    runtime._conversations.clear()
    cid = prepare_conversation()
    turn = start_text("question", cid, TurnObservation(cid, None, None, "text", "test"))
    with pytest.raises(RuntimeError, match="incomplete"):
        turn.commit()
    assert [text async for text in turn] == ["Hello."]
    assert cid not in runtime._conversations
    turn.commit()
    turn.commit()
    assert len(runtime._conversations[cid]) == 2
    await turn.aclose()
    runtime._conversations.clear()


def test_groq_client_bounds_waiting_to_one_retry(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    model = runtime._create_model()
    client = model.client
    assert client.max_retries == 1
    assert client.timeout == runtime._GROQ_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("model", "env", "expected"),
    [
        ("groq/openai/gpt-oss-120b", None, "low"),
        ("groq/openai/gpt-oss-120b", "medium", "medium"),
        ("groq/openai/gpt-oss-120b", "HIGH", "high"),
        ("groq/openai/gpt-oss-120b", "none", None),
        ("groq/openai/gpt-oss-120b", "", None),
        ("groq/qwen/qwen3.8-27b", None, None),  # other models reject the parameter
    ],
)
def test_reasoning_effort_is_sent_only_to_gpt_oss(monkeypatch, model, env, expected):
    if env is None:
        monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("LLM_REASONING_EFFORT", env)
    assert runtime._reasoning_effort(model) == expected


def test_reasoning_effort_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "extreme")
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        runtime._reasoning_effort("groq/openai/gpt-oss-120b")


async def test_initialize_rejects_invalid_gpt_oss_effort(monkeypatch):
    monkeypatch.setattr(runtime, "MODEL", "groq/openai/gpt-oss-120b")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "extreme")
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        await runtime.initialize()


@pytest.mark.parametrize("effort", [None, "low", "medium", "HIGH", "none", "default", ""])
async def test_initialize_accepts_supported_gpt_oss_effort(monkeypatch, effort):
    monkeypatch.setattr(runtime, "MODEL", "groq/openai/gpt-oss-120b")
    if effort is None:
        monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("LLM_REASONING_EFFORT", effort)
    await runtime.initialize()


async def test_initialize_preserves_other_models_effort_behavior(monkeypatch):
    monkeypatch.setattr(runtime, "MODEL", "groq/qwen/qwen3.8-27b")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "extreme")
    await runtime.initialize()


def test_model_settings_pass_effort_in_the_request_body(monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    monkeypatch.setattr(runtime, "MODEL", "groq/openai/gpt-oss-120b")
    assert runtime._model_settings() == {"extra_body": {"reasoning_effort": "low"}}
    monkeypatch.setenv("LLM_REASONING_EFFORT", "none")
    assert runtime._model_settings() == {}
