"""The privacy guard that keeps personal data out of tracked files."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_privacy", ROOT / "scripts/check_privacy.py")
check_privacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_privacy)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".privacy-denylist\n", encoding="utf-8")
    license_text = "Copyright Jane Doe <jane@corp.io>\n"  # privacy: allow
    (tmp_path / "LICENSE").write_text(license_text, encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "Book at https://cal.com/your-username/30min or mail noreply@corp.io.\n"
        "Write to someone@example.com.\n"
        "Created by Jane Doe. <!-- privacy: allow -->\n",
        encoding="utf-8",
    )
    return tmp_path


def _track(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)


def test_clean_repo_passes(repo: Path) -> None:
    _track(repo, "app.py", "x = 1\n")
    (repo / ".privacy-denylist").write_text("jane doe\n", encoding="utf-8")
    assert check_privacy.check(repo) == []


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("contact me at jane@corp.io\n", "email address"),  # privacy: allow
        ("see eu.posthog.com/project/12345\n", "PostHog project URL"),  # privacy: allow
        ("book https://cal.com/jane-doe/30min\n", "personal Cal.com link"),  # privacy: allow
        ("Jane Doe built this\n", "denylisted term 'jane doe'"),
    ],
)
def test_each_kind_of_personal_data_is_reported(repo: Path, text: str, problem: str) -> None:
    (repo / ".privacy-denylist").write_text("jane doe\n", encoding="utf-8")
    _track(repo, "notes.md", text)
    assert check_privacy.check(repo) == [f"notes.md:1: {problem}"]


def test_the_owner_file_may_hold_personal_details(repo: Path) -> None:
    (repo / "config").mkdir()
    (repo / ".privacy-denylist").write_text("jane doe\n", encoding="utf-8")
    details = 'name = "Jane Doe"\nemail = "jane@corp.io"\n'  # privacy: allow
    _track(repo, "config/owner.md", details)
    assert check_privacy.check(repo) == []


def test_this_repository_passes() -> None:
    assert check_privacy.check(ROOT) == []
