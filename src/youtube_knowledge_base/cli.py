"""Command line: ``youtube-knowledge-base run | search | check | test-destinations``."""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .collect import CollectError, channel_videos, make_client, transcript
from .config import Config, ConfigError, load
from .process import build_note, clock, plan
from .render import Saved
from .sinks import USER_AGENT, DeliveryError, QdrantSink, Sink, build, close
from .state import MAX_TRIES, State

Out = Callable[[str], None]


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.yml", help="config file (default: %(default)s)")
    common.add_argument(
        "--state", default="state/videos.json", help="state file (default: %(default)s)"
    )
    p = argparse.ArgumentParser(
        prog="youtube-knowledge-base",
        description="Save every new video from the YouTube channels you follow as a transcript "
        "note in Obsidian, Notion or Qdrant, with a Slack, Discord or Telegram ping.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    run = sub.add_parser("run", parents=[common], help="read the channels and save new videos")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be saved and sent; deliver nothing, change no state",
    )
    run.add_argument("--full", action="store_true", help="with --dry-run, print whole notes")
    search = sub.add_parser(
        "search", parents=[common], help="search everything saved in Qdrant by meaning"
    )
    search.add_argument("query", help='what to look for, e.g. "how do vector databases work"')
    search.add_argument("--limit", type=int, default=5, help="results (default: %(default)s)")
    search.add_argument("--channel", default="", help="only this channel (its display name)")
    sub.add_parser(
        "check", parents=[common], help="check config.yml and show destinations and cost"
    )
    sub.add_parser(
        "test-destinations",
        parents=[common],
        help="send a hello to each chat destination and check the note destinations",
    )
    return p


def run(
    args: argparse.Namespace,
    env: Mapping[str, str],
    *,
    client: Any = None,
    http: Any = None,
    now: datetime | None = None,
    out: Out = print,
    err: Out | None = None,
) -> int:
    err = err or _stderr(env)
    config = load(args.config)
    state = State.load(args.state)
    now = now or datetime.now(UTC)
    dry = bool(args.dry_run)
    if client is None:
        if not env.get("SCRAPEUNBLOCKER_KEY"):
            err("Set SCRAPEUNBLOCKER_KEY to your ScrapeUnblocker API key (see .env.example).")
            return 2
        client = make_client()
    http = http or httpx.Client(headers={"User-Agent": USER_AGENT})
    sinks, skipped = build(config, env, http, now)
    for line in skipped:
        out(f"skipped {line}")
    if not sinks:
        err("No destination is switched on. Keep obsidian in config.yml or set a secret.")
        return 2
    if dry:
        out("DRY RUN: nothing is saved or sent, and the state file is not changed.")

    calls = 0
    results = []
    failed_channels = []
    for ch in config.channels:
        calls += 1
        try:
            info, videos = channel_videos(client, ch.ref, config.per_channel)
        except CollectError as exc:
            err(f"Channel failed: {exc}")
            failed_channels.append(ch.ref)
            continue
        results.append((ch, info, videos))

    todo = plan(results, state, config, now)
    if todo.first_channels:
        out(
            f"First run for {', '.join(todo.first_channels)}: saving up to {config.first_run} "
            f"newest per channel, remembering {len(todo.baseline)} older uploads without saving."
        )
    if not dry:
        for ch, info, _ in results:
            state.add_channel(info, ch.ref, now)
        state.remember(todo.baseline, {info.id: ch.ref for ch, info, _ in results}, now)
        state.save(args.state)

    saved_count = waiting = failures = 0
    for cand in todo.todo:
        v = cand.video
        calls += 1
        problem = None
        try:
            tr = transcript(client, v.id, cand.channel.language or config.language)
        except CollectError as exc:
            tr, problem = None, exc
        if tr is None:
            since = v.published or state.first_seen(v.id) or now
            hours = (now - since).total_seconds() / 3600
            if problem:
                err(f"Captions failed: {problem}")
                keep = state.tries(v.id) + 1 < MAX_TRIES
            else:
                keep = hours < config.wait_hours
            if keep:
                waiting += 1
                out(f"waiting {v.channel.name}: {v.title} (no captions yet, next run checks again)")
                if not dry:
                    state.waiting(v, cand.channel.ref, now)
                    state.save(args.state)
                continue

        note = build_note(v, tr, cand.channel, config)
        ping = config.ping == "all" or (config.ping == "mentions" and note.mentioned)
        targets = [
            s
            for s in sinks
            if (not cand.retry or s.name in cand.retry) and (s.kind == "note" or ping)
        ]
        if dry:
            _preview(note, targets, args.full, out)
            saved_count += 1
            continue
        saved: list[Saved] = []
        failed: list[str] = []
        for sink in targets:
            try:
                where = sink.deliver(note, saved)
            except DeliveryError as exc:
                err(f"Delivery failed: {exc}")
                failed.append(sink.name)
                continue
            if where:
                saved.append(where)
        status = state.saved(v, cand.channel.ref, now, failed, transcript=tr is not None)
        state.save(args.state)
        took = [s.name for s in targets if s.name not in failed]
        line = f"saved {v.channel.name}: {v.title} -> {', '.join(took) or 'nothing'}"
        if failed:
            failures += 1
            line += f" (failed: {', '.join(failed)}; " + (
                "retried next run)" if status == "retry" else "gave up)"
            )
        out(line)
        saved_count += 1

    if not dry:
        state.prune(now, _keep_days(config))
        state.save(args.state)
    close(sinks)

    verb = "would be saved" if dry else "saved"
    summary = (
        f"Done: {len(results)} of {len(config.channels)} channels read, {saved_count} videos {verb}"
    )
    if waiting:
        summary += f", {waiting} waiting for captions"
    if todo.deferred:
        summary += f", {todo.deferred} left for the next run (videos.max_new_per_run)"
    out(summary + f". API calls: {calls}.")
    if failed_channels and not results:
        return 1
    return 1 if failures else 0


