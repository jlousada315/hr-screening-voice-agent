"""The person the rep represents, read from one loose Markdown profile anyone can write.

    # Alex Rivera
    Pronouns: they/them
    Booking: https://cal.com/example/30min

    ## Experience
    - Senior Software Engineer, Example Corp (Jan 2023–present): Leads the payments team.

    ## Links
    - [GitHub](https://github.com/example): their public code

    ## Open to
    Anything else, under any heading, is kept verbatim as the owner's own notes.

Only the name and one link are required; everything else has a sensible default.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

LINK_TOPICS = ("profile", "code", "papers", "articles")
# The repository root in a source checkout (agent/src/hireme_agent/owner.py → repo).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXPERIENCE_HEADINGS = {"experience", "career", "roles", "work", "work experience"}
_LINK_HEADINGS = {"links", "online", "online presence"}
_HEADER_KEYS = {"pronouns", "booking", "short name", "tagline", "initials"}
_COMMON_PRONOUNS = {"he": ("he", "him", "his"), "she": ("she", "her", "her"),
                    "they": ("they", "them", "their")}
# Which question a link answers, guessed from its host; anything unknown is writing.
_TOPIC_HOSTS = {
    "profile": ("linkedin.com",),
    "code": ("github.com", "gitlab.com", "huggingface.co", "kaggle.com"),
    "papers": ("arxiv.org", "doi.org", "scholar.google.", "dl.acm.org", "ieeexplore.ieee.org",
               "openreview.net", "semanticscholar.org", "researchgate.net"),
}
_LINK = re.compile(r"\[([^\]]+)\]\((\S+?)\)")
_LINK_BULLET = re.compile(r"^\[([^\]]+)\]\((\S+?)\)\s*(?:[:—–-]\s*(.*))?$")
_URL = re.compile(r"https?://[^\s)>\]]+")
_RESEARCH = re.compile(r"\b(?:thesis|paper|publication|preprint)s?\b", re.IGNORECASE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class Pronouns:
    subject: str  # he / she / they
    object: str  # him / her / them
    possessive: str  # his / her / their


@dataclass(frozen=True)
class Role:
    text: str  # one role as the owner wrote it: title, company, dates and what they did


@dataclass(frozen=True)
class Link:
    label: str
    url: str
    supports: str
    topic: str  # which question this answers: profile, code, papers or articles


@dataclass(frozen=True)
class Owner:
    name: str
    short_name: str
    pronouns: Pronouns
    initials: str
    tagline: str
    contact_link: str
    highlights: tuple[str, ...]
    booking_url: str | None
    roles: tuple[Role, ...]
    links: tuple[Link, ...]
    source: Path
    notes: dict[str, str] = field(default_factory=dict)  # any other section, verbatim

    @property
    def contact(self) -> Link:
        return next(link for link in self.links if link.label == self.contact_link)

    @property
    def note_urls(self) -> set[str]:
        return {url for body in self.notes.values() for url in _URL.findall(body)}


def owner_path() -> Path:
    """`$OWNER_FILE`, else `config/owner.md`, else the shipped `config/example.md`."""
    configured = os.environ.get("OWNER_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    roots = dict.fromkeys((Path.cwd(), _REPO_ROOT))
    for root in roots:
        if (root / "config/owner.md").is_file():
            return root / "config/owner.md"
    for root in roots:
        if (root / "config/example.md").is_file():
            logger.warning(
                "No config/owner.md found; the rep is speaking for the example persona in %s. "
                "Copy it to config/owner.md and fill in your own details.",
                root / "config/example.md",
            )
            return root / "config/example.md"
    raise RuntimeError("owner file not found: create config/owner.md or set OWNER_FILE")


def _topic(label: str, url: str) -> str:
    host = urlsplit(url).netloc.lower()
    for topic, hosts in _TOPIC_HOSTS.items():
        if any(known in host for known in hosts):
            return topic
    # Self-hosted research: a thesis, a paper or a PDF on a personal site.
    if _RESEARCH.search(label) or urlsplit(url).path.lower().endswith(".pdf"):
        return "papers"
    return "articles"


def _pronouns(value: str, path: Path) -> Pronouns:
    words = [word.strip().lower() for word in value.split("/") if word.strip()]
    if len(words) >= 3:
        return Pronouns(*words[:3])
    if words and words[0] in _COMMON_PRONOUNS:
        return Pronouns(*_COMMON_PRONOUNS[words[0]])
    raise RuntimeError(
        f"{path}: write Pronouns as he/him, she/her or they/them, "
        "or give all three like xe/xem/xyr"
    )


def _sections(text: str, path: Path) -> tuple[str, dict[str, str], dict[str, list[str]]]:
    """Split the file into its # name, the Key: value lines under it, and ## sections."""
    name = ""
    header: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in _COMMENT.sub("", text).splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif line.startswith("# ") and not name:
            name = line[2:].strip()
        elif current is not None:
            current.append(line)
        elif ":" in line and name:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key in _HEADER_KEYS:
                header[key] = value.strip()
            elif line.strip():
                logger.warning("%s: ignoring unknown line under the name: %s", path, line.strip())
    if not name:
        raise RuntimeError(f"{path}: start the file with your name as a heading: `# Alex Rivera`")
    return name, header, sections


