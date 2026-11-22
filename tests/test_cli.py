import copy
import json
from datetime import timedelta

import pytest
from conftest import CHANNEL, ENV, NOW, FakeClient, FakeHttp, Response, transcript_json, video_item

from youtube_knowledge_base import cli


def args(workdir, *extra):
    return cli.parser().parse_args(
        [
            "run",
            "--config",
            str(workdir / "config.yml"),
            "--state",
            str(workdir / "state" / "videos.json"),
            *extra,
        ]
    )


def go(workdir, client, http, *extra, now=NOW, env=ENV):
    lines: list[str] = []
    errors: list[str] = []
    code = cli.run(
        args(workdir, *extra),
        env,
        client=client,
        http=http,
        now=now,
        out=lines.append,
        err=errors.append,
    )
    return code, lines, errors


def notes(workdir):
    return sorted(p.name for p in (workdir / "vault").rglob("*.md"))


def test_parser():
    p = cli.parser()
    a = p.parse_args(["run", "--dry-run", "--config", "c.yml"])
    assert (a.command, a.dry_run, a.config, a.state) == ("run", True, "c.yml", "state/videos.json")
    a = p.parse_args(["search", "vector databases", "--limit", "3"])
    assert (a.query, a.limit, a.channel) == ("vector databases", 3, "")
    assert p.parse_args(["check"]).command == "check"
    assert p.parse_args(["test-destinations"]).command == "test-destinations"
    with pytest.raises(SystemExit):
        p.parse_args([])


def test_first_run_then_nothing_new(workdir):
    client = FakeClient(transcripts={"vidNewest01": transcript_json()})
    http = FakeHttp()
    code, lines, errors = go(workdir, client, http)
    assert code == 0, errors
    # first run: the two newest matching uploads; vidSecond02 has no captions and is
    # three days old, past wait_hours, so it is saved without a transcript
    assert notes(workdir) == [
        "2026-11-19 Rust for Python developers.md",
        "2026-11-21 Build a vector database in 200 lines.md",
    ]
    assert len(http.requests) == 8  # four chat destinations x two videos
    slack = http.posts("hooks.slack.test")
    assert slack[1]["text"] == "New from Example Lab: Build a vector database in 200 lines"
    assert "Saved to Obsidian (YouTube/Example Lab/2026-11-21 Build a vector" in json.dumps(
        slack[1]
    )
    state = json.loads((workdir / "state" / "videos.json").read_text(encoding="utf-8"))
    statuses = {vid: e["status"] for vid, e in state["videos"].items()}
    assert statuses == {
        "vidNewest01": "saved",
        "vidSecond02": "saved",
        "vidShort003": "baseline",
        "vidOlder004": "baseline",
        "vidAncient5": "baseline",
    }
    assert lines[-1].startswith("Done: 1 of 1 channels read, 2 videos saved")
    assert lines[-1].endswith("API calls: 3.")

    # the second run against the same state delivers nothing and fetches no transcript
    client.calls.clear()
    http2 = FakeHttp()
    code, lines, _ = go(workdir, client, http2, now=NOW + timedelta(hours=6))
    assert code == 0
    assert http2.requests == []
    assert [c[0] for c in client.calls] == ["channel"]
    assert lines[-1] == "Done: 1 of 1 channels read, 0 videos saved. API calls: 1."


def test_dry_run_delivers_nothing_and_keeps_no_state(workdir):
    client = FakeClient(transcripts={"vidNewest01": transcript_json()})
    http = FakeHttp()
    code, lines, _ = go(workdir, client, http, "--dry-run")
    assert code == 0
    assert http.requests == []
    assert notes(workdir) == []
    assert not (workdir / "state" / "videos.json").exists()
    text = "\n".join(lines)
    assert lines[0].startswith("DRY RUN")
    assert "--- slack" in text and "--- telegram" in text and "--- webhook" in text
    assert "write YouTube/Example Lab/2026-11-21 Build a vector database in 200 lines.md" in text
    assert "Saved to" in text
    assert lines[-1].startswith("Done: 1 of 1 channels read, 2 videos would be saved")


def test_new_upload_waits_for_captions_then_is_saved(workdir):
    channel = copy.deepcopy(CHANNEL)
    client = FakeClient(
        channels={"@examplelab": channel}, transcripts={"vidNewest01": transcript_json()}
    )
    go(workdir, client, FakeHttp())

    fresh = video_item("vidFresh006", "Postgres in production", "2026-11-22T11:00:00Z", 480)
    channel["videos"].insert(0, fresh)
    http = FakeHttp()
    code, lines, _ = go(workdir, client, http, now=NOW)
    assert code == 0 and http.requests == []
    assert any(line.startswith("waiting Example Lab: Postgres in production") for line in lines)

    client.transcripts["vidFresh006"] = transcript_json("vidFresh006")
    http = FakeHttp()
    code, lines, _ = go(workdir, client, http, now=NOW + timedelta(hours=3))
    assert code == 0 and len(http.requests) == 4
    assert "2026-11-22 Postgres in production.md" in notes(workdir)


def test_a_failed_destination_is_retried_alone(workdir):
    client = FakeClient(transcripts={"vidNewest01": transcript_json()})
    http = FakeHttp({("POST", "hooks.slack.test"): Response(500, text="oops")})
    code, _, errors = go(workdir, client, http)
    assert code == 1
    assert errors[0] == "Delivery failed: slack: HTTP 500 oops"

    http = FakeHttp()
    code, lines, _ = go(workdir, client, http, now=NOW + timedelta(hours=1))
    assert code == 0
    assert {r["url"] for r in http.requests} == {ENV["SLACK_WEBHOOK_URL"]}
    assert len(http.requests) == 2


def test_ping_only_on_mentions(workdir):
    path = workdir / "config.yml"
    path.write_text(path.read_text(encoding="utf-8") + "ping: mentions\n", encoding="utf-8")
    client = FakeClient(transcripts={"vidNewest01": transcript_json()})
    http = FakeHttp()
    go(workdir, client, http)
    assert len(notes(workdir)) == 2
    assert len(http.requests) == 4  # only the video that says "Postgres"


def test_missing_key_and_broken_channel(workdir):
    errors: list[str] = []
    code = cli.run(args(workdir), {}, out=lambda _: None, err=errors.append)
    assert code == 2 and "SCRAPEUNBLOCKER_KEY" in errors[0]

    client = FakeClient(broken={"@examplelab"})
    code, _, errors = go(workdir, client, FakeHttp())
    assert code == 1 and errors[0].startswith("Channel failed: @examplelab: ServerError 502")


def test_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\nSCRAPEUNBLOCKER_KEY=abc\nexport SLACK_WEBHOOK_URL='https://x.test'\n"
        "EMPTY=\nnot a line\n",
        encoding="utf-8",
    )
    assert cli.read_env_file(str(path)) == {
        "SCRAPEUNBLOCKER_KEY": "abc",
        "SLACK_WEBHOOK_URL": "https://x.test",
    }
    assert cli.read_env_file(str(tmp_path / "missing")) == {}


def test_check_prints_destinations_and_cost(workdir, capsys):
    code = cli.main(
        ["check", "--config", str(workdir / "config.yml"), "--state", str(workdir / "s.json")]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "destinations on: obsidian" in out
    assert "API calls per run: 1 (one per channel) plus one per new video, at most 11" in out
