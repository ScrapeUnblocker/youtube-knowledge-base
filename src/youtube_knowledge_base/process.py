"""What a raw scrape does not give you.

* which uploads are new since the last run, and worth saving (length, words, age);
* the creator's chapters, read from the description;
* the captions folded into readable paragraphs, each with a link to that moment;
* where in the video your terms are said, with timestamp links;
* a one-paragraph summary from the description, skipping sponsor blurbs.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .collect import ChannelInfo, Segment, Transcript, Video
from .config import Channel, Config, Rules
from .state import State

_TIMESTAMP = re.compile(
    r"^\s*(?:[-*\N{BULLET}]\s*)?"  # an optional list bullet
    r"[(\[]?((?:\d{1,2}:)?\d{1,2}:\d{2})[)\]]?"  # 0:00, 1:02:03, (12:34) or [12:34]
    r"\s*(?:[-\N{EN DASH}\N{EM DASH}:|]\s*)?(\S.*)$"  # a separator, then the title
)
_SENTENCE_END = re.compile(r"[.!?\N{HORIZONTAL ELLIPSIS}][\"')\]]*(?=\s|$)")
_URL = re.compile(r"https?://|www\.", re.I)
MENTION_GAP = 30.0
MAX_HITS = 20


@dataclass(frozen=True)
class Chapter:
    start: int
    title: str


@dataclass(frozen=True)
class Paragraph:
    start: float
    text: str
    chapter: int | None = None


@dataclass(frozen=True)
class Mention:
    term: str
    times: tuple[float, ...]
    snippets: tuple[str, ...]
    in_title: bool = False

    @property
    def count(self) -> int:
        return len(self.times)


@dataclass(frozen=True)
class Note:
    """Everything a destination needs to save one video."""

    video: Video
    transcript: Transcript | None
    chapters: tuple[Chapter, ...]
    paragraphs: tuple[Paragraph, ...]
    mentions: tuple[Mention, ...]
    summary: str
    tags: tuple[str, ...]

    @property
    def mentioned(self) -> bool:
        return any(m.times or m.in_title for m in self.mentions)


@dataclass
class Candidate:
    video: Video
    channel: Channel
    retry: tuple[str, ...] = ()


@dataclass
class Plan:
    """What this run does: save ``todo``, remember ``baseline`` without saving."""

    todo: list[Candidate] = field(default_factory=list)
    baseline: list[Video] = field(default_factory=list)
    deferred: int = 0
    first_channels: list[str] = field(default_factory=list)


def skip_reason(video: Video, rules: Rules, now: datetime) -> str | None:
    """Why a video is not saved, or ``None`` when it should be."""
    if not video.seconds:
        return "live or not out yet"
    if video.published and video.published > now + timedelta(minutes=5):
        return "not out yet"
    minutes = video.seconds / 60
    if minutes < rules.min_minutes:
        return f"shorter than {rules.min_minutes:g} min"
    if rules.max_minutes and minutes > rules.max_minutes:
        return f"longer than {rules.max_minutes:g} min"
    if rules.max_age_days and video.published:
        if now - video.published > timedelta(days=rules.max_age_days):
            return f"older than {rules.max_age_days} days"
    title = video.title.lower()
    for word in rules.exclude:
        if word.lower() in title:
            return f"title has {word!r}"
    if rules.include:
        text = f"{video.title}\n{video.description}".lower()
        if not any(word.lower() in text for word in rules.include):
            return "none of the include words"
    return None


def plan(
    results: Sequence[tuple[Channel, ChannelInfo, Sequence[Video]]],
    state: State,
    config: Config,
    now: datetime,
) -> Plan:
    """Pick the videos to save this run.

    A channel seen for the first time saves only its ``first_run`` newest matching
    uploads; the rest are remembered so they are never sent. Videos that were waiting for
    captions, or that a destination failed to take, come back first. The total is capped
    by ``max_new_per_run``; the overflow waits for the next run.
    """
    out = Plan()
    queued: set[str] = set()
    channels = {c.ref: c for c in config.channels}

    for entry in state.retries():
        video = Video.from_dict(entry["video"])
        cfg = channels.get(entry.get("ref", "")) or _match(results, video.channel.id)
        if cfg is None:
            continue  # the channel was taken out of config.yml
        out.todo.append(Candidate(video, cfg, tuple(entry.get("todo") or ())))
        queued.add(video.id)

    for cfg, info, videos in results:
        fresh = [
            v
            for v in videos
            if v.id not in queued and not state.seen(v.id) and not skip_reason(v, cfg.rules, now)
        ]
        fresh.sort(key=lambda v: v.published or now, reverse=True)
        if not state.knows_channel(info.id):
            out.first_channels.append(info.name)
            keep, rest = fresh[: config.first_run], fresh[config.first_run :]
            out.baseline.extend(rest)
            # Uploads that do not match are remembered too, so loosening a rule later does
            # not flood the vault with the back catalogue. Premieres and live streams are
            # left alone: they are new once they are out.
            out.baseline.extend(
                v
                for v in videos
                if v not in fresh and v.id not in queued and not state.seen(v.id) and _out(v, now)
            )
            fresh = keep
        for video in reversed(fresh):
            out.todo.append(Candidate(video, cfg))
            queued.add(video.id)

    if len(out.todo) > config.max_new_per_run:
        out.deferred = len(out.todo) - config.max_new_per_run
        out.todo = out.todo[: config.max_new_per_run]
    return out


def _out(video: Video, now: datetime) -> bool:
    if not video.seconds:
        return False
    return not (video.published and video.published > now + timedelta(minutes=5))


def _match(
    results: Iterable[tuple[Channel, ChannelInfo, Sequence[Video]]], cid: str
) -> Channel | None:
    for cfg, info, _ in results:
        if info.id == cid:
            return cfg
    return None


def chapters(description: str, seconds: int | None) -> tuple[Chapter, ...]:
    """The creator's chapters from the description, by YouTube's own rule: the first
    starts at 0:00, there are at least three, and they run in order."""
    found: list[Chapter] = []
    for line in description.splitlines():
        m = _TIMESTAMP.match(line)
        if not m:
            continue
        start = _seconds(m.group(1))
        title = m.group(2).strip(" -\N{EN DASH}\N{EM DASH}|:")
        if not title:
            continue
        if not found:
            if start != 0:
                continue
        elif start <= found[-1].start:
            if start == 0:
                found = []
            else:
                continue
        if seconds and start >= seconds:
            break
        found.append(Chapter(start, title[:120]))
    return tuple(found) if len(found) >= 3 else ()


def paragraphs(
    transcript: Transcript, chapters: Sequence[Chapter], target: int
) -> tuple[Paragraph, ...]:
    """Fold caption lines into paragraphs of about ``target`` seconds, breaking at the end
    of a sentence and always at a chapter start."""
    out: list[Paragraph] = []
    bounds = [c.start for c in chapters]
    words: list[str] = []
    start = 0.0
    chapter: int | None = 0 if chapters else None

    def flush() -> None:
        if words:
            out.append(Paragraph(start, " ".join(words), chapter))
            words.clear()

    for seg in transcript.segments:
        if chapters:
            index = _chapter_at(bounds, seg.start)
            if index != chapter:
                flush()
                chapter = index
        if not words:
            start = seg.start
        elapsed = seg.start + seg.dur - start
        if elapsed >= target:
            # Caption lines rarely end with the sentence, so break inside the line, after
            # its last full stop; the rest opens the next paragraph.
            ends = list(_SENTENCE_END.finditer(seg.text))
            if ends:
                cut = ends[-1].end()
                words.append(seg.text[:cut])
                flush()
                rest = seg.text[cut:].strip()
                if rest:
                    start = seg.start
                    words.append(rest)
                continue
        words.append(seg.text)
        if elapsed >= 2 * target:
            flush()
    flush()
    return tuple(out)


def _chapter_at(bounds: Sequence[int], at: float) -> int:
    index = 0
    for i, start in enumerate(bounds):
        if at + 0.5 >= start:
            index = i
    return index


def mentions(title: str, segments: Sequence[Segment], terms: Sequence[str]) -> tuple[Mention, ...]:
    """Where each term is said: whole words, any case, across caption-line breaks. Hits
    closer than ``MENTION_GAP`` seconds count once."""
    if not terms:
        return ()
    text, offsets = _joined(segments)
    out = []
    for term in terms:
        words = r"\s+".join(map(re.escape, term.split()))
        pattern = re.compile(r"(?<!\w)" + words + r"(?!\w)", re.I)
        times: list[float] = []
        snippets: list[str] = []
        for m in pattern.finditer(text):
            at = _time_at(offsets, m.start())
            if times and at - times[-1] < MENTION_GAP:
                continue
            times.append(at)
            snippets.append(_snippet(text, m.start(), m.end()))
            if len(times) >= MAX_HITS:
                break
        in_title = bool(pattern.search(title))
        if times or in_title:
            out.append(Mention(term, tuple(times), tuple(snippets), in_title))
    return tuple(out)


def _joined(segments: Sequence[Segment]) -> tuple[str, list[tuple[int, float]]]:
    parts: list[str] = []
    offsets: list[tuple[int, float]] = []
    pos = 0
    for seg in segments:
        offsets.append((pos, seg.start))
        parts.append(seg.text)
        pos += len(seg.text) + 1
    return " ".join(parts), offsets


def _time_at(offsets: Sequence[tuple[int, float]], pos: int) -> float:
    at = 0.0
    for start, when in offsets:
        if start > pos:
            break
        at = when
    return at


def _snippet(text: str, start: int, end: int, width: int = 70) -> str:
    left = text.rfind(" ", 0, max(start - width, 0)) + 1 if start > width else 0
    right = text.find(" ", min(end + width, len(text)))
    right = len(text) if right < 0 else right
    out = text[left:right].strip()
    return (
        ("\N{HORIZONTAL ELLIPSIS}" if left else "")
        + out
        + ("\N{HORIZONTAL ELLIPSIS}" if right < len(text) else "")
    )


def summary(description: str, limit: int = 300) -> str:
    """The first paragraph of the description that reads like one: no links, no
    timestamps, at least a sentence long. Sponsor blurbs usually carry a link."""
    for block in re.split(r"\n\s*\n", description):
        lines: list[str] = []
        for line in (ln.strip() for ln in block.splitlines()):
            if _URL.search(line) or _TIMESTAMP.match(line):
                break
            if line:
                lines.append(line)
        text = " ".join(lines)
        if len(text) >= 60 and not text.startswith(("#", "*")):
            return _cut(text, limit)
    return ""


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "\N{HORIZONTAL ELLIPSIS}"


def build_note(
    video: Video, transcript: Transcript | None, channel: Channel, config: Config
) -> Note:
    found = chapters(video.description, video.seconds)
    paras = paragraphs(transcript, found, config.paragraph_seconds) if transcript else ()
    hits = mentions(video.title, transcript.segments if transcript else (), config.mentions)
    tags = ("youtube", tag(video.channel.name), *(tag(t) for t in channel.tags))
    tags += tuple(tag(m.term) for m in hits)
    return Note(
        video=video,
        transcript=transcript,
        chapters=found,
        paragraphs=paras,
        mentions=hits,
        summary=summary(video.description),
        tags=tuple(dict.fromkeys(t for t in tags if t)),
    )


def passages(note: Note) -> list[dict[str, Any]]:
    """The transcript as search passages, one per paragraph, for a vector database. Ids
    are stable (video id + start), so saving a video again overwrites, never duplicates."""
    v = note.video
    out = []
    for p in note.paragraphs:
        chapter = note.chapters[p.chapter].title if note.chapters and p.chapter is not None else ""
        heading = f"{v.title} ({chapter})" if chapter else v.title
        out.append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{v.url}&t={int(p.start)}s")),
                "text": f"{heading}\n{p.text}",
                "payload": {
                    "video_id": v.id,
                    "title": v.title,
                    "channel": v.channel.name,
                    "channel_id": v.channel.id,
                    "published": v.published.date().isoformat() if v.published else None,
                    "start": int(p.start),
                    "url": v.at(p.start),
                    "chapter": chapter,
                    "text": p.text,
                    "language": note.transcript.language if note.transcript else None,
                },
            }
        )
    return out


def tag(text: str) -> str:
    """An Obsidian-safe tag: lowercase, letters, digits, dashes."""
    out = re.sub(r"[^\w/-]+", "-", text.strip().lower()).strip("-_/")
    return "" if out.isdigit() else out


def clock(seconds: float) -> str:
    """``754`` -> ``12:34``; ``3725`` -> ``1:02:05``."""
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _seconds(stamp: str) -> int:
    total = 0
    for part in stamp.split(":"):
        total = total * 60 + int(part)
    return total
