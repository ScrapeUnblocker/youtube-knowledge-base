from conftest import CHANNEL, NOW, transcript_json

from youtube_knowledge_base import config as cfg
from youtube_knowledge_base import render
from youtube_knowledge_base.collect import ChannelInfo, shape_video, transcript
from youtube_knowledge_base.process import build_note
from youtube_knowledge_base.render import Saved

INFO = ChannelInfo("UCexamplelab0000000000", "Example Lab", "@examplelab")
CONFIG = cfg.parse(
    {
        "channels": ["@examplelab"],
        "mentions": ["Postgres"],
        "destinations": {"obsidian": {}},
    }
)


class One:
    def youtube_transcript(self, video, **kw):
        return transcript_json(video)


def note(with_transcript=True):
    v = shape_video(CHANNEL["videos"][0], INFO)
    tr = transcript(One(), v.id, "en") if with_transcript else None
    return build_note(v, tr, CONFIG.channels[0], CONFIG)


def test_markdown_note_has_properties_embed_chapters_and_timestamped_paragraphs():
    text = render.markdown(note(), NOW)
    head, body = text.split("\n---\n", 1)
    assert head.startswith('---\ntitle: "Build a vector database in 200 lines"')
    assert 'video_id: "vidNewest01"' in head
    assert 'duration: "4:05"' in head
    assert 'tags: ["youtube", "example-lab", "postgres"]' in head
    assert "![](https://www.youtube.com/watch?v=vidNewest01)" in body
    assert "> We build a tiny vector database from scratch" in body
    assert "- **Postgres** (2x)" in body
    assert "  - [1:34](https://www.youtube.com/watch?v=vidNewest01&t=94s) " in body
    assert "- [1:00](https://www.youtube.com/watch?v=vidNewest01&t=60s) Building the index" in body
    assert "*Captions: English (auto-generated).*" in body
    assert "### Postgres and pgvector" in body
    assert "[0:00](https://www.youtube.com/watch?v=vidNewest01&t=0s) Today we build" in body
    assert "> [!abstract]- Description" in body


def test_markdown_note_without_captions():
    text = render.markdown(note(with_transcript=False), NOW)
    assert 'captions: "none"' in text
    assert "*This video has no captions, so there is no transcript.*" in text


def test_markdown_escapes_markup_in_captions():
    assert render._md("a <b> [[link]] %%hidden%%") == "a &lt;b> [\\[link]] %\\%hidden%\\%"


def test_file_names_are_safe_and_dated():
    n = note()
    assert render.file_name(n) == "2026-11-21 Build a vector database in 200 lines.md"
    assert render.safe_name('What? A "quote": #1 / [x]', 90) == "What A quote 1 x"
    assert len(render.safe_name("word " * 50, 90)) <= 90


def test_ping_formats():
    n = note()
    saved = [
        Saved("Obsidian", "YouTube/Example Lab/x.md"),
        Saved("Notion", "page", "https://n.test/p"),
    ]
    plain = render.plain(n, saved)
    assert plain.splitlines()[0] == "New from Example Lab: Build a vector database in 200 lines"
    assert "Mentions Postgres: 1:34, 2:05" in plain
    assert plain.endswith("Saved to Obsidian (YouTube/Example Lab/x.md); Notion (https://n.test/p)")

    slack = render.slack(n, saved)
    assert slack.startswith("*<https://www.youtube.com/watch?v=vidNewest01|Build a vector")
    assert "<https://www.youtube.com/watch?v=vidNewest01&t=94s|1:34>" in slack
    assert "<https://n.test/p|Notion>" in slack

    discord = render.discord(n, saved)
    assert "[1:34](https://www.youtube.com/watch?v=vidNewest01&t=94s)" in discord
    assert "[Notion](https://n.test/p)" in discord

    tg = render.telegram(n, saved)
    assert tg.startswith(
        '<b>Example Lab</b>: <a href="https://www.youtube.com/watch?v=vidNewest01">'
    )
    assert '<a href="https://n.test/p">Notion</a>' in tg


def test_details_line():
    assert render.details(note()) == "4:05 \N{MIDDLE DOT} 21 Nov 2026 \N{MIDDLE DOT} 4 chapters"
    assert render.details(note(with_transcript=False)).endswith("no captions")


def test_chat_text_is_escaped():
    assert render._slack("a <b> & c") == "a &lt;b&gt; &amp; c"
    assert render._discord("*bold* _x_") == "\\*bold\\* \\_x\\_"
