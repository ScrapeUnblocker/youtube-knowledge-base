import base64
from dataclasses import replace

import pytest
from conftest import CHANNEL, ENV, NOW, FakeHttp, Response, transcript_json

from youtube_knowledge_base import config as cfg
from youtube_knowledge_base import sinks
from youtube_knowledge_base.collect import ChannelInfo, shape_video, transcript
from youtube_knowledge_base.process import build_note
from youtube_knowledge_base.render import Saved

INFO = ChannelInfo("UCexamplelab0000000000", "Example Lab", "@examplelab")
CONFIG = cfg.parse(
    {"channels": ["@examplelab"], "mentions": ["Postgres"], "destinations": {"obsidian": {}}}
)


class One:
    def youtube_transcript(self, video, **kw):
        return transcript_json(video)


def note(index=0):
    v = shape_video(CHANNEL["videos"][index], INFO)
    return build_note(v, transcript(One(), v.id, "en"), CONFIG.channels[0], CONFIG)


def no_sleep(_):
    return None


def test_obsidian_folder_writes_one_note_per_video(tmp_path):
    sink = sinks.ObsidianFolder(tmp_path / "vault" / "YouTube", NOW)
    where = sink.deliver(note(), [])
    path = (
        tmp_path
        / "vault"
        / "YouTube"
        / "Example Lab"
        / "2026-11-21 Build a vector database in 200 lines.md"
    )
    assert path.exists()
    assert where == Saved(
        "Obsidian", "YouTube/Example Lab/2026-11-21 Build a vector database in 200 lines.md"
    )
    sink.deliver(note(), [])  # the same video again overwrites the same file
    assert len(list(path.parent.iterdir())) == 1


def test_obsidian_folder_links_the_committed_note_on_github_actions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "you/kb",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REF_NAME": "main",
    }
    built, _ = sinks.build(CONFIG, env, FakeHttp(), NOW)
    where = built[0].deliver(note(), [])
    assert where.url == (
        "https://github.com/you/kb/blob/main/vault/YouTube/Example%20Lab/"
        "2026-11-21%20Build%20a%20vector%20database%20in%20200%20lines.md"
    )
    assert sinks.ObsidianFolder("vault", NOW).deliver(note(), []).url == ""


def test_obsidian_folder_keeps_two_videos_with_the_same_title(tmp_path):
    sink = sinks.ObsidianFolder(tmp_path, NOW)
    a = note()
    sink.deliver(a, [])
    clash = replace(a, video=replace(a.video, id="vidOther999"))
    where = sink.deliver(clash, [])
    assert where.text.endswith("(vidOther999).md")


def test_obsidian_github_creates_then_updates_with_sha():
    stored = {}

    def get(kwargs):
        if not stored:
            return Response(404, {"message": "Not Found"})
        return Response(
            200, {"sha": "abc123", "content": base64.b64encode(stored["text"]).decode()}
        )

    def put(kwargs):
        stored["text"] = base64.b64decode(kwargs["json"]["content"])
        return Response(
            201, {"content": {"html_url": "https://github.test/you/vault/blob/main/x.md"}}
        )

    http = FakeHttp({("GET", "/contents/"): get, ("PUT", "/contents/"): put})
    sink = sinks.ObsidianGitHub(
        "you/vault", "YouTube", "tok", http, NOW, api_base="https://gh.test"
    )
    first = sink.deliver(note(), [])
    assert first.url == "https://github.test/you/vault/blob/main/x.md"
    put_req = [r for r in http.requests if r["method"] == "PUT"][0]
    assert put_req["url"] == (
        "https://gh.test/repos/you/vault/contents/YouTube/Example%20Lab/"
        "2026-11-21%20Build%20a%20vector%20database%20in%20200%20lines.md"
    )
    assert "sha" not in put_req["json"]
    assert put_req["headers"]["Authorization"] == "Bearer tok"
    sink.deliver(note(), [])
    assert [r for r in http.requests if r["method"] == "PUT"][1]["json"]["sha"] == "abc123"


def test_notion_adds_columns_skips_duplicates_and_appends_long_transcripts():
    db = {"properties": {"Name": {"type": "title"}, "URL": {"type": "url"}}, "title": []}
    pages = []

    def query(kwargs):
        vid = kwargs["json"]["filter"]["rich_text"]["equals"]
        hits = [{"url": p["url"]} for p in pages if p["vid"] == vid]
        return Response(200, {"results": hits})

    def create(kwargs):
        props = kwargs["json"]["properties"]
        pages.append(
            {
                "vid": props["Video ID"]["rich_text"][0]["text"]["content"],
                "url": "https://notion.test/p1",
            }
        )
        return Response(200, {"id": "page-1", "url": "https://notion.test/p1"})

    http = FakeHttp(
        {
            ("GET", "/v1/databases/"): Response(200, db),
            ("POST", "/query"): query,
            ("POST", "/v1/pages"): create,
        }
    )
    sink = sinks.NotionSink("secret", "db-1", http, api_base="https://notion.test", sleep=no_sleep)
    n = note()
    long_note = replace(n, paragraphs=n.paragraphs * 60)
    where = sink.deliver(long_note, [])
    assert where == Saved("Notion", "page", "https://notion.test/p1")

    patch_db = [r for r in http.requests if r["method"] == "PATCH" and "/databases/" in r["url"]][0]
    assert set(patch_db["json"]["properties"]) == {
        "Channel",
        "Published",
        "Minutes",
        "Mentions",
        "Video ID",
    }
    create_req = [r for r in http.requests if r["url"].endswith("/v1/pages")][0]["json"]
    assert create_req["parent"] == {"database_id": "db-1"}
    assert create_req["properties"]["Name"]["title"][0]["text"]["content"].startswith(
        "Build a vector"
    )
    assert create_req["properties"]["Mentions"]["multi_select"] == [{"name": "Postgres"}]
    assert len(create_req["children"]) == 100
    assert create_req["children"][0]["type"] == "video"
    appends = [r for r in http.requests if "/blocks/page-1/children" in r["url"]]
    assert appends and all(len(r["json"]["children"]) <= 100 for r in appends)

    again = sink.deliver(n, [])
    assert again.url == "https://notion.test/p1"
    assert len([r for r in http.requests if r["url"].endswith("/v1/pages")]) == 1


