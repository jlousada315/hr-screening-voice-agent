"""The owner's supplied profile, served to the model by tools instead of living in the prompt."""

from __future__ import annotations

from typing import Literal

from ..owner import OWNER, Link, Role

ROLES: tuple[Role, ...] = OWNER.roles
LINKS: tuple[Link, ...] = OWNER.links


def render_experience() -> str:
    lines = [f"- {role.text}" for role in ROLES]
    return (
        f"{OWNER.short_name}-supplied career timeline, most recent first; "
        "not independently verified.\n" + "\n".join(lines)
    )


LinkTopic = Literal["highlights", "profile", "code", "papers", "articles", "all"]


def render_links(topic: LinkTopic = "all") -> str:
    """The links for one kind of question, worded for the caption and for speech.

    A narrow topic gets a one-line answer; `all` is the full list. Built from the data above
    so the wording cannot drift or attribute a link to the wrong place.
    """
    if topic == "all":
        return render_online_presence()
    if topic == "highlights":
        # The best evidence in one short answer, as chosen in the owner file.
        links = [link for link in LINKS if link.label in OWNER.highlights]
    else:
        links = [link for link in LINKS if link.topic == topic]
    if not links:
        contact = OWNER.contact
        return f"I don't have any {topic} links; try [{contact.label}]({contact.url})."
    if len(links) == 1:
        link = links[0]
        return f"Here it is: [{link.label}]({link.url}), {link.supports}."
    lines = [f"- [{link.label}]({link.url}): {link.supports}" for link in links]
    return "Here is what I have:\n" + "\n".join(lines)


def render_notes() -> str:
    """The owner's other sections, verbatim and attributed to them."""
    sections = [f"{heading}:\n{body}" for heading, body in OWNER.notes.items()]
    return f"{OWNER.short_name}'s own notes, in {OWNER.pronouns.possessive} words:\n\n" + (
        "\n\n".join(sections)
    )


def render_online_presence() -> str:
    lines = [f"- [{link.label}]({link.url}): {link.supports}" for link in LINKS]
    return "Public links, with what each one supports:\n" + "\n".join(lines)
