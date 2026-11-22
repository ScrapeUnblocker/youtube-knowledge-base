from datetime import timedelta

from conftest import CHANNEL, DESCRIPTION, NOW, transcript_json

from youtube_knowledge_base import config as cfg
from youtube_knowledge_base.collect import ChannelInfo, Segment, Transcript, Video, shape_video
from youtube_knowledge_base.process import (
    build_note,
    chapters,
    clock,
    mentions,
    paragraphs,
    passages,
    plan,
    skip_reason,
    summary,
    tag,
)
from youtube_knowledge_base.state import State

INFO = ChannelInfo("UCexamplelab0000000000", "Example Lab", "@examplelab")


def videos():
    return [shape_video(item, INFO) for item in CHANNEL["videos"]]


def config(**videos_opts):
    return cfg.parse(
        {
            "channels": ["@examplelab"],
            "videos": videos_opts,
            "mentions": ["Postgres", "open source"],
            "destinations": {"obsidian": {}},
        }
    )


def tr(lines):
    return Transcript("en", "English", False, tuple(Segment(s, 4.0, t) for s, t in lines))


def test_chapters_follow_youtube_rules():
    found = chapters(DESCRIPTION, 245)
    assert [(c.start, c.title) for c in found] == [
        (0, "Intro"),
        (60, "Building the index"),
        (120, "Postgres and pgvector"),
        (180, "Wrap up"),
    ]


def test_chapters_accept_common_styles_and_long_videos():
    text = "(0:00) Intro\n- 12:30 - Middle part\n[1:02:03] Late\n"
    assert [c.start for c in chapters(text, 4000)] == [0, 750, 3723]
    text = "00:00:00 Start\n00:10:00 A\n01:00:00 B"
    assert [c.start for c in chapters(text, 7200)] == [0, 600, 3600]


def test_chapters_need_three_starting_at_zero():
    assert chapters("0:00 Intro\n1:00 End", 300) == ()
    assert chapters("0:30 Intro\n1:00 A\n2:00 B", 300) == ()
    # timestamps past the end of the video are not chapters
    assert len(chapters("0:00 A\n1:00 B\n2:00 C\n9:00 D", 200)) == 3


def test_paragraphs_break_after_a_sentence_and_at_chapters():
    lines = [
        (0.0, "One two three."),
        (20.0, "Four five six seven"),
        (40.0, "eight. Nine ten"),
        (61.0, "eleven twelve."),
        (75.0, "New chapter text."),
    ]
    chaps = chapters("0:00 A\n1:10 B\n2:00 C", 200)
    paras = paragraphs(tr(lines), chaps, target=40)
    assert [(p.start, p.chapter) for p in paras] == [(0.0, 0), (40.0, 0), (75.0, 1)]
    assert paras[0].text == "One two three. Four five six seven eight."
    assert paras[1].text == "Nine ten eleven twelve."


def test_paragraphs_never_grow_past_twice_the_target():
    lines = [(float(i * 5), f"word{i}") for i in range(40)]
    paras = paragraphs(tr(lines), (), target=30)
    assert all(len(p.text.split()) <= 13 for p in paras)
    assert paras[0].start == 0.0 and paras[1].start > 0


def test_mentions_match_whole_words_across_caption_lines():
    lines = [
        (10.0, "we used postgres here"),
        (20.0, "and Postgres again soon"),  # within 30 s of the first: counted once
        (90.0, "an open"),
        (94.0, "source library"),
        (120.0, "postgresql is not a match"),
    ]
    hits = mentions("A video", tr(lines).segments, ["Postgres", "open source", "Kafka"])
    assert [(m.term, m.times) for m in hits] == [("Postgres", (10.0,)), ("open source", (90.0,))]
    assert "postgres" in hits[0].snippets[0]


def test_mentions_in_the_title_only():
    hits = mentions("Kafka in 100 seconds", (), ["kafka"])
    assert hits[0].in_title and hits[0].times == ()


def test_summary_skips_sponsor_lines_and_links():
    assert summary(DESCRIPTION).startswith("We build a tiny vector database")
    assert summary("Watch: https://x.test\n\nshort") == ""


