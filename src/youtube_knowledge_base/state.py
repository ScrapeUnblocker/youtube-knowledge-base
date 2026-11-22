"""What has already been saved, so a video is never delivered twice.

The state is one small JSON file, ``state/videos.json`` by default. On GitHub Actions the
scheduled workflow commits it back to your repository together with the notes, so it
survives between runs. Each video id is in it once:

* ``saved``    every destination took it (or gave up after ``MAX_TRIES``);
* ``baseline`` seen on a channel's first run and deliberately not saved;
* ``waiting``  no captions yet; tried again next run, with the video kept here;
* ``retry``    a destination failed; only that destination is tried again next run.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .collect import ChannelInfo, Video

VERSION = 1
MAX_TRIES = 3
PENDING = ("waiting", "retry")


class State:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self.channels: dict[str, dict[str, Any]] = dict(data.get("channels") or {})
        self.videos: dict[str, dict[str, Any]] = dict(data.get("videos") or {})

    @classmethod
    def load(cls, path: str | Path) -> State:
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise ValueError(f"{p}: not valid JSON ({exc}); fix or delete it") from None
        if not isinstance(data, dict):
            raise ValueError(f"{p}: expected a JSON object")
        return cls(data)

    def save(self, path: str | Path) -> None:
        """Write atomically, so a cancelled run never leaves half a file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": VERSION, "channels": self.channels, "videos": self.videos}
        text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".videos-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # channels

    def knows_channel(self, channel_id: str) -> bool:
        return channel_id in self.channels

    def add_channel(self, info: ChannelInfo, ref: str, now: datetime) -> None:
        entry = self.channels.setdefault(info.id, {"since": _day(now)})
        entry.update(name=info.name, ref=ref)

    # videos

    def seen(self, video_id: str) -> bool:
        return video_id in self.videos

    def status(self, video_id: str) -> str | None:
        return (self.videos.get(video_id) or {}).get("status")

    def tries(self, video_id: str) -> int:
        return int((self.videos.get(video_id) or {}).get("tries", 0))

    def first_seen(self, video_id: str) -> datetime | None:
        seen = (self.videos.get(video_id) or {}).get("seen")
        return datetime.fromisoformat(seen) if seen else None

    def retries(self) -> list[dict[str, Any]]:
        """Videos waiting for captions or for a destination, oldest first."""
        out = [e for e in self.videos.values() if e.get("status") in PENDING and e.get("video")]
        return sorted(out, key=lambda e: e.get("seen", ""))

    def remember(self, videos: Iterable[Video], ref_of: dict[str, str], now: datetime) -> None:
        """Baseline: seen on a first run, never to be sent."""
        for video in videos:
            if video.id not in self.videos:
                self.videos[video.id] = self._entry(video, ref_of.get(video.channel.id, ""), now)
                self.videos[video.id]["status"] = "baseline"

    def waiting(self, video: Video, ref: str, now: datetime) -> None:
        entry = self.videos.get(video.id) or self._entry(video, ref, now)
        entry.update(status="waiting", tries=int(entry.get("tries", 0)) + 1, video=video.to_dict())
        entry["checked"] = _stamp(now)
        self.videos[video.id] = entry

    def saved(
        self,
        video: Video,
        ref: str,
        now: datetime,
        failed: Iterable[str] = (),
        transcript: bool = True,
    ) -> str:
        """Record a delivery. Destinations in ``failed`` are tried again on the next run,
        up to ``MAX_TRIES`` runs in all. Returns the new status."""
        entry = self.videos.get(video.id) or self._entry(video, ref, now)
        failed = sorted(set(failed))
        tries = int(entry.get("tries", 0)) + 1 if failed else int(entry.get("tries", 0))
        entry["checked"] = _stamp(now)
        entry["transcript"] = transcript
        if failed and tries < MAX_TRIES:
            entry.update(status="retry", todo=failed, tries=tries, video=video.to_dict())
        else:
            entry["status"] = "saved"
            entry["saved"] = _stamp(now)
            entry.pop("video", None)
            entry.pop("todo", None)
            if failed:
                entry["gave_up"] = failed
        self.videos[video.id] = entry
        return entry["status"]

    def prune(self, now: datetime, keep_days: int | None) -> int:
        """Forget saved and baseline videos first seen more than ``keep_days`` ago. They
        are older than every channel's ``max_age_days`` by then, so they cannot come back.
        ``None`` keeps everything (a channel without an age limit)."""
        if keep_days is None:
            return 0
        cutoff = _stamp(now - timedelta(days=keep_days))
        old = [
            vid
            for vid, e in self.videos.items()
            if e.get("status") in ("saved", "baseline") and e.get("seen", "") < cutoff
        ]
        for vid in old:
            del self.videos[vid]
        return len(old)

    def _entry(self, video: Video, ref: str, now: datetime) -> dict[str, Any]:
        return {
            "channel": video.channel.id,
            "ref": ref,
            "title": video.title[:200],
            "published": _day(video.published) if video.published else None,
            "seen": _stamp(now),
        }


def _stamp(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat()


def _day(when: datetime) -> str:
    return when.date().isoformat()
