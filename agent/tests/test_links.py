"""The link guard: only owner-file URLs reach the caller, however the stream is split."""

from __future__ import annotations

import pytest
from hireme_agent.links import LinkGuard

ALLOWED = {"https://www.linkedin.com/in/example", "https://github.com/example"}


def _stream(*chunks: str) -> tuple[str, int]:
    guard = LinkGuard(ALLOWED)
    text = "".join(guard.feed(chunk) for chunk in chunks) + guard.flush()
    return text, guard.dropped


@pytest.mark.parametrize(
    ("chunks", "expected", "dropped"),
    [
        # An invented profile URL keeps its label and loses the link.
        (["See his [Linked", "In](https://www.linkedin.com/in/al", "exrivera)."],
         "See his LinkedIn.", 1),
        # A known link passes intact even split mid-URL; a trailing slash still matches.
        (["See [LinkedIn](https://www.linkedin.com/in/ex", "ample/) now"],
         "See [LinkedIn](https://www.linkedin.com/in/example/) now", 0),
        # Bare URLs: known kept with its punctuation, unknown dropped.
        (["Code at https://github.com/exa", "mple. Also https://evil.com/x ok"],
         "Code at https://github.com/example. Also  ok", 1),
        (["Visit https://github.com/example"], "Visit https://github.com/example", 0),
        (["Visit https://evil.com/a"], "Visit ", 1),
        # Brackets and words that only look like links are ordinary text.
        (["he has the [right] skills and http stuff, half [open"],
         "he has the [right] skills and http stuff, half [open", 0),
        (["hello there, ", "how are you"], "hello there, how are you", 0),
    ],
)
def test_guard(chunks: list[str], expected: str, dropped: int) -> None:
    assert _stream(*chunks) == (expected, dropped)


def test_plain_text_is_not_held_back() -> None:
    guard = LinkGuard(ALLOWED)
    assert guard.feed("Alex leads the payments team. ") == "Alex leads the payments team. "
