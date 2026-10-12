"""Fail when code or docs carry personal data that belongs in config/owner.md.

Checks that no tracked file holds an email address, a PostHog project URL or a personal
Cal.com link, and that none contains a term from the optional, gitignored `.privacy-denylist`
(one term per line, e.g. your surname). The owner file itself, the LICENSE and any line
containing `privacy: allow` are exempt.

    uv run python scripts/check_privacy.py [repo]
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "email address": re.compile(r"[\w.+-]+@(?!example\.(?:com|org)\b)[\w-]+\.[\w.-]*[a-z]{2,}"),
    "PostHog project URL": re.compile(r"posthog\.com/project/\d+"),
    "personal Cal.com link": re.compile(
        r"cal\.com/(?!(?:<username>|username|your-username|example|owner)/)[\w-]+/[\w-]+"
    ),
}
ALLOWED = ("noreply@", "privacy: allow")
SKIPPED = {"config/owner.md", "LICENSE", "uv.lock", "web/package-lock.json"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def check(repo: Path) -> list[str]:
    problems = []
    denylist_path = repo / ".privacy-denylist"
    denylist = []
    if denylist_path.is_file():
        lines = denylist_path.read_text(encoding="utf-8").splitlines()
        denylist = [term.strip().lower() for term in lines if term.strip()]

    for name in _git(repo, "ls-files", "-z").stdout.split("\0"):
        if not name or name in SKIPPED:
            continue
        try:
            text = (repo / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable
        for number, line in enumerate(text.splitlines(), 1):
            if any(marker in line for marker in ALLOWED):
                continue
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    problems.append(f"{name}:{number}: {label}")
            lowered = line.lower()
            for term in denylist:
                if term in lowered:
                    problems.append(f"{name}:{number}: denylisted term {term!r}")
    return problems


def main() -> int:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    problems = check(repo)
    for problem in problems:
        print(problem)
    print("privacy check passed" if not problems else f"{len(problems)} privacy problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
