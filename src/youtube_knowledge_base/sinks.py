"""Destinations behind one small interface.

There are two kinds. *Note* destinations keep the whole video (Obsidian, Notion, Qdrant);
*chat* destinations send a short ping that links to it (Slack, Discord, Telegram, a
webhook). Every destination has a ``name``, ``kind`` and three methods:

* ``deliver(note, saved)`` saves or sends one video and returns where it went (a
  :class:`~youtube_knowledge_base.render.Saved`, or ``None`` for a chat ping);
* ``preview(note, saved, full)`` returns what ``deliver`` would do, for ``--dry-run``;
* ``test()`` checks the connection, for ``test-destinations``.

Note destinations run first, so the ping can say where the note landed. Failures raise
:class:`DeliveryError`. Secrets come from environment variables, never from
``config.yml``. To add a destination, write a class like these and add it to
:func:`build`.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from . import render
from .config import Config
from .process import Note, clock, passages
from .render import Saved

USER_AGENT = "youtube-knowledge-base (+https://github.com/ScrapeUnblocker/youtube-knowledge-base)"
CONNECTED = "youtube-knowledge-base is connected. New videos from your channels arrive here."
PREVIEW_LINES = 40
NOTION_VERSION = "2022-06-28"
_NOTION_ID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.I)


class DeliveryError(RuntimeError):
    """A destination did not take the video."""


class Sink(Protocol):
    name: str
    kind: str

    def deliver(self, note: Note, saved: Sequence[Saved]) -> Saved | None: ...

    def preview(self, note: Note, saved: Sequence[Saved], full: bool = False) -> str: ...

    def test(self) -> str: ...


class Http(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


def call(
    http: Http,
    method: str,
    url: str,
    *,
    sink: str,
    ok: Sequence[int] = (),
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """One HTTP request, retried once on 429/502/503. Errors never include the URL, because
    webhook URLs and bot tokens are secrets. Statuses in ``ok`` are returned, not raised."""
    for attempt in (1, 2):
        try:
            resp = http.request(method, url, timeout=60.0, **kwargs)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"{sink}: request failed ({type(exc).__name__})") from None
        if resp.status_code in (429, 502, 503) and attempt == 1:
            sleep(_retry_after(resp))
            continue
        if resp.status_code >= 300 and resp.status_code not in ok:
            body = (resp.text or "")[:300].replace("\n", " ")
            raise DeliveryError(f"{sink}: HTTP {resp.status_code} {body}".rstrip())
        return resp
    raise AssertionError("unreachable")


def _retry_after(resp: Any) -> float:
    wait: Any = None
    try:
        wait = resp.headers.get("Retry-After")
    except AttributeError:
        pass
    if wait is None:
        try:
            wait = json.loads(resp.text or "{}").get("parameters", {}).get("retry_after")
        except (ValueError, AttributeError):
            wait = None
    try:
        return min(max(float(wait), 1.0), 30.0)
    except (TypeError, ValueError):
        return 2.0


def _head(text: str, full: bool) -> str:
    lines = text.splitlines()
    if full or len(lines) <= PREVIEW_LINES:
        return text
    rest = len(lines) - PREVIEW_LINES
    return "\n".join(lines[:PREVIEW_LINES]) + f"\n[... {rest} more lines; --full shows all]"


# Obsidian


class ObsidianFolder:
    """Markdown notes in a folder: your vault, or a folder of this repository that the
    scheduled workflow commits (Obsidian Git, or any sync, brings it to your devices)."""

    name = "obsidian"
    kind = "note"

    def __init__(self, folder: str | Path, now: datetime, web_base: str = "") -> None:
        self.folder = Path(folder).expanduser()
        self.now = now
        self.web_base = web_base.rstrip("/")

    def path_for(self, note: Note) -> Path:
        path = self.folder / render.folder_name(note) / render.file_name(note)
        if path.exists() and not _same_video(path.read_text(encoding="utf-8"), note):
            path = path.with_name(f"{path.stem} ({note.video.id}).md")
        return path

    def deliver(self, note: Note, saved: Sequence[Saved]) -> Saved:
        path = self.path_for(note)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render.markdown(note, self.now), encoding="utf-8", newline="\n")
        except OSError as exc:
            raise DeliveryError(f"obsidian: cannot write {path} ({exc.strerror})") from None
        return Saved("Obsidian", self._shown(path), self._web(path))

    def _web(self, path: Path) -> str:
        """On GitHub Actions: the note's page in this repository, live once the workflow
        has committed it."""
        if not self.web_base:
            return ""
        try:
            rel = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            return ""
        return f"{self.web_base}/{quote(rel)}"

    def preview(self, note: Note, saved: Sequence[Saved], full: bool = False) -> str:
        path = self.path_for(note)
        return f"write {self._shown(path)}\n" + _head(render.markdown(note, self.now), full)

    def would_save(self, note: Note) -> Saved:
        return Saved("Obsidian", self._shown(self.path_for(note)))

    def test(self) -> str:
        self.folder.mkdir(parents=True, exist_ok=True)
        return f"notes go to {self.folder}"

    def _shown(self, path: Path) -> str:
        try:
            return path.relative_to(self.folder.parent).as_posix()
        except ValueError:
            return path.as_posix()


class ObsidianGitHub:
    """Markdown notes committed straight into another GitHub repository, such as the
    private repo your vault syncs with through the Obsidian Git plugin. Needs a
    fine-grained token with Contents read and write on that one repository."""

    name = "obsidian"
    kind = "note"

    def __init__(
        self,
        repo: str,
        folder: str,
        token: str,
        http: Http,
        now: datetime,
        branch: str = "",
        api_base: str = "https://api.github.com",
    ) -> None:
        self.repo = repo
        self.folder = folder.strip("/")
        self.branch = branch
        self.http = http
        self.now = now
        self.base = f"{api_base.rstrip('/')}/repos/{repo}/contents/"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }

    def _path(self, note: Note, suffix: str = "") -> str:
        name = render.file_name(note)
        if suffix:
            name = name[:-3] + f" ({suffix}).md"
        return f"{self.folder}/{render.folder_name(note)}/{name}"

    def _get(self, path: str) -> dict[str, Any] | None:
        params = {"ref": self.branch} if self.branch else None
        resp = call(
            self.http,
            "GET",
            self.base + quote(path),
            sink="obsidian",
            ok=(404,),
            headers=self.headers,
            params=params,
        )
        return None if resp.status_code == 404 else resp.json()

    def deliver(self, note: Note, saved: Sequence[Saved]) -> Saved:
        path = self._path(note)
        current = self._get(path)
        if current and not _same_video(_decode(current), note):
            path = self._path(note, note.video.id)
            current = self._get(path)
        body: dict[str, Any] = {
            "message": f"Add note: {note.video.title[:60]}",
            "content": base64.b64encode(render.markdown(note, self.now).encode()).decode(),
        }
        if self.branch:
            body["branch"] = self.branch
        if current:
            body["sha"] = current.get("sha")
        resp = call(
            self.http,
            "PUT",
            self.base + quote(path),
            sink="obsidian",
            json=body,
            headers=self.headers,
        )
        url = ((resp.json() or {}).get("content") or {}).get("html_url", "")
        return Saved("Obsidian", path, url)

    def preview(self, note: Note, saved: Sequence[Saved], full: bool = False) -> str:
        head = _head(render.markdown(note, self.now), full)
        return f"commit {self.repo}:{self._path(note)}\n{head}"

    def would_save(self, note: Note) -> Saved:
        return Saved("Obsidian", self._path(note))

    def test(self) -> str:
        resp = call(
            self.http,
            "GET",
            self.base.rsplit("/contents/", 1)[0],
            sink="obsidian",
            headers=self.headers,
        )
        return f"can reach {resp.json().get('full_name', self.repo)}"


def _same_video(text: str, note: Note) -> bool:
    return f'video_id: "{note.video.id}"' in text[:3000]


def _decode(item: Mapping[str, Any]) -> str:
    try:
        return base64.b64decode(item.get("content") or "").decode("utf-8", "replace")
    except ValueError:
        return ""


# Notion


class NotionSink:
    """One page per video in a Notion database: the video embedded, the details as
    columns (added to the database if missing), mentions, chapters and the transcript with
    a timestamp link per paragraph. Share the database with your integration first.

    ``NOTION_API_URL`` points at another API host (used by the tests)."""

    name = "notion"
    kind = "note"
    columns = {
        "Channel": {"select": {}},
        "Published": {"date": {}},
        "URL": {"url": {}},
        "Minutes": {"number": {}},
        "Mentions": {"multi_select": {}},
        "Video ID": {"rich_text": {}},
    }

    def __init__(
        self,
        token: str,
        database_id: str,
        http: Http,
        api_base: str = "https://api.notion.com",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.database_id = notion_id(database_id)
        self.http = http
        self.sleep = sleep
        self.base = api_base.rstrip("/") + "/v1"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "User-Agent": USER_AGENT,
        }
        self._title: str | None = None

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        resp = call(
            self.http,
            method,
            self.base + path,
            sink="notion",
            sleep=self.sleep,
            headers=self.headers,
            json=body,
        )
        self.sleep(0.35)  # Notion allows about three requests a second
        return resp.json()

    def _setup(self) -> str:
        """Find the title column and add any missing columns, once per run."""
        if self._title:
            return self._title
        db = self._call("GET", f"/databases/{self.database_id}")
        props = db.get("properties") or {}
        title = next((k for k, v in props.items() if v.get("type") == "title"), "Name")
        missing = {k: v for k, v in self.columns.items() if k not in props}
        if missing:
            self._call("PATCH", f"/databases/{self.database_id}", {"properties": missing})
        self._title = title
        return title

    def _existing(self, video_id: str) -> str | None:
        found = self._call(
            "POST",
            f"/databases/{self.database_id}/query",
            {"filter": {"property": "Video ID", "rich_text": {"equals": video_id}}, "page_size": 1},
        )
        results = found.get("results") or []
        return results[0].get("url", "") if results else None

    def properties(self, note: Note, title: str) -> dict[str, Any]:
        v = note.video
        props: dict[str, Any] = {
            title: {"title": _rich(v.title[:2000])},
            "Channel": {"select": {"name": _option(v.channel.name)}},
            "URL": {"url": v.url},
            "Video ID": {"rich_text": _rich(v.id)},
            "Mentions": {"multi_select": [{"name": _option(m.term)} for m in note.mentions]},
        }
        if v.published:
            props["Published"] = {"date": {"start": v.published.date().isoformat()}}
        if v.seconds:
            props["Minutes"] = {"number": round(v.seconds / 60, 1)}
        return props

    def deliver(self, note: Note, saved: Sequence[Saved]) -> Saved:
        title = self._setup()
        existing = self._existing(note.video.id)
        if existing is not None:
            return Saved("Notion", "page", existing)
        blocks = notion_blocks(note)
        page = self._call(
            "POST",
            "/pages",
            {
                "parent": {"database_id": self.database_id},
                "properties": self.properties(note, title),
                "children": blocks[:100],
            },
        )
        try:
            for i in range(100, len(blocks), 100):
                self._call(
                    "PATCH", f"/blocks/{page['id']}/children", {"children": blocks[i : i + 100]}
                )
        except DeliveryError:
            # Half a transcript would look complete; archive it so the retry starts clean.
            try:
                self._call("PATCH", f"/pages/{page['id']}", {"archived": True})
            except DeliveryError:
                pass
            raise
        return Saved("Notion", "page", page.get("url", ""))

    def preview(self, note: Note, saved: Sequence[Saved], full: bool = False) -> str:
        blocks = notion_blocks(note)
        return (
            f"add a page to database {self.database_id}: {note.video.title}"
            f" ({len(blocks)} blocks, {len(note.paragraphs)} transcript paragraphs)"
        )

    def would_save(self, note: Note) -> Saved:
        return Saved("Notion", "page")

    def test(self) -> str:
        db = self._call("GET", f"/databases/{self.database_id}")
        name = "".join(t.get("plain_text", "") for t in db.get("title") or []) or "database"
        return f"can reach {name}"


def notion_id(text: str) -> str:
    """The database id from an id or a pasted database link."""
    found = _NOTION_ID.findall(text)
    return found[-1] if found else text.strip()


def _rich(text: str, url: str = "") -> list[dict[str, Any]]:
    """Notion rich text, split into the 2,000-character pieces the API accepts."""
    out = []
    for i in range(0, max(len(text), 1), 2000):
        piece: dict[str, Any] = {"type": "text", "text": {"content": text[i : i + 2000]}}
        if url:
            piece["text"]["link"] = {"url": url}
        out.append(piece)
    return out


def _option(name: str) -> str:
    return name.replace(",", " ")[:100] or "-"


def _block(kind: str, rich: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"object": "block", "type": kind, kind: {"rich_text": rich, **extra}}


def notion_blocks(note: Note) -> list[dict[str, Any]]:
    v = note.video
    blocks: list[dict[str, Any]] = [
        {
            "object": "block",
            "type": "video",
            "video": {"type": "external", "external": {"url": v.url}},
        },
        _block("paragraph", _rich(f"{v.channel.name}{render.DOT}{render.details(note)}")),
    ]
    if note.summary:
        blocks.append(_block("quote", _rich(note.summary)))
    if note.mentions:
        blocks.append(_block("heading_2", _rich("Mentions")))
        for m in note.mentions:
            rich = [
                {"type": "text", "text": {"content": f"{m.term}: "}, "annotations": {"bold": True}}
            ]
            if not m.times:
                rich += _rich("in the title")
            for i, at in enumerate(m.times):
                if i:
                    rich += _rich(", ")
                rich += _rich(clock(at), v.at(at))
            blocks.append(_block("bulleted_list_item", rich[:100]))
    if note.chapters:
        blocks.append(_block("heading_2", _rich("Chapters")))
        for c in note.chapters:
            rich = _rich(clock(c.start), v.at(c.start)) + _rich(f" {c.title}")
            blocks.append(_block("bulleted_list_item", rich))
    blocks.append(_block("heading_2", _rich("Transcript")))
    if note.transcript is None:
        blocks.append(_block("paragraph", _rich("This video has no captions.")))
    current: int | None = None
    for p in note.paragraphs:
        if note.chapters and p.chapter is not None and p.chapter != current:
            current = p.chapter
            blocks.append(_block("heading_3", _rich(note.chapters[current].title)))
        rich = _rich(clock(p.start), v.at(p.start)) + _rich(f" {p.text}")
        blocks.append(_block("paragraph", rich[:100]))
    if v.description.strip():
        paras = [p.strip() for p in v.description.strip().split("\n\n") if p.strip()][:50]
        blocks.append(
            _block(
                "toggle",
                _rich("Description"),
                children=[_block("paragraph", _rich(p)) for p in paras],
            )
        )
    return blocks


# Qdrant


class FastEmbedder:
    """Local embeddings with fastembed (ONNX, CPU): no API key, no per-call cost. The
    model downloads once and is cached."""

    def __init__(self, model: str) -> None:
        from fastembed import TextEmbedding

        self.model = TextEmbedding(model)

    def passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(x) for x in vec] for vec in self.model.passage_embed(list(texts))]

    def query(self, text: str) -> list[float]:
        return [float(x) for x in next(iter(self.model.query_embed(text)))]


class QdrantSink:
    """Transcript passages in a Qdrant collection, one point per paragraph with the
    video, channel, chapter and a timestamp link as payload, for RAG or the ``search``
    command. Qdrant Cloud (``QDRANT_URL`` + ``QDRANT_API_KEY``) or a local folder."""

    name = "qdrant"
    kind = "note"

    def __init__(
        self,
        collection: str,
        model: str,
        url: str = "",
        api_key: str = "",
        path: str = "",
        embedder: Any = None,
        client: Any = None,
    ) -> None:
        try:
            from qdrant_client import QdrantClient, models
        except ImportError:
            raise DeliveryError(
                "qdrant: install the extra first: pip install '.[qdrant]'"
            ) from None
        self.models = models
        self.collection = collection
        self.model_name = model
        self.where = url or str(Path(path).expanduser())
        if url:
            self._connect = lambda: QdrantClient(url=url, api_key=api_key or None, timeout=60)
        else:
            self._connect = lambda: QdrantClient(path=str(Path(path).expanduser()))
        self._client = client
        self._embedder = embedder
        self._ready = False

    @property
    def client(self) -> Any:
        """Connected on first use, so ``--dry-run`` never opens or creates a database."""
        if self._client is None:
            self._client = self._connect()
        return self._client

    @property
    def embedder(self) -> Any:
        if self._embedder is None:
            self._embedder = FastEmbedder(self.model_name)
        return self._embedder

    def _ensure(self, size: int) -> None:
        if self._ready:
            return
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=self.models.VectorParams(
                    size=size, distance=self.models.Distance.COSINE
                ),
            )
        self._ready = True

    def deliver(self, note: Note, saved: Sequence[Saved]) -> Saved:
        items = passages(note)
        if not items:
            return Saved("Qdrant", "no transcript to index")
        try:
            vectors = self.embedder.passages([i["text"] for i in items])
            self._ensure(len(vectors[0]))
            points = [
                self.models.PointStruct(id=i["id"], vector=vec, payload=i["payload"])
                for i, vec in zip(items, vectors, strict=True)
            ]
            for start in range(0, len(points), 64):
                self.client.upsert(self.collection, points=points[start : start + 64])
        except DeliveryError:
            raise
        except Exception as exc:  # the client raises many types; report them all the same
            raise DeliveryError(f"qdrant: {type(exc).__name__}: {str(exc)[:200]}") from None
        return Saved("Qdrant", f"{len(points)} passages")

    def search(self, query: str, limit: int = 5, channel: str = "") -> list[dict[str, Any]]:
        flt = None
        if channel:
            m = self.models
            flt = m.Filter(
                must=[m.FieldCondition(key="channel", match=m.MatchValue(value=channel))]
            )
        try:
            if not self.client.collection_exists(self.collection):
                return []
            found = self.client.query_points(
                self.collection, query=self.embedder.query(query), limit=limit, query_filter=flt
            )
        except Exception as exc:
            raise DeliveryError(f"qdrant: {type(exc).__name__}: {str(exc)[:200]}") from None
        return [{"score": p.score, **(p.payload or {})} for p in found.points]

    def preview(self, note: Note, saved: Sequence[Saved], full: bool = False) -> str:
        n = len(passages(note))
        return f"upsert {n} passages into {self.collection} ({self.where})"

    def would_save(self, note: Note) -> Saved:
        n = len(passages(note))
        return Saved("Qdrant", f"{n} passages" if n else "no transcript to index")

    def test(self) -> str:
        try:
            names = [c.name for c in self.client.get_collections().collections]
        except Exception as exc:
            raise DeliveryError(f"qdrant: {type(exc).__name__}: {str(exc)[:200]}") from None
        state = "exists" if self.collection in names else "is created on the first save"
        return f"connected; collection {self.collection} {state}"

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:
            pass


# chat pings


class SlackSink:
    """Slack incoming webhook: the title, details, mentions and where the note is, with
    the thumbnail beside them."""

    name = "slack"
    kind = "chat"

    def __init__(self, url: str, http: Http, sleep: Callable[[float], None] = time.sleep):
        self.url = url
        self.http = http
        self.sleep = sleep

    def payload(self, note: Note, saved: Sequence[Saved]) -> dict[str, Any]:
        section: dict[str, Any] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": render.slack(note, saved)[:3000]},
        }
        if note.video.thumbnail:
            section["accessory"] = {
                "type": "image",
                "image_url": note.video.thumbnail,
                "alt_text": note.video.title[:2000],
            }
        return {"text": render.headline(note), "blocks": [section], "unfurl_links": False}

    def deliver(self, note: Note, saved: Sequence[Saved]) -> None:
        call(
            self.http,
            "POST",
            self.url,
            sink="slack",
            sleep=self.sleep,
            json=self.payload(note, saved),
        )
        self.sleep(1.0)

    def preview(self, note: Note, saved: Sequence[Saved], full: bool = False) -> str:
        return render.slack(note, saved)

    def test(self) -> str:
        call(self.http, "POST", self.url, sink="slack", json={"text": CONNECTED})
        return "test message sent"


class DiscordSink:
    """Discord channel webhook: one embed per video. Mentions never ping anyone."""

    name = "discord"
    kind = "chat"

    def __init__(self, url: str, http: Http, sleep: Callable[[float], None] = time.sleep):
        self.url = url
        self.http = http
        self.sleep = sleep

    def payload(self, note: Note, saved: Sequence[Saved]) -> dict[str, Any]:
        v = note.video
        embed: dict[str, Any] = {
            "title": v.title[:256],
            "url": v.url,
            "description": render.discord(note, saved)[:4096],
            "color": 0xFF0033,
            "author": {"name": v.channel.name[:256], "url": v.channel.url},
        }
        if v.thumbnail:
            embed["thumbnail"] = {"url": v.thumbnail}
        return {"embeds": [embed], "allowed_mentions": {"parse": []}}

    def deliver(self, note: Note, saved: Sequence[Saved]) -> None:
        call(
            self.http,
            "POST",
            self.url,
            sink="discord",
            sleep=self.sleep,
            json=self.payload(note, saved),
        )
        self.sleep(0.5)

    def preview(self, note: Note, saved: Sequence[Saved], full: bool = False) -> str:
        return f"**{note.video.title}** <{note.video.url}>\n" + render.discord(note, saved)

    def test(self) -> str:
        call(
            self.http,
            "POST",
            self.url,
            sink="discord",
            json={"content": CONNECTED, "allowed_mentions": {"parse": []}},
        )
        return "test message sent"


class TelegramSink:
    """Telegram bot message to one chat (you, a group or a channel), with the video's
    link preview. ``TELEGRAM_API_BASE`` points at a self-hosted Bot API server."""

    name = "telegram"
    kind = "chat"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        http: Http,
        api_base: str = "https://api.telegram.org",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base = f"{api_base.rstrip('/')}/bot{bot_token}"
        self.chat_id = chat_id
        self.http = http
        self.sleep = sleep

    def payload(self, note: Note, saved: Sequence[Saved]) -> dict[str, Any]:
        text = render.telegram(note, saved)
        if len(text) > 4096:
            return {"chat_id": self.chat_id, "text": render.plain(note, saved)[:4096]}
        return {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"url": note.video.url},
        }

    def deliver(self, note: Note, saved: Sequence[Saved]) -> None:
        call(
            self.http,
            "POST",
            f"{self.base}/sendMessage",
            sink="telegram",
            sleep=self.sleep,
            json=self.payload(note, saved),
        )
        self.sleep(1.0)

    def preview(self, note: Note, saved: Sequence[Saved], full: bool = False) -> str:
        return render.telegram(note, saved)

    def test(self) -> str:
        call(
            self.http,
            "POST",
            f"{self.base}/sendMessage",
            sink="telegram",
            json={"chat_id": self.chat_id, "text": CONNECTED},
        )
        return "test message sent"


class WebhookSink:
    """A JSON POST per video for Zapier, Make, n8n or your own code, with the full
    transcript text, chapters, mentions and where the note was saved."""

    name = "webhook"
    kind = "chat"

    def __init__(self, url: str, http: Http, sleep: Callable[[float], None] = time.sleep):
        self.url = url
        self.http = http
        self.sleep = sleep

    def payload(self, note: Note, saved: Sequence[Saved]) -> dict[str, Any]:
        v = note.video
        tr = note.transcript
        return {
            "event": "video_saved",
            "video": {**v.to_dict(), "url": v.url},
            "summary": note.summary,
            "chapters": [
                {"start": c.start, "title": c.title, "url": v.at(c.start)} for c in note.chapters
            ],
            "mentions": [
                {
                    "term": m.term,
                    "in_title": m.in_title,
                    "moments": [
                        {"start": int(t), "url": v.at(t), "snippet": s}
                        for t, s in zip(m.times, m.snippets, strict=True)
                    ],
                }
                for m in note.mentions
            ],
            "transcript": (
                {
                    "language": tr.language,
                    "auto_generated": tr.auto,
                    "text": " ".join(p.text for p in note.paragraphs),
                }
                if tr
                else None
            ),
            "saved": [{"destination": s.label, "where": s.text, "url": s.url} for s in saved],
            "text": render.plain(note, saved),
        }

    def deliver(self, note: Note, saved: Sequence[Saved]) -> None:
        call(
            self.http,
            "POST",
            self.url,
            sink="webhook",
            sleep=self.sleep,
            json=self.payload(note, saved),
        )

    def preview(self, note: Note, saved: Sequence[Saved], full: bool = False) -> str:
        data = self.payload(note, saved)
        if data["transcript"] and not full:
            text = data["transcript"]["text"]
            data["transcript"]["text"] = text[:200] + (
                f"... ({len(text)} chars)" if len(text) > 200 else ""
            )
        return json.dumps(data, indent=2, ensure_ascii=False)

    def test(self) -> str:
        call(self.http, "POST", self.url, sink="webhook", json={"event": "test", "text": CONNECTED})
        return "test message sent"


# wiring


def build(
    config: Config, env: Mapping[str, str], http: Http, now: datetime
) -> tuple[list[Sink], list[str]]:
    """The destinations that are switched on in config.yml and have their secrets set,
    note destinations first. Also returns one line per destination that was skipped."""
    sinks: list[Any] = []
    skipped: list[str] = []

    def need(name: str, *keys: str) -> bool:
        missing = [k for k in keys if not env.get(k)]
        if missing:
            skipped.append(f"{name}: set {' and '.join(missing)} to switch it on")
        return not missing

    for name in config.destinations:
        if name == "obsidian":
            o = config.obsidian
            if not o.repo:
                web = ""
                if env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_REPOSITORY"):
                    server = env.get("GITHUB_SERVER_URL") or "https://github.com"
                    branch = env.get("GITHUB_REF_NAME") or "main"
                    web = f"{server}/{env['GITHUB_REPOSITORY']}/blob/{quote(branch)}"
                sinks.append(ObsidianFolder(o.folder, now, web_base=web))
            elif need(name, "VAULT_GITHUB_TOKEN"):
                sinks.append(
                    ObsidianGitHub(
                        o.repo,
                        o.folder,
                        env["VAULT_GITHUB_TOKEN"],
                        http,
                        now,
                        branch=o.branch,
                        api_base=env.get("GITHUB_API_URL") or "https://api.github.com",
                    )
                )
        elif name == "notion":
            db = config.notion.database_id or env.get("NOTION_DATABASE_ID", "")
            keys = ("NOTION_TOKEN",) if db else ("NOTION_TOKEN", "NOTION_DATABASE_ID")
            if need(name, *keys):
                sinks.append(
                    NotionSink(
                        env["NOTION_TOKEN"],
                        db,
                        http,
                        api_base=env.get("NOTION_API_URL") or "https://api.notion.com",
                    )
                )
        elif name == "qdrant":
            q = config.qdrant
            url = env.get("QDRANT_URL", "")
            if url or q.path:
                sinks.append(
                    QdrantSink(
                        q.collection,
                        q.model,
                        url=url,
                        api_key=env.get("QDRANT_API_KEY", ""),
                        path=q.path,
                    )
                )
            else:
                skipped.append(
                    "qdrant: set QDRANT_URL (or destinations.qdrant.path) to switch it on"
                )
        elif name == "slack" and need(name, "SLACK_WEBHOOK_URL"):
            sinks.append(SlackSink(env["SLACK_WEBHOOK_URL"], http))
        elif name == "discord" and need(name, "DISCORD_WEBHOOK_URL"):
            sinks.append(DiscordSink(env["DISCORD_WEBHOOK_URL"], http))
        elif name == "telegram" and need(name, "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            sinks.append(
                TelegramSink(
                    env["TELEGRAM_BOT_TOKEN"],
                    env["TELEGRAM_CHAT_ID"],
                    http,
                    api_base=env.get("TELEGRAM_API_BASE") or "https://api.telegram.org",
                )
            )
        elif name == "webhook" and need(name, "WEBHOOK_URL"):
            sinks.append(WebhookSink(env["WEBHOOK_URL"], http))
    sinks.sort(key=lambda s: 0 if s.kind == "note" else 1)
    return sinks, skipped


def close(sinks: Sequence[Any]) -> None:
    for sink in sinks:
        closer = getattr(sink, "close", None)
        if closer:
            closer()
