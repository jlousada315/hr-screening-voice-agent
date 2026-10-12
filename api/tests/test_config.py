"""Offline checks for Groq-only configuration and non-fatal startup key warnings."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from hireme_agent import runtime as agent_runtime

import app as app_module


def test_default_model(monkeypatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert agent_runtime._configured_model() == "groq/openai/gpt-oss-120b"


def test_configured_groq_model(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "groq/llama-3.3-70b-versatile")
    assert agent_runtime._configured_model() == "groq/llama-3.3-70b-versatile"


@pytest.mark.parametrize(
    "model",
    ["anthropic/claude-sonnet-4-6", "gpt-4o-mini", "mistral/mistral-large", "", "Groq/model"],
)
def test_rejects_non_groq_models(monkeypatch, model) -> None:
    monkeypatch.setenv("LLM_MODEL", model)
    with pytest.raises(ValueError, match="LLM_MODEL must use the groq/ prefix"):
        agent_runtime._configured_model()


def test_check_provider_key_warns_when_missing(monkeypatch, caplog) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="hireme"):
        app_module._check_provider_key()
    assert any("GROQ_API_KEY" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_check_provider_key_info_when_present(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
    with caplog.at_level(logging.INFO, logger="hireme"):
        app_module._check_provider_key()
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_missing_key_does_not_prevent_startup(monkeypatch, caplog) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="hireme"):
        with TestClient(app_module.app) as client:
            health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "model": agent_runtime.MODEL,
        "provider": "groq",
        "provider_key": "GROQ_API_KEY",
        "provider_key_set": False,
        "calendar_enabled": agent_runtime.calendar_enabled(),
    }
    assert "GROQ_API_KEY" in caplog.text


def test_check_calendar_config_logs_disabled_state(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="hireme"):
        app_module._check_calendar_config()
    assert any("Booking link" in r.message for r in caplog.records)


def test_model_source_log_names_the_process_environment(monkeypatch, caplog) -> None:
    monkeypatch.setattr(app_module._bootstrap, "_FROM_PROCESS_ENV", True)
    monkeypatch.setattr(app_module._bootstrap, "_DOTENV_MODEL", None)
    monkeypatch.setenv("LLM_MODEL", agent_runtime.MODEL)
    with caplog.at_level(logging.INFO, logger="hireme"):
        app_module._check_model_source()
    assert any("from process environment" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_model_warns_when_process_environment_shadows_dotenv(monkeypatch, caplog) -> None:
    monkeypatch.setattr(app_module._bootstrap, "_FROM_PROCESS_ENV", True)
    monkeypatch.setattr(app_module._bootstrap, "_DOTENV_MODEL", "groq/openai/gpt-oss-120b")
    monkeypatch.setenv("LLM_MODEL", "groq/qwen/qwen3.8-27b")
    with caplog.at_level(logging.WARNING, logger="hireme"):
        app_module._check_model_source()
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "overrides the .env value" in warning.message
    assert "gpt-oss-120b" in warning.message


def test_model_source_defaults_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert app_module._bootstrap.model_source() == "code default"
