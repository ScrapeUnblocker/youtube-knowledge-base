"""Load and check ``config.yml``, the one file you edit.

Secrets (the API key, tokens, webhook URLs) never live here. They come from environment
variables, which on GitHub Actions are your repository secrets.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

NOTE_SINKS = ("obsidian", "notion", "qdrant")
CHAT_SINKS = ("slack", "discord", "telegram", "webhook")
DESTINATIONS = NOTE_SINKS + CHAT_SINKS
PING_MODES = ("all", "mentions", "off")


class ConfigError(ValueError):
    """config.yml has a value the tool cannot use."""


@dataclass(frozen=True)
class Rules:
    """Which uploads are worth saving."""

    min_minutes: float = 2
    max_minutes: float = 0
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ("#shorts",)
    max_age_days: int = 14


@dataclass(frozen=True)
class Channel:
    """One channel to follow, with its own rules (the defaults unless overridden)."""

    ref: str
    rules: Rules
    tags: tuple[str, ...] = ()
    language: str | None = None


@dataclass(frozen=True)
class Obsidian:
    folder: str = "vault/YouTube"
    repo: str = ""
    branch: str = ""


@dataclass(frozen=True)
class Notion:
    database_id: str = ""


@dataclass(frozen=True)
class Qdrant:
    collection: str = "youtube"
    model: str = "BAAI/bge-small-en-v1.5"
    path: str = ""


@dataclass(frozen=True)
class Config:
    channels: tuple[Channel, ...]
    per_channel: int = 15
    first_run: int = 2
    max_new_per_run: int = 10
    language: str | None = "en"
    paragraph_seconds: int = 60
    wait_hours: float = 24
    mentions: tuple[str, ...] = ()
    ping: str = "all"
    destinations: tuple[str, ...] = ("obsidian",)
    obsidian: Obsidian = field(default_factory=Obsidian)
    notion: Notion = field(default_factory=Notion)
    qdrant: Qdrant = field(default_factory=Qdrant)

    def calls_per_run(self) -> tuple[int, int]:
        """API calls per run: (always, at most). One per channel, plus one transcript per
        new video, capped by ``max_new_per_run``."""
        return len(self.channels), len(self.channels) + self.max_new_per_run


TOP_KEYS = ("channels", "videos", "transcript", "mentions", "ping", "destinations")
VIDEO_KEYS = (
    "per_channel",
    "min_minutes",
    "max_minutes",
    "include",
    "exclude",
    "max_age_days",
    "first_run",
    "max_new_per_run",
)
RULE_KEYS = ("min_minutes", "max_minutes", "include", "exclude", "max_age_days")
CHANNEL_KEYS = ("channel", "tags", "language", *RULE_KEYS)
TRANSCRIPT_KEYS = ("language", "paragraph_seconds", "wait_hours")
SINK_KEYS = {
    "obsidian": ("folder", "repo", "branch"),
    "notion": ("database_id",),
    "qdrant": ("collection", "model", "path"),
    "slack": (),
    "discord": (),
    "telegram": (),
    "webhook": (),
}


def load(path: str | Path) -> Config:
    """Read and check a config file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{p}: file not found") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: not valid YAML ({exc})") from None
    return parse(data or {})