def _bullets(lines: list[str]) -> list[str]:
    """Each `-` or `*` bullet, with indented continuation lines folded into it."""
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped[:2] in ("- ", "* "):
            items.append(stripped[2:].strip())
        elif stripped and items and line[:1].isspace():
            items[-1] += " " + stripped
    return [item for item in items if item]


def load_owner(path: Path | None = None) -> Owner:
    """Read the owner's Markdown profile, deriving every detail they left out."""
    path = path or owner_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"owner file is missing or unreadable: {path}") from exc
    name, header, sections = _sections(text, path)
    short_name = header.get("short name") or name.split()[0]
    pronouns = _pronouns(header["pronouns"], path) if "pronouns" in header else Pronouns(
        *_COMMON_PRONOUNS["they"]
    )

    roles: tuple[Role, ...] = ()
    links: list[Link] = []
    notes: dict[str, str] = {}
    for heading, lines in sections.items():
        kind = heading.lower()
        if kind in _EXPERIENCE_HEADINGS:
            roles += tuple(Role(item) for item in _bullets(lines))
        elif kind in _LINK_HEADINGS:
            for item in _bullets(lines):
                match = _LINK_BULLET.match(item)
                if not match:
                    raise RuntimeError(
                        f"{path}: write each link as `- [Label](https://…): what it shows`, "
                        f"not {item!r}"
                    )
                label, url, supports = match.groups()
                links.append(Link(label.strip(), url, (supports or "").strip().rstrip(".")
                                  or f"{pronouns.possessive} {label.strip()}", _topic(label, url)))
        elif body := "\n".join(lines).strip():
            notes[heading] = body

    if not links:
        raise RuntimeError(
            f"{path}: add a `## Links` section with at least one `- [Label](https://…)` bullet"
        )
    labels = [link.label for link in links]
    urls = [link.url for link in links]
    if len(set(labels)) != len(labels) or len(set(urls)) != len(urls):
        raise RuntimeError(f"{path}: each link needs its own label and url")
    for link in links:
        if urlsplit(link.url).scheme != "https" or not urlsplit(link.url).netloc:
            raise RuntimeError(f"{path}: the {link.label} link must start with https://")

    booking_url = header.get("booking") or None
    if booking_url is not None and urlsplit(booking_url).scheme != "https":
        raise RuntimeError(f"{path}: the Booking link must start with https://")

    profile = next((link for link in links if link.topic == "profile"), links[0])
    initials = header.get("initials") or "".join(word[0] for word in name.split()[:2]).upper()
    return Owner(
        name=name,
        short_name=short_name,
        pronouns=pronouns,
        initials=initials,
        tagline=header.get("tagline") or "AI screening call",
        contact_link=profile.label,
        highlights=tuple(labels[:3]),
        booking_url=booking_url,
        roles=roles,
        links=tuple(links),
        source=path,
        notes=notes,
    )


def describe(owner: Owner) -> str:
    """What the rep understood from the file, for checking it by eye."""
    p = owner.pronouns
    lines = [
        f"{owner.name} ({owner.short_name}, {p.subject}/{p.object}/{p.possessive})",
        f"Booking link: {owner.booking_url or 'none'}",
        f"Experience ({len(owner.roles)}):",
        *(f"  - {role.text}" for role in owner.roles),
        f"Links ({len(owner.links)}), contact via {owner.contact_link}:",
        *(f"  - [{link.topic}] {link.label}: {link.url} ({link.supports})" for link in owner.links),
        f"Notes: {', '.join(owner.notes) or 'none'}",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """`scripts/check_owner.py [path]` checks an owner file and prints what it read."""
    try:
        owner = load_owner(Path(argv[0]) if argv else None)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(describe(owner))
    return 0


if __name__ == "__main__":
    # Run as a script, a broken file is reported by main instead of failing the import below.
    raise SystemExit(main(sys.argv[1:]))

OWNER = load_owner()
