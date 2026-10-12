"""Keep the model from inventing links: only URLs from the owner file reach the caller."""

from __future__ import annotations

import re

_MARKDOWN_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\s]*)\)")
_PARTIAL_MARKDOWN = re.compile(r"\[[^\]\n]*(\](\([^)\s]*)?)?")
_BARE_URL = re.compile(r"https?://\S+")
_TRAILING = ".,;:!?)"
_HOLD_LIMIT = 400  # a "link" this long without closing is just text


def _normalize(url: str) -> str:
    return url.rstrip("/")


class LinkGuard:
    """Streams text through, holding back a link until it is complete.

    A Markdown link to an unknown URL keeps its label and loses the URL; an unknown bare
    URL is dropped. `dropped` counts both, for telemetry.
    """

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = {_normalize(url) for url in allowed}
        self.pending = ""
        self.dropped = 0

    def _known(self, url: str) -> bool:
        return _normalize(url) in self.allowed

    def _markdown(self, label: str, url: str) -> str:
        if self._known(url):
            return f"[{label}]({url})"
        self.dropped += 1
        return label

    def _bare(self, url: str) -> str:
        core = url.rstrip(_TRAILING)
        tail = url[len(core) :]
        if self._known(core):
            return url
        self.dropped += 1
        return tail

    def _url_start(self, i: int) -> bool:
        return self.pending[i] == "h" and (i == 0 or self.pending[i - 1] in " \n\t(")

    def feed(self, delta: str, final: bool = False) -> str:
        self.pending += delta
        out: list[str] = []
        i = 0
        text = self.pending
        while i < len(text):
            char = text[i]
            if char == "[":
                if match := _MARKDOWN_LINK.match(text, i):
                    out.append(self._markdown(*match.groups()))
                    i = match.end()
                    continue
                partial = _PARTIAL_MARKDOWN.match(text, i)
                if not final and partial.end() == len(text) and len(text) - i < _HOLD_LIMIT:
                    break  # the link may still be streaming
            elif self._url_start(i):
                match = _BARE_URL.match(text, i)
                ended = match is not None and match.end() < len(text)
                if match and (ended or final):
                    out.append(self._bare(match.group()))
                    i = match.end()
                    continue
                rest = text[i:]
                still_streaming = (match is not None) or "https://".startswith(rest[:8])
                if not final and still_streaming and len(rest) < _HOLD_LIMIT:
                    break
            out.append(char)
            i += 1
        self.pending = text[i:]
        return "".join(out)

    def flush(self) -> str:
        return self.feed("", final=True)