def test_skip_reasons():
    rules = cfg.Rules(min_minutes=2, max_minutes=12, exclude=("#shorts",), max_age_days=14)
    v = {x.id: x for x in videos()}
    assert skip_reason(v["vidNewest01"], rules, NOW) is None
    assert skip_reason(v["vidShort003"], rules, NOW) == "shorter than 2 min"
    assert skip_reason(v["vidOlder004"], rules, NOW) == "longer than 12 min"
    assert "older than" in skip_reason(v["vidAncient5"], rules, NOW)
    live = Video("live0000001", "Live now", INFO, NOW, None)
    assert skip_reason(live, rules, NOW) == "live or not out yet"
    future = Video("prem0000001", "Premiere", INFO, NOW + timedelta(hours=3), 600)
    assert skip_reason(future, rules, NOW) == "not out yet"
    include = cfg.Rules(min_minutes=0, include=("vector",))
    assert skip_reason(v["vidSecond02"], include, NOW) == "none of the include words"
    assert skip_reason(v["vidNewest01"], include, NOW) is None


def test_first_run_saves_the_newest_and_remembers_the_rest():
    c = config(first_run=1)
    p = plan([(c.channels[0], INFO, videos())], State(), c, NOW)
    assert [x.video.id for x in p.todo] == ["vidNewest01"]
    assert {v.id for v in p.baseline} == {
        "vidSecond02",
        "vidShort003",
        "vidOlder004",
        "vidAncient5",
    }
    assert p.first_channels == ["Example Lab"]


def test_known_channel_saves_only_unseen_matching_videos_oldest_first():
    c = config()
    state = State()
    state.add_channel(INFO, "@examplelab", NOW)
    vs = {v.id: v for v in videos()}
    state.remember([vs["vidOlder004"]], {}, NOW)
    p = plan([(c.channels[0], INFO, list(vs.values()))], state, c, NOW)
    assert [x.video.id for x in p.todo] == ["vidSecond02", "vidNewest01"]
    assert p.baseline == []


def test_plan_caps_the_run_and_brings_retries_back_first():
    c = cfg.parse(
        {
            "channels": ["@examplelab"],
            "videos": {"max_new_per_run": 1},
            "destinations": {"obsidian": {}},
        }
    )
    state = State()
    state.add_channel(INFO, "@examplelab", NOW)
    vs = {v.id: v for v in videos()}
    state.waiting(vs["vidSecond02"], "@examplelab", NOW)
    p = plan([(c.channels[0], INFO, list(vs.values()))], state, c, NOW)
    assert [x.video.id for x in p.todo] == ["vidSecond02"]
    assert p.deferred == 2


def test_build_note_collects_everything():
    c = config()
    vs = {v.id: v for v in videos()}
    from youtube_knowledge_base.collect import transcript

    class One:
        def youtube_transcript(self, video, **kw):
            return transcript_json(video)

    note = build_note(vs["vidNewest01"], transcript(One(), "vidNewest01", "en"), c.channels[0], c)
    assert len(note.chapters) == 4
    assert note.paragraphs[0].text.startswith("Today we build")
    assert "[Music]" not in " ".join(p.text for p in note.paragraphs)
    assert [m.term for m in note.mentions] == ["Postgres", "open source"]
    assert note.mentions[0].times == (94.0, 125.0)
    assert note.tags == ("youtube", "example-lab", "postgres", "open-source")
    assert note.mentioned


def test_passages_have_stable_ids_and_timestamp_links():
    c = config()
    v = {x.id: x for x in videos()}["vidNewest01"]
    note = build_note(v, tr([(0.0, "Hello there."), (70.0, "Second part.")]), c.channels[0], c)
    first = passages(note)
    assert first == passages(note)
    assert first[0]["payload"]["url"].endswith("&t=0s")
    assert first[1]["payload"]["chapter"] == "Building the index"
    assert first[1]["text"].startswith("Build a vector database in 200 lines (Building the index)")


def test_clock_and_tag():
    assert clock(5) == "0:05"
    assert clock(754) == "12:34"
    assert clock(3725) == "1:02:05"
    assert tag("Lex Fridman") == "lex-fridman"
    assert tag("C++ & Rust!") == "c-rust"
    assert tag("2024") == ""
