"""Shared fixtures and helpers for the hireme-api test suite.

The default suite is fully offline: framework test-model streams and the ElevenLabs HTTP
calls are mocked, so nothing here needs real API keys or network access.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def make_acompletion_stream(*deltas: str | None) -> AsyncMock:
    """Build a stand-in for a provider stream.

    Returns an AsyncMock whose awaited result is an async iterator yielding chunk-like
    objects (`chunk.choices[0].delta.content`), one per delta. `None` deltas are included
    to exercise the None-guard in the production model stream.
    """

    async def _aiter():
        for d in deltas:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=d))])

    return AsyncMock(side_effect=lambda **kwargs: _aiter())


@pytest.fixture
def acompletion_stream_factory():
    """Fixture exposing the `make_acompletion_stream` factory."""
    return make_acompletion_stream


@pytest.fixture(autouse=True)
def clean_conversations():
    from hireme_agent import runtime as agent_runtime

    agent_runtime._conversations.clear()
    yield
    agent_runtime._conversations.clear()


@pytest.fixture(autouse=True)
def disable_live_analytics(monkeypatch):
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)


# Test-only adapter keeps delivery regressions on real framework streams while
# existing fault/blocked-stream spies remain easy to control.
provider = SimpleNamespace(complete=None)


@pytest.fixture(autouse=True)
def framework_model(monkeypatch, request):
    if request.node.get_closest_marker("integration"):
        return
    from contextlib import aclosing

    from hireme_agent import runtime
    from pydantic_ai import Agent
    from pydantic_ai.messages import SystemPromptPart, TextPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from tests.conftest import provider as test_provider

    def factory(observation):
        async def stream(messages, info):
            wire = []
            for message in messages:
                for part in message.parts:
                    if isinstance(part, SystemPromptPart):
                        wire.append({"role": "system", "content": part.content})
                    elif isinstance(part, UserPromptPart):
                        wire.append({"role": "user", "content": part.content})
                    elif isinstance(part, TextPart):
                        wire.append({"role": "assistant", "content": part.content})
            # Pydantic places the system prompt in the newest request when history
            # was supplied independently; normalize the spy view to its old order.
            wire.sort(key=lambda m: m["role"] != "system")
            tokens = await test_provider.complete(
                model=runtime.MODEL,
                messages=wire,
                stream=True,
                trace_id=observation.trace_id,
                metadata=observation.metadata,
            )
            async with aclosing(tokens):
                try:
                    async for chunk in tokens:
                        delta = chunk.choices[0].delta.content
                        if delta:
                            yield delta
                except Exception as exc:
                    observation.error = exc
                    raise

        return Agent(
            FunctionModel(stream_function=stream), system_prompt=runtime._SYSTEM_PROMPT, retries=0
        )

    monkeypatch.setattr(runtime, "_create_agent", factory)