def parse(data: Any) -> Config:
    """Build a :class:`Config` from the parsed YAML, with a clear message on any mistake."""
    top = _mapping(data, "config.yml")
    _known(top, TOP_KEYS, "config.yml")

    videos = _mapping(top.get("videos") or {}, "videos")
    _known(videos, VIDEO_KEYS, "videos")
    base = _rules(videos, Rules(), "videos")

    raw_channels = top.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ConfigError("channels: list at least one channel (@handle, UC... id or URL)")
    channels = tuple(_channel(item, base, i) for i, item in enumerate(raw_channels, 1))
    refs = [c.ref.lower() for c in channels]
    for ref in refs:
        if refs.count(ref) > 1:
            raise ConfigError(f"channels: {ref} is listed twice")

    transcript = _mapping(top.get("transcript") or {}, "transcript")
    _known(transcript, TRANSCRIPT_KEYS, "transcript")

    ping = str(top.get("ping", "all")).strip().lower()
    if ping not in PING_MODES:
        raise ConfigError(f"ping: use one of {', '.join(PING_MODES)} (got {ping!r})")

    dest = _mapping(top.get("destinations") or {}, "destinations")
    _known(dest, DESTINATIONS, "destinations")
    if not dest:
        raise ConfigError("destinations: turn on at least one (obsidian needs no secret)")
    settings: dict[str, Mapping[str, Any]] = {}
    for name, value in dest.items():
        opts = _mapping(value or {}, f"destinations.{name}")
        _known(opts, SINK_KEYS[name], f"destinations.{name}")
        settings[name] = opts

    obs = settings.get("obsidian", {})
    obsidian = Obsidian(
        folder=_text(obs.get("folder", Obsidian.folder), "destinations.obsidian.folder"),
        repo=_text(obs.get("repo", ""), "destinations.obsidian.repo"),
        branch=_text(obs.get("branch", ""), "destinations.obsidian.branch"),
    )
    if not obsidian.folder.strip("/ "):
        raise ConfigError("destinations.obsidian.folder: give a folder for the notes")
    if obsidian.repo and obsidian.repo.count("/") != 1:
        raise ConfigError("destinations.obsidian.repo: use owner/name, e.g. you/my-vault")
    qd = settings.get("qdrant", {})
    qdrant = Qdrant(
        collection=_text(qd.get("collection", Qdrant.collection), "destinations.qdrant.collection"),
        model=_text(qd.get("model", Qdrant.model), "destinations.qdrant.model"),
        path=_text(qd.get("path", ""), "destinations.qdrant.path"),
    )
    notion = Notion(
        database_id=_text(
            settings.get("notion", {}).get("database_id", ""), "destinations.notion.database_id"
        )
    )

    language = _text(transcript.get("language", "en"), "transcript.language") or None
    return Config(
        channels=channels,
        per_channel=_int(videos.get("per_channel", 15), "videos.per_channel", 1, 200),
        first_run=_int(videos.get("first_run", 2), "videos.first_run", 0, 200),
        max_new_per_run=_int(videos.get("max_new_per_run", 10), "videos.max_new_per_run", 1, 500),
        language=language,
        paragraph_seconds=_int(
            transcript.get("paragraph_seconds", 60), "transcript.paragraph_seconds", 15, 600
        ),
        wait_hours=_number(transcript.get("wait_hours", 24), "transcript.wait_hours", 0, 720),
        mentions=_words(top.get("mentions"), "mentions"),
        ping=ping,
        destinations=tuple(n for n in DESTINATIONS if n in settings),
        obsidian=obsidian,
        notion=notion,
        qdrant=qdrant,
    )


def _channel(item: Any, base: Rules, i: int) -> Channel:
    where = f"channels[{i}]"
    if isinstance(item, str):
        ref = item.strip()
        if not ref:
            raise ConfigError(f"{where}: empty channel")
        return Channel(ref=ref, rules=base)
    opts = _mapping(item, where)
    _known(opts, CHANNEL_KEYS, where)
    ref = _text(opts.get("channel", ""), f"{where}.channel").strip()
    if not ref:
        raise ConfigError(f"{where}: set channel: to a @handle, UC... id or URL")
    language = opts.get("language")
    return Channel(
        ref=ref,
        rules=_rules(opts, base, where),
        tags=_words(opts.get("tags"), f"{where}.tags"),
        language=_text(language, f"{where}.language") if language else None,
    )


def _rules(opts: Mapping[str, Any], base: Rules, where: str) -> Rules:
    rules = replace(
        base,
        min_minutes=_number(opts.get("min_minutes", base.min_minutes), f"{where}.min_minutes"),
        max_minutes=_number(opts.get("max_minutes", base.max_minutes), f"{where}.max_minutes"),
        max_age_days=_int(
            opts.get("max_age_days", base.max_age_days), f"{where}.max_age_days", 0, 3650
        ),
    )
    if "include" in opts:
        rules = replace(rules, include=_words(opts["include"], f"{where}.include"))
    if "exclude" in opts:
        rules = replace(rules, exclude=_words(opts["exclude"], f"{where}.exclude"))
    if rules.max_minutes and rules.max_minutes < rules.min_minutes:
        raise ConfigError(f"{where}: max_minutes is below min_minutes")
    return rules


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where}: expected a mapping (key: value lines)")
    return value


def _known(opts: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> None:
    for key in opts:
        if key not in allowed:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f" (did you mean {close[0]}?)" if close else ""
            raise ConfigError(f"{where}: unknown setting {key!r}{hint}")


def _text(value: Any, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        raise ConfigError(f"{where}: expected text")
    return str(value).strip()


def _words(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list of words")
    out = []
    for item in value:
        word = _text(item, where)
        if word and word.lower() not in (w.lower() for w in out):
            out.append(word)
    return tuple(out)


def _number(value: Any, where: str, low: float = 0, high: float = 100_000) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{where}: expected a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: expected a number (got {value!r})") from None
    if not low <= number <= high:
        raise ConfigError(f"{where}: use a number from {low:g} to {high:g}")
    return number


def _int(value: Any, where: str, low: int, high: int) -> int:
    number = _number(value, where, low, high)
    if number != int(number):
        raise ConfigError(f"{where}: expected a whole number")
    return int(number)
