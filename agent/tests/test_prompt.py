"""Loading and behavioral contracts for the slim role-and-behavior system prompt."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from hireme_agent import owner as owner_module
from hireme_agent import runtime as agent_runtime
from hireme_agent.tools import profile

PROMPT_TOKEN_BUDGET = 320  # ≈ characters / 4; career facts live in tools, not here


@pytest.mark.parametrize(
    "content", ["", "   \n", "Answer with {{BOOKING_URL}}", "Status: TBD", "Use ⟨link⟩"]
)
def test_prompt_loader_rejects_empty_or_unresolved_content(tmp_path: Path, content: str) -> None:
    prompt_path = tmp_path / "system-prompt.md"
    prompt_path.write_text(content, encoding="utf-8")
    with pytest.raises(RuntimeError):
        agent_runtime._load_system_prompt(prompt_path)


def test_prompt_loader_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        agent_runtime._load_system_prompt(tmp_path / "missing.md")


def test_prompt_stays_slim_and_holds_no_career_facts() -> None:
    prompt = agent_runtime._compose_prompt()
    assert len(prompt) / 4 <= PROMPT_TOKEN_BUDGET
    for company in ("Example Corp", "Sample Labs", "State University"):
        assert company not in prompt
    for link in profile.LINKS:
        # The contact link is the only one the prompt carries, so it is never invented.
        assert link.url not in prompt.replace(owner_module.OWNER.contact.url, "")


def test_prompt_states_role_and_behavior_contracts() -> None:
    prompt = agent_runtime._compose_prompt()
    for contract in (
        "AI representative",
        "an AI, not Alex",
        "third person",
        "[label](url)",
        "one to three short",
        "in English",
        "never invent or embellish",
        "get_experience",
        "get_online_presence",
        "lead with their current role and one outcome",
        "Alex says",
        "LinkedIn",
        "cannot take notes",
        "even if the caller uses other pronouns",
        "only for facts your tools returned",
        "never estimate compensation",
        "[LinkedIn](https://www.linkedin.com/in/example)",
    ):
        assert contract in prompt, contract


def test_disclosure_rule_is_in_prompt_and_code_backstop_matches() -> None:
    assert agent_runtime._DISCLOSURE in agent_runtime._compose_prompt().replace("“", "").replace(
        "”", ""
    )


def test_notes_line_names_the_sections_when_the_owner_has_notes() -> None:
    assert agent_runtime._SYSTEM_PROMPT.endswith(
        "Call get_notes for their own notes on Open to; repeat them, never extrapolate."
    )
    assert len(agent_runtime._SYSTEM_PROMPT) / 4 <= PROMPT_TOKEN_BUDGET


def test_scheduling_line_only_when_enabled() -> None:
    assert "show_booking_link" not in agent_runtime._compose_prompt()
    assert agent_runtime._SCHEDULING_LINE.startswith("To schedule a call")


def test_prompt_version_covers_prompt_and_tool_results() -> None:
    material = "\n\n".join(
        [
            agent_runtime._SYSTEM_PROMPT,
            profile.render_experience(),
            profile.render_online_presence(),
            profile.render_notes(),
        ]
    )
    assert agent_runtime.PROMPT_VERSION == hashlib.sha256(material.encode()).hexdigest()[:12]


def test_required_resources_are_installed() -> None:
    from importlib.resources import files

    for path in agent_runtime._AUTHORED_PATHS:
        assert files("hireme_agent").joinpath(path).is_file()
