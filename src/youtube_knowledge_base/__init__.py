"""youtube-knowledge-base: every new video from the YouTube channels you follow, saved as a
transcript note in Obsidian, Notion or Qdrant, with a Slack, Discord or Telegram ping.

Collects with the ScrapeUnblocker YouTube channel and transcript APIs
(https://scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos).
"""

from .collect import ChannelInfo, Segment, Transcript, Video, channel_videos, transcript
from .config import Config, ConfigError, load
from .process import Note, build_note, chapters, mentions, paragraphs, passages

__version__ = "0.1.0"

__all__ = [
    "ChannelInfo",
    "Config",
    "ConfigError",
    "Note",
    "Segment",
    "Transcript",
    "Video",
    "__version__",
    "build_note",
    "channel_videos",
    "chapters",
    "load",
    "mentions",
    "paragraphs",
    "passages",
    "transcript",
]
