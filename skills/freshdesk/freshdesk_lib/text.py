"""Plain text to the simple HTML Freshdesk bodies take, and HTML back to readable text."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BULLET = re.compile(r"^\s*[-*•]\s+")
_NUMBER = re.compile(r"^\s*\d+[.)]\s+")


def to_html(text: str) -> str:
    """Paragraphs on blank lines, line breaks inside them, and simple bulleted or numbered lists."""
    blocks = re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").strip())
    out = []
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if all(_BULLET.match(line) for line in lines):
            items = "".join(f"<li>{html.escape(_BULLET.sub('', line))}</li>" for line in lines)
            out.append(f"<ul>{items}</ul>")
        elif all(_NUMBER.match(line) for line in lines):
            items = "".join(f"<li>{html.escape(_NUMBER.sub('', line))}</li>" for line in lines)
            out.append(f"<ol>{items}</ol>")
        else:
            out.append("<p>" + "<br>".join(html.escape(line) for line in lines) + "</p>")
    return "".join(out) or "<p></p>"


class _Text(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BREAKS:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith(("http://", "https://", "mailto:")):
                self.parts.append("")
                self._href = href

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BREAKS:
            self.parts.append("\n")
        elif tag == "a" and getattr(self, "_href", None):
            self.parts.append(f" ({self._href})")
            self._href = None

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _Text()
    parser.feed(value or "")
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def normalized(value: str) -> str:
    """Whitespace-insensitive comparison form, used only to find our own send in a readback."""
    return re.sub(r"\s+", " ", html_to_text(value) if "<" in (value or "") else (value or "")).strip().casefold()
