from datetime import timedelta

import pytest
from conftest import NOW

from youtube_knowledge_base.collect import ChannelInfo, Video
from youtube_knowledge_base.state import MAX_TRIES, State

INFO = ChannelInfo("UCx", "X", "@x")
VIDEO = Video("vid00000001", "A video", INFO, NOW - timedelta(days=1), 300, "about")


def test_round_trip(tmp_path):
    path = tmp_path / "state" / "videos.json"
    s = State()
    s.add_channel(INFO, "@x", NOW)
    s.saved(VIDEO, "@x", NOW)
    s.save(path)
    again = State.load(path)
    assert again.knows_channel("UCx") and again.status("vid00000001") == "saved"
    assert "video" not in again.videos["vid00000001"]


def test_waiting_keeps_the_video_for_the_next_run():
    s = State()
    s.waiting(VIDEO, "@x", NOW)
    s.waiting(VIDEO, "@x", NOW + timedelta(hours=1))
    entry = s.videos["vid00000001"]
    assert entry["status"] == "waiting" and entry["tries"] == 2
    assert s.retries()[0]["video"]["title"] == "A video"
    assert s.first_seen("vid00000001") == NOW


def test_failed_destination_is_retried_then_given_up():
    s = State()
    assert s.saved(VIDEO, "@x", NOW, failed=["notion"]) == "retry"
    assert s.videos["vid00000001"]["todo"] == ["notion"]
    for _ in range(MAX_TRIES - 1):
        status = s.saved(VIDEO, "@x", NOW, failed=["notion"])
    assert status == "saved" and s.videos["vid00000001"]["gave_up"] == ["notion"]
    assert s.retries() == []


def test_prune_forgets_old_entries_only_with_an_age_limit():
    s = State()
    s.saved(VIDEO, "@x", NOW - timedelta(days=90))
    assert s.prune(NOW, None) == 0
    assert s.prune(NOW, 44) == 1 and not s.seen("vid00000001")


def test_broken_file_names_itself(tmp_path):
    path = tmp_path / "videos.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="videos.json: not valid JSON"):
        State.load(path)
