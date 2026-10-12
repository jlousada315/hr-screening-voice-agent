"""The owner file: the one place a fork edits to make the rep its own."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hireme_agent import owner as owner_module
from hireme_agent import runtime

EXAMPLE = Path(__file__).resolve().parents[2] / "config/example.md"
MINIMAL = "# Sam Lee Park\n\n## Links\n- [Site](https://sam.dev)\n"


def test_shipped_example_loads_and_validates() -> None:
    owner = owner_module.load_owner(EXAMPLE)
    assert owner.short_name == "Alex" and owner.initials == "AR"
    assert owner.pronouns.possessive == "their"
    assert owner.contact.label == "LinkedIn"
    assert owner.highlights == ("LinkedIn", "GitHub", "Example paper")
    assert [link.topic for link in owner.links] == ["profile", "code", "papers", "articles"]
    assert owner.notes["Open to"] == "Senior backend or platform roles, remote or hybrid in Europe."


def test_suite_runs_against_the_example_owner() -> None:
    assert owner_module.OWNER.source == EXAMPLE
    assert runtime._DISCLOSURE == "I'm Alex's AI representative."


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "owner.md"
    path.write_text(text, encoding="utf-8")
    return path


def _example_with(old: str, new: str) -> str:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert old in text
    return text.replace(old, new, 1)


def test_a_name_and_one_link_are_enough(tmp_path: Path) -> None:
    owner = owner_module.load_owner(_write(tmp_path, MINIMAL))
    assert (owner.short_name, owner.initials, owner.tagline) == ("Sam", "SL", "AI screening call")
    assert owner.pronouns == owner_module.Pronouns("they", "them", "their")
    assert owner.contact_link == "Site" and owner.highlights == ("Site",)
    assert owner.links[0].supports == "their Site" and owner.links[0].topic == "articles"
    assert owner.roles == () and owner.notes == {} and owner.booking_url is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Pronouns: she/her", ("she", "her", "her")),
        ("pronouns: He/Him", ("he", "him", "his")),
        ("Pronouns: xe/xem/xyr", ("xe", "xem", "xyr")),
    ],
)
def test_pronoun_shorthand(tmp_path: Path, line: str, expected: tuple[str, str, str]) -> None:
    owner = owner_module.load_owner(_write(tmp_path, MINIMAL.replace("\n", f"\n{line}\n", 1)))
    assert (owner.pronouns.subject, owner.pronouns.object, owner.pronouns.possessive) == expected


@pytest.mark.parametrize(
    ("label", "url", "topic"),
    [
        ("Profile", "https://www.linkedin.com/in/sam", "profile"),
        ("Models", "https://huggingface.co/sam", "code"),
        ("A paper", "https://doi.org/10.1/x", "papers"),
        ("MSc thesis slides", "https://sam.github.io/talk", "papers"),
        ("Talk deck", "https://sam.dev/deck.PDF", "papers"),
        ("Blog", "https://sam.dev/blog", "articles"),
    ],
)
def test_link_topics_are_inferred(label: str, url: str, topic: str) -> None:
    assert owner_module._topic(label, url) == topic


def test_header_lines_override_the_defaults(tmp_path: Path) -> None:
    header = "Short name: Sammy\nTagline: Hiring call\nBooking: https://calendly.com/sam/intro\n"
    owner = owner_module.load_owner(_write(tmp_path, MINIMAL.replace("\n", "\n" + header, 1)))
    assert (owner.short_name, owner.tagline) == ("Sammy", "Hiring call")
    assert owner.booking_url == "https://calendly.com/sam/intro"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("# Alex Rivera", "Alex Rivera"),
        ("(https://github.com/example)", "(http://github.com/example)"),
        ("(https://github.com/example)", "(https://www.linkedin.com/in/example)"),
        ("[GitHub]", "[LinkedIn]"),
        ("- [GitHub](https://github.com/example)", "- GitHub: https://github.com/example"),
        ("Pronouns: they/them", "Pronouns: it"),
        ("Pronouns: they/them", "Pronouns: they/them\nBooking: http://cal.com/example/30min"),
        ("## Links", "## Elsewhere"),
    ],
)
def test_invalid_owner_files_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(RuntimeError):
        owner_module.load_owner(_write(tmp_path, _example_with(old, new)))


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing or unreadable"):
        owner_module.load_owner(tmp_path / "absent.md")
    with pytest.raises(RuntimeError, match="## Links"):
        owner_module.load_owner(_write(tmp_path, "# Sam\n"))


def test_comments_and_indented_continuations(tmp_path: Path) -> None:
    text = (
        "<!-- # Not the name -->\n# Sam Lee\n\n## Career\n- Engineer, Acme (2020–now): Built\n"
        "  the thing.\n\n## Links\n- [GitHub](https://github.com/sam) — code\n"
        "\n## About\nSee [my talk](https://youtube.com/watch?v=1).\n"
    )
    owner = owner_module.load_owner(_write(tmp_path, text))
    assert owner.name == "Sam Lee"
    assert owner.roles == (owner_module.Role("Engineer, Acme (2020–now): Built the thing."),)
    assert owner.links[0].supports == "code" and owner.links[0].topic == "code"
    assert owner.note_urls == {"https://youtube.com/watch?v=1"}


def test_resolution_prefers_env_then_owner_md_then_example(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(owner_module, "_REPO_ROOT", tmp_path / "repo")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OWNER_FILE")
    with pytest.raises(RuntimeError):
        owner_module.owner_path()
    (tmp_path / "config").mkdir()
    (tmp_path / "config/example.md").write_text("", encoding="utf-8")
    assert owner_module.owner_path() == tmp_path / "config/example.md"
    (tmp_path / "config/owner.md").write_text("", encoding="utf-8")
    assert owner_module.owner_path() == tmp_path / "config/owner.md"
    monkeypatch.setenv("OWNER_FILE", "/srv/me.md")
    assert owner_module.owner_path() == Path("/srv/me.md")


def test_check_prints_what_was_read_or_the_error(tmp_path: Path, capsys) -> None:
    assert owner_module.main([str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "Alex Rivera (Alex, they/them/their)" in out and "[papers] Example paper" in out
    assert owner_module.main([str(_write(tmp_path, "no heading"))]) == 1
    assert "# Alex Rivera" in capsys.readouterr().err


def test_prompt_has_owner_fields_filled_in() -> None:
    prompt = runtime._compose_prompt()
    assert not re.search(r"\{[a-z_]+\}", prompt)
    assert "point to their [LinkedIn](https://www.linkedin.com/in/example)" in prompt


def test_tool_descriptions_name_the_owner() -> None:
    assert runtime._EXPERIENCE_DESCRIPTION.startswith("Alex's career timeline")
    assert "their public links" in runtime._ONLINE_PRESENCE_DESCRIPTION
    assert "Alex's verified booking link" in runtime._BOOKING_DESCRIPTION
    assert runtime._NOTES_DESCRIPTION.startswith("Alex's own notes, in their words, on: Open to.")