def _preview(note: Any, sinks: Sequence[Sink], full: bool, out: Out) -> None:
    out("")
    out(f"=== {note.video.channel.name}: {note.video.title} ({note.video.url})")
    planned: list[Saved] = []
    for sink in sinks:
        out(f"--- {sink.name}")
        out(sink.preview(note, planned, full))
        guess = getattr(sink, "would_save", None)
        if guess:
            planned.append(guess(note))


def search(args: argparse.Namespace, env: Mapping[str, str], out: Out = print) -> int:
    config = load(args.config)
    url = env.get("QDRANT_URL", "")
    if not url and not config.qdrant.path:
        out("Search needs Qdrant: set QDRANT_URL (and QDRANT_API_KEY) or destinations.qdrant.path.")
        return 2
    q = config.qdrant
    sink = QdrantSink(
        q.collection, q.model, url=url, api_key=env.get("QDRANT_API_KEY", ""), path=q.path
    )
    try:
        hits = sink.search(args.query, limit=args.limit, channel=args.channel)
    finally:
        sink.close()
    if not hits:
        out("Nothing found. Run `youtube-knowledge-base run` with qdrant switched on first.")
        return 0
    for hit in hits:
        where = f" > {hit['chapter']}" if hit.get("chapter") else ""
        out(f"{hit['score']:.2f}  {hit['channel']}: {hit['title']}{where} @ {clock(hit['start'])}")
        out(f"      {hit['url']}")
        out(textwrap.indent(textwrap.fill(hit["text"][:400], 88), "      "))
        out("")
    return 0


def check(args: argparse.Namespace, env: Mapping[str, str], out: Out = print) -> int:
    config = load(args.config)
    state = State.load(args.state)
    now = datetime.now(UTC)
    sinks, skipped = build(config, env, httpx.Client(), now)
    close(sinks)
    out(f"{len(config.channels)} channels: {', '.join(c.ref for c in config.channels)}")
    out("destinations on: " + (", ".join(s.name for s in sinks) or "none"))
    for line in skipped:
        out(f"  off {line}")
    low, high = config.calls_per_run()
    out(
        f"API calls per run: {low} (one per channel) plus one per new video, "
        f"at most {high} (videos.max_new_per_run = {config.max_new_per_run})"
    )
    counts: dict[str, int] = {}
    for entry in state.videos.values():
        counts[entry.get("status", "?")] = counts.get(entry.get("status", "?"), 0) + 1
    out(
        "state: "
        + (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "empty (first run next)")
    )
    if not env.get("SCRAPEUNBLOCKER_KEY"):
        out("SCRAPEUNBLOCKER_KEY is not set: `run` will stop until it is.")
    return 0


def test_destinations(args: argparse.Namespace, env: Mapping[str, str], out: Out = print) -> int:
    config = load(args.config)
    sinks, skipped = build(
        config, env, httpx.Client(headers={"User-Agent": USER_AGENT}), datetime.now(UTC)
    )
    bad = 0
    for sink in sinks:
        try:
            out(f"ok   {sink.name}: {sink.test()}")
        except DeliveryError as exc:
            out(f"FAIL {exc}")
            bad += 1
    for line in skipped:
        out(f"off  {line}")
    close(sinks)
    return 1 if bad else 0


def read_env_file(path: str) -> dict[str, str]:
    """``KEY=value`` lines from a local ``.env`` (for runs on your own machine). Variables
    already set in the environment win."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value:
            out[key.strip()] = value
    return out


def _keep_days(config: Config) -> int | None:
    ages = [c.rules.max_age_days for c in config.channels]
    if not ages or min(ages) == 0:
        return None
    return max(ages) + 30


def _stderr(env: Mapping[str, str]) -> Out:
    actions = env.get("GITHUB_ACTIONS") == "true"

    def err(message: str) -> None:
        print(f"::warning::{message}" if actions else message, file=sys.stderr)

    return err


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = parser().parse_args(argv)
    env = {**read_env_file(".env"), **os.environ}
    try:
        if args.command == "run":
            return run(args, env)
        if args.command == "search":
            return search(args, env)
        if args.command == "check":
            return check(args, env)
        return test_destinations(args, env)
    except (ConfigError, DeliveryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


__all__ = ["main", "parser", "run"]
