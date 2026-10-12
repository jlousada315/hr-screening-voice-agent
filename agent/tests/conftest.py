import pytest
from hireme_agent import runtime


@pytest.fixture(autouse=True)
def isolate_agent(monkeypatch):
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    runtime._conversations.clear()
    yield
    runtime._conversations.clear()
