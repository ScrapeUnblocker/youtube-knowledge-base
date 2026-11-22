from pathlib import Path

import pytest

from youtube_knowledge_base import config as cfg

ROOT = Path(__file__).resolve().parent.parent


def test_shipped_config_loads_with_working_defaults():
    c = cfg.load(ROOT / "config.yml")
    assert [ch.ref for ch in c.channels] == ["@Fireship", "@3blue1brown", "@lexfridman"]
    lex = c.channels[2]
    assert lex.rules.min_minutes == 20 and lex.tags == ("podcast",)
    assert c.channels[0].rules.min_minutes == 2
    assert c.destinations == cfg.DESTINATIONS
    assert c.obsidian.folder == "vault/YouTube"
    assert c.calls_per_run() == (3, 13)


def test_channel_overrides_keep_the_other_defaults():
    c = cfg.parse(
        {
            "channels": [{"channel": "@a", "exclude": ["live"], "language": "de"}, "@b"],
            "videos": {"min_minutes": 5, "exclude": ["#shorts", "trailer"]},
            "destinations": {"obsidian": None},
        }
    )
    a, b = c.channels
    assert a.rules.min_minutes == 5 and a.rules.exclude == ("live",) and a.language == "de"
    assert b.rules.exclude == ("#shorts", "trailer") and b.language is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"destinations": {"obsidian": {}}}, "channels: list at least one channel"),
        ({"channels": ["@a"], "destinations": {}}, "turn on at least one"),
        ({"channels": ["@a"], "destinations": {"slak": {}}}, "did you mean slack?"),
        ({"channels": ["@a", "@A"], "destinations": {"obsidian": {}}}, "listed twice"),
        (
            {"channels": ["@a"], "videos": {"per_chanel": 3}, "destinations": {"obsidian": {}}},
            "did you mean per_channel?",
        ),
        (
            {"channels": ["@a"], "videos": {"per_channel": 500}, "destinations": {"obsidian": {}}},
            "videos.per_channel: use a number from 1 to 200",
        ),
        ({"channels": ["@a"], "ping": "sometimes", "destinations": {"obsidian": {}}}, "ping:"),
        (
            {"channels": ["@a"], "destinations": {"obsidian": {"repo": "no-slash"}}},
            "use owner/name",
        ),
        (
            {
                "channels": [{"channel": "@a", "min_minutes": 10, "max_minutes": 5}],
                "destinations": {"obsidian": {}},
            },
            "max_minutes is below min_minutes",
        ),
    ],
)
def test_mistakes_get_a_clear_message(data, message):
    with pytest.raises(cfg.ConfigError, match=message.replace("?", r"\?")):
        cfg.parse(data)


def test_missing_file(tmp_path):
    with pytest.raises(cfg.ConfigError, match="file not found"):
        cfg.load(tmp_path / "nope.yml")