def test_notion_rich_text_is_split_at_2000_chars():
    rich = sinks._rich("x" * 4500)
    assert [len(r["text"]["content"]) for r in rich] == [2000, 2000, 500]


def test_chat_payloads():
    http = FakeHttp()
    saved = [Saved("Obsidian", "YouTube/Example Lab/x.md")]
    n = note()
    sinks.SlackSink("https://hooks.slack.test/x", http, no_sleep).deliver(n, saved)
    sinks.DiscordSink("https://discord.test/x", http, no_sleep).deliver(n, saved)
    sinks.TelegramSink("123:abc", "42", http, api_base="https://tg.test", sleep=no_sleep).deliver(
        n, saved
    )
    sinks.WebhookSink("https://hook.test/x", http, no_sleep).deliver(n, saved)
    slack, discord, telegram, webhook = http.posts()

    assert slack["text"] == "New from Example Lab: Build a vector database in 200 lines"
    assert slack["blocks"][0]["accessory"]["image_url"].endswith("/vidNewest01/hqdefault.jpg")
    embed = discord["embeds"][0]
    assert embed["url"] == "https://www.youtube.com/watch?v=vidNewest01"
    assert embed["author"]["name"] == "Example Lab"
    assert discord["allowed_mentions"] == {"parse": []}
    assert http.requests[2]["url"] == "https://tg.test/bot123:abc/sendMessage"
    assert telegram["parse_mode"] == "HTML" and telegram["chat_id"] == "42"
    assert webhook["event"] == "video_saved"
    assert webhook["mentions"][0]["moments"][0]["url"].endswith("&t=94s")
    assert webhook["transcript"]["text"].startswith("Today we build a vector database.")
    assert webhook["saved"] == [
        {"destination": "Obsidian", "where": "YouTube/Example Lab/x.md", "url": ""}
    ]


def test_errors_never_show_the_secret_url():
    http = FakeHttp({("POST", "hooks.slack.test"): Response(404, text="no_team")})
    with pytest.raises(sinks.DeliveryError) as exc:
        sinks.SlackSink("https://hooks.slack.test/T0/B0/secret", http, no_sleep).deliver(note(), [])
    assert "secret" not in str(exc.value)
    assert str(exc.value) == "slack: HTTP 404 no_team"


def test_rate_limit_is_retried_once():
    answers = [Response(429, {}), Response(200, {})]
    http = FakeHttp({("POST", "discord"): lambda kw: answers.pop(0)})
    sinks.DiscordSink("https://discord.test/x", http, no_sleep).deliver(note(), [])
    assert len(http.requests) == 2


def test_build_switches_on_destinations_with_secrets(tmp_path):
    config = cfg.parse(
        {
            "channels": ["@examplelab"],
            "destinations": {
                "obsidian": {"folder": str(tmp_path)},
                "notion": {},
                "qdrant": {},
                "slack": {},
                "telegram": {},
            },
        }
    )
    built, skipped = sinks.build(
        config, {"SLACK_WEBHOOK_URL": ENV["SLACK_WEBHOOK_URL"]}, FakeHttp(), NOW
    )
    assert [s.name for s in built] == ["obsidian", "slack"]
    assert skipped == [
        "notion: set NOTION_TOKEN and NOTION_DATABASE_ID to switch it on",
        "qdrant: set QDRANT_URL (or destinations.qdrant.path) to switch it on",
        "telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to switch it on",
    ]


class FakeEmbedder:
    def passages(self, texts):
        return [[float(len(t) % 7), 1.0, float(i)] for i, t in enumerate(texts)]

    def query(self, text):
        return [1.0, 1.0, 0.0]


def test_qdrant_upserts_passages_and_searches():
    qdrant_client = pytest.importorskip("qdrant_client")
    client = qdrant_client.QdrantClient(":memory:")
    sink = sinks.QdrantSink("yt", "unused", embedder=FakeEmbedder(), client=client)
    where = sink.deliver(note(), [])
    count = client.count("yt").count
    assert where == Saved("Qdrant", f"{count} passages") and count >= 3
    sink.deliver(note(), [])  # stable ids: saving again does not add points
    assert client.count("yt").count == count
    hits = sink.search("anything", limit=2)
    assert len(hits) == 2 and hits[0]["url"].startswith(
        "https://www.youtube.com/watch?v=vidNewest01&t="
    )
    assert sink.search("anything", channel="Someone else") == []
