"""The API calls: a channel's newest uploads and a video's captions.

Both go through the official ScrapeUnblocker SDK (``pip install scrapeunblocker``), which
reads the API key from ``SCRAPEUNBLOCKER_KEY`` and retries rate limits and 5xx answers
on its own:

* ``client.youtube_channel(channel, limit=...)`` -> ``POST /content/youtube-channel``
* ``client.youtube_transcript(video, lang=...)`` -> ``POST /content/youtube-transcript``

Everything else in this module turns the JSON into small typed records.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import scrapeunblocker

WATCH_URL = "https://www.youtube.com/watch?v={id}"
_BRACKET_ONLY = re.compile(r"^\s*[\[(][^\])]{1,30}[\])]\s*$")
_SPACE = re.compile(r"\s+")


class CollectError(RuntimeError):
    """A call failed. The message names the error type, never the API key."""


class Client(Protocol):
    def youtube_channel(self, channel: str, **kwargs: Any) -> Any: ...

    def youtube_transcript(self, video: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class ChannelInfo:
    id: str
    name: str
    handle: str = ""

    @property
    def url(self) -> str:
        if self.handle:
            return f"https://www.youtube.com/{self.handle}"
        return f"https://www.youtube.com/channel/{self.id}"


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    channel: ChannelInfo
    published: datetime | None
    seconds: int | None
    description: str = ""
    views: int | None = None
    thumbnail: str = ""

    @property
    def url(self) -> str:
        return WATCH_URL.format(id=self.id)

    def at(self, seconds: float) -> str:
        """The watch URL that starts playing at ``seconds``."""
        return f"{self.url}&t={int(seconds)}s"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "channel": {
                "id": self.channel.id,
                "name": self.channel.name,
                "handle": self.channel.handle,
            },
            "published": self.published.isoformat() if self.published else None,
            "seconds": self.seconds,
            "description": self.description,
            "views": self.views,
            "thumbnail": self.thumbnail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Video:
        ch = data.get("channel") or {}
        return cls(
            id=data["id"],
            title=data.get("title") or data["id"],
            channel=ChannelInfo(ch.get("id", ""), ch.get("name", ""), ch.get("handle", "")),
            published=_when(data.get("published")),
            seconds=data.get("seconds"),
            description=data.get("description") or "",
            views=data.get("views"),
            thumbnail=data.get("thumbnail") or "",
        )


@dataclass(frozen=True)
class Segment:
    start: float
    dur: float
    text: str


@dataclass(frozen=True)
class Transcript:
    language: str
    label: str
    auto: bool
    segments: tuple[Segment, ...]


def make_client(api_key: str | None = None) -> scrapeunblocker.Client:
    """The SDK client. ``api_key=None`` lets the SDK read ``SCRAPEUNBLOCKER_KEY``."""
    return scrapeunblocker.Client(api_key=api_key, max_retries=2)


def channel_videos(client: Client, ref: str, limit: int) -> tuple[ChannelInfo, list[Video]]:
    """A channel's newest uploads (the Videos tab, newest first). One API call."""
    try:
        data = client.youtube_channel(ref, limit=limit)
    except scrapeunblocker.ScrapeUnblockerError as exc:
        raise CollectError(f"{ref}: {_describe(exc)}") from None
    if not isinstance(data, dict) or not isinstance(data.get("videos"), list):
        raise CollectError(f"{ref}: unexpected answer (no videos list)")
    raw = data.get("channel") or {}
    info = ChannelInfo(
        id=str(raw.get("channelId") or ref),
        name=str(raw.get("name") or ref).strip(),
        handle=str(raw.get("handle") or (ref if ref.startswith("@") else "")),
    )
    videos = [v for v in (shape_video(item, info) for item in data["videos"]) if v]
    return info, videos


def shape_video(item: Any, channel: ChannelInfo) -> Video | None:
    if not isinstance(item, dict) or not item.get("videoId"):
        return None
    seconds = item.get("durationSeconds")
    views = item.get("viewCount")
    return Video(
        id=str(item["videoId"]),
        title=_SPACE.sub(" ", str(item.get("title") or item["videoId"])).strip(),
        channel=channel,
        published=_when(item.get("publishedAt")),
        seconds=int(seconds) if isinstance(seconds, int | float) else None,
        description=str(item.get("description") or "").replace("\r\n", "\n"),
        views=int(views) if isinstance(views, int | float) else None,
        thumbnail=str(item.get("thumbnail") or ""),
    )


def transcript(client: Client, video_id: str, language: str | None) -> Transcript | None:
    """A video's captions, or ``None`` when it has none (yet). One API call.

    When the preferred language is missing the API returns the track the video has, so
    check ``Transcript.language``.
    """
    kwargs: dict[str, Any] = {"lang": language} if language else {}
    try:
        data = client.youtube_transcript(video_id, **kwargs)
    except scrapeunblocker.NotFoundError:
        return None
    except scrapeunblocker.ScrapeUnblockerError as exc:
        raise CollectError(f"{video_id}: {_describe(exc)}") from None
    if not isinstance(data, dict):
        raise CollectError(f"{video_id}: unexpected answer")
    segments = []
    for raw in data.get("segments") or []:
        text = clean_caption(str(raw.get("text") or ""))
        if not text:
            continue
        segments.append(
            Segment(start=float(raw.get("start") or 0), dur=float(raw.get("dur") or 0), text=text)
        )
    if not segments:
        return None
    code = str(data.get("language") or language or "")
    return Transcript(
        language=code,
        label=str(data.get("languageName") or code),
        auto=bool(data.get("isAutoGenerated")),
        segments=tuple(segments),
    )


def clean_caption(text: str) -> str:
    """One caption line as plain text: entities decoded, line breaks folded, cue-only
    lines such as ``[Music]`` dropped."""
    text = _SPACE.sub(" ", html.unescape(text)).strip()
    if _BRACKET_ONLY.match(text):
        return ""
    return text


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _describe(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    message = str(exc).splitlines()[0][:200] if str(exc) else ""
    label = type(exc).__name__ + (f" {status}" if status else "")
    return f"{label}: {message}" if message else label
