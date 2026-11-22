"""How a saved video looks: the Markdown note and the short chat ping."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .process import Note, clock

DOT = " \N{MIDDLE DOT} "
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')


@dataclass(frozen=True)
class Saved:
    """Where a destination put a note, for the ping: ``Notion: <link>``."""

    label: str
    text: str
    url: str = ""


# the note


def file_name(note: Note) -> str:
    """``2026-11-20 Title of the video.md``: dated so a channel folder sorts by upload."""
    when = note.video.published
    day = when.date().isoformat() if when else "undated"
    return f"{day} {safe_name(note.video.title, 90)}.md"


def folder_name(note: Note) -> str:
    return safe_name(note.video.channel.name or note.video.channel.id, 60)


def safe_name(text: str, limit: int) -> str:
    """A file or folder name that works on every OS and inside Obsidian links."""
    name = _UNSAFE_NAME.sub(" ", text)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > limit:
        name = name[:limit].rsplit(" ", 1)[0].rstrip(" .,;-")
    return name or "untitled"


def markdown(note: Note, saved_on: datetime) -> str:
    """The Obsidian note: properties, the embedded video, mentions, chapters, the
    transcript with a timestamp link per paragraph, and the description folded away."""
    v = note.video
    tr = note.transcript
    props: list[tuple[str, object]] = [
        ("title", v.title),
        ("channel", v.channel.name),
        ("channel_url", v.channel.url),
        ("url", v.url),
        ("video_id", v.id),
        ("published", v.published.date().isoformat() if v.published else None),
        ("duration", clock(v.seconds) if v.seconds else None),
        ("views", v.views),
        ("captions", caption_label(note) if tr else "none"),
        ("chapters", len(note.chapters)),
        ("mentions", [m.term for m in note.mentions]),
        ("tags", list(note.tags)),
        ("saved", saved_on.date().isoformat()),
    ]
    lines = ["---"]
    for key, value in props:
        if value is None:
            continue
        lines.append(f"{key}: {_yaml(value)}")
    lines += ["---", "", f"# {_md(v.title)}", "", f"![]({v.url})", ""]
    meta = [f"[{_md(v.channel.name)}]({v.channel.url})"]
    if v.published:
        meta.append(v.published.date().isoformat())
    if v.seconds:
        meta.append(clock(v.seconds))
    meta.append(f"[Watch on YouTube]({v.url})")
    lines += [DOT.join(meta), ""]
    if note.summary:
        lines += [f"> {_md(note.summary)}", ""]

    if note.mentions:
        lines += ["## Mentions", ""]
        for m in note.mentions:
            if not m.times:
                lines.append(f"- **{_md(m.term)}**: in the title")
                continue
            lines.append(f"- **{_md(m.term)}** ({m.count}x)")
            for at, snippet in zip(m.times, m.snippets, strict=True):
                lines.append(f"  - [{clock(at)}]({v.at(at)}) {_md(snippet)}")
        lines.append("")

    if note.chapters:
        lines += ["## Chapters", ""]
        lines += [f"- [{clock(c.start)}]({v.at(c.start)}) {_md(c.title)}" for c in note.chapters]
        lines.append("")

    lines += ["## Transcript", ""]
    if tr is None:
        lines += ["*This video has no captions, so there is no transcript.*", ""]
    else:
        lines += [f"*Captions: {_md(caption_label(note))}.*", ""]
        current: int | None = None
        for p in note.paragraphs:
            if note.chapters and p.chapter is not None and p.chapter != current:
                current = p.chapter
                lines += [f"### {_md(note.chapters[current].title)}", ""]
            lines += [f"[{clock(p.start)}]({v.at(p.start)}) {_md(p.text)}", ""]

    if v.description.strip():
        lines += ["> [!abstract]- Description"]
        lines += [f"> {_md(line)}".rstrip() for line in v.description.strip().splitlines()]
        lines.append("")
    return "\n".join(lines)


def caption_label(note: Note) -> str:
    """``English (auto-generated)`` or ``English``, as YouTube names the track."""
    tr = note.transcript
    if tr is None:
        return ""
    label = tr.label or tr.language or "unknown language"
    if tr.auto and "auto" not in label.lower():
        label += " (auto-generated)"
    return label


def _yaml(value: object) -> str:
    """A YAML value. JSON strings and lists are valid YAML, and they quote safely."""
    if isinstance(value, bool | int | float):
        return json.dumps(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _md(text: str) -> str:
    """Keep caption text from turning into markup: no HTML, no wikilinks, no comments."""
    return text.replace("<", "&lt;").replace("[[", "[\\[").replace("%%", "%\\%").replace("\n", " ")


# the chat ping


def headline(note: Note) -> str:
    return f"New from {note.video.channel.name}: {note.video.title}"


def details(note: Note) -> str:
    v = note.video
    parts = []
    if v.seconds:
        parts.append(clock(v.seconds))
    if v.published:
        parts.append(v.published.strftime("%d %b %Y"))
    if note.chapters:
        parts.append(f"{len(note.chapters)} chapters")
    if note.transcript is None:
        parts.append("no captions")
    return DOT.join(parts)


def mention_lines(note: Note, limit: int = 3) -> list[tuple[str, list[tuple[str, str]]]]:
    """``[(term, [(clock, url), ...])]`` with the first ``limit`` moments per term."""
    out = []
    for m in note.mentions:
        moments = [(clock(t), note.video.at(t)) for t in m.times[:limit]]
        out.append((m.term, moments))
    return out


def plain(note: Note, saved: Sequence[Saved]) -> str:
    lines = [headline(note), note.video.url, details(note)]
    if note.summary:
        lines += ["", note.summary]
    terms = mention_lines(note)
    if terms:
        lines.append("")
        for term, moments in terms:
            lines.append(f"Mentions {term}: " + (", ".join(c for c, _ in moments) or "title"))
    if saved:
        lines += ["", "Saved to " + "; ".join(_saved_plain(s) for s in saved)]
    return "\n".join(lines)


def _saved_plain(s: Saved) -> str:
    return f"{s.label} ({s.url or s.text})" if (s.url or s.text) else s.label


def slack(note: Note, saved: Sequence[Saved]) -> str:
    v = note.video
    lines = [f"*<{v.url}|{_slack(v.title)}>*", f"{_slack(v.channel.name)}{DOT}{details(note)}"]
    if note.summary:
        lines += ["", _slack(note.summary)]
    for term, moments in mention_lines(note):
        links = ", ".join(f"<{url}|{c}>" for c, url in moments) or "in the title"
        lines.append(f"Mentions *{_slack(term)}*: {links}")
    if saved:
        lines.append("Saved to " + DOT.join(_saved_slack(s) for s in saved))
    return "\n".join(lines)


def _saved_slack(s: Saved) -> str:
    if s.url:
        return f"<{s.url}|{_slack(s.label)}>"
    return f"{_slack(s.label)} ({_slack(s.text)})" if s.text else _slack(s.label)


def discord(note: Note, saved: Sequence[Saved]) -> str:
    """The embed description (the title, link, channel and thumbnail sit around it)."""
    lines = [details(note)]
    if note.summary:
        lines += ["", _discord(note.summary)]
    for term, moments in mention_lines(note):
        links = ", ".join(f"[{c}]({url})" for c, url in moments) or "in the title"
        lines.append(f"Mentions **{_discord(term)}**: {links}")
    if saved:
        lines += ["", "Saved to " + DOT.join(_saved_discord(s) for s in saved)]
    return "\n".join(lines)


def _saved_discord(s: Saved) -> str:
    if s.url:
        return f"[{_discord(s.label)}]({s.url})"
    return f"{_discord(s.label)} ({_discord(s.text)})" if s.text else _discord(s.label)


def telegram(note: Note, saved: Sequence[Saved]) -> str:
    v = note.video
    a = html.escape  # attribute values

    def e(text: str) -> str:  # text: Telegram knows &lt; &gt; &amp; and numeric entities
        return html.escape(text, quote=False)

    lines = [
        f'<b>{e(v.channel.name)}</b>: <a href="{a(v.url)}">{e(v.title)}</a>',
        e(details(note)),
    ]
    if note.summary:
        lines += ["", e(note.summary)]
    for term, moments in mention_lines(note):
        links = ", ".join(f'<a href="{a(url)}">{c}</a>' for c, url in moments) or "in the title"
        lines.append(f"Mentions <b>{e(term)}</b>: {links}")
    if saved:
        parts = [
            f'<a href="{a(s.url)}">{e(s.label)}</a>'
            if s.url
            else (f"{e(s.label)} ({e(s.text)})" if s.text else e(s.label))
            for s in saved
        ]
        lines += ["", "Saved to " + DOT.join(parts)]
    return "\n".join(lines)


def _slack(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _discord(text: str) -> str:
    return re.sub(r"([\\*_~`|>\[\]])", r"\\\1", text)
