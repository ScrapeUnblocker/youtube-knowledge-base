# YouTube Knowledge Base

[![CI](https://github.com/ScrapeUnblocker/youtube-knowledge-base/actions/workflows/ci.yml/badge.svg)](https://github.com/ScrapeUnblocker/youtube-knowledge-base/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Powered by ScrapeUnblocker](https://img.shields.io/badge/powered%20by-ScrapeUnblocker-6c47ff.svg)](https://scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos)

**For people who learn from YouTube: every new video from the channels you follow is saved
as a searchable transcript note in your Obsidian vault, Notion or a Qdrant vector database,
with a Slack, Discord or Telegram ping. It runs once a day.**

Each note has the creator's chapters, a timestamp link for every paragraph and the moments
your keywords are said. A video is saved once. If its captions are not out yet, the next run
tries again. Everything runs on a GitHub Actions schedule in your own copy of this repository.

## What arrives

The ping. This one was delivered during the smoke test, to a local stand-in for the chat apps:

```text
New from Fireship: If you’ve deployed anything since 2015, I have bad news...
https://www.youtube.com/watch?v=rMaMe32qQkA
5:35 · 20 Nov 2026

Hackers hijacked 178 major websites through Optimizely, a single Base64 string took over Java servers, and a 10-year-old Nginx bug put 225 million websites at risk, all in three days. Let's dive in.

Mentions Nginx: 2:32, 3:02, 3:37

Saved to Obsidian (YouTube/Fireship/2026-11-20 If you’ve deployed anything since 2015, I have bad news.md); Notion (https://www.notion.so/...); Qdrant (6 passages)
```

In Slack, Discord and Telegram the title and every timestamp are links. Each timestamp opens
the video at that second.

The note `vault/YouTube/Fireship/2026-11-20 If you’ve deployed anything since 2015, I have
bad news.md` from the same run, shortened:

```markdown
---
title: "If you’ve deployed anything since 2015, I have bad news..."
channel: "Fireship"
url: "https://www.youtube.com/watch?v=rMaMe32qQkA"
video_id: "rMaMe32qQkA"
published: "2026-11-20"
duration: "5:35"
captions: "English (auto-generated)"
mentions: ["Nginx"]
tags: ["youtube", "fireship", "nginx"]
---

# If you’ve deployed anything since 2015, I have bad news...

![](https://www.youtube.com/watch?v=rMaMe32qQkA)

> Hackers hijacked 178 major websites through Optimizely, a single Base64 string took over ...

## Mentions

- **Nginx** (3x)
  - [2:32](https://www.youtube.com/watch?v=rMaMe32qQkA&t=152s) …some of their exploits break when NGINX is sitting in front of the Java app…
  - [3:02](https://www.youtube.com/watch?v=rMaMe32qQkA&t=182s) …the number of bytes NGINX is allowed to send…

## Transcript

*Captions: English (auto-generated).*

[0:00](https://www.youtube.com/watch?v=rMaMe32qQkA&t=0s) Earlier this week, ...

[1:03](https://www.youtube.com/watch?v=rMaMe32qQkA&t=63s) A bug in Optimizely's signup flow ...

> [!abstract]- Description
> (the video description, folded away)
```

Obsidian plays the embedded video inside the note. When a creator lists chapters in the
description (`0:00 Intro`, `12:30 ...`), they become a `## Chapters` list and the transcript
gets a `###` heading per chapter. A 3-hour podcast from the same run produced 12 chapters
and 204 timestamped paragraphs.

From the terminal, Qdrant finds things by meaning:

```text
$ youtube-knowledge-base search "how did the nginx bug let attackers run code" --limit 3
0.76  Fireship: If you’ve deployed anything since 2015, I have bad news... @ 2:03
      https://www.youtube.com/watch?v=rMaMe32qQkA&t=123s
      The problem is that version has a math bug where if you hand it a short string with ...

0.74  Fireship: If you’ve deployed anything since 2015, I have bad news... @ 4:05
      https://www.youtube.com/watch?v=rMaMe32qQkA&t=245s
      Overwrites a function Engine X runs whenever a connection closes and then hangs up, ...

0.73  Fireship: If you’ve deployed anything since 2015, I have bad news... @ 3:04
      https://www.youtube.com/watch?v=rMaMe32qQkA&t=184s
      And the protocol says that credit can never go above 2 GB. The problem is NGINX ...
```

## Setup in three steps

1. **Use this template.** Click **Use this template > Create a new repository**. Make it
   **private**, because your notes are committed into it.
2. **Add secrets.** Go to Settings > Secrets and variables > Actions > New repository secret.
   Add `SCRAPEUNBLOCKER_KEY` (get a key at
   [scrapeunblocker.com](https://scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos)),
   plus the secrets of any destination you want (see [Destinations](#destinations)). Obsidian
   needs no secret.
3. **Edit `config.yml`.** Put in your channels, the words to watch for and your destinations.
   Then open Actions > **YouTube knowledge base** > **Run workflow**. Tick *dry run* the
   first time to see what would be saved.

After that it runs every day at 06:17 UTC. Without `SCRAPEUNBLOCKER_KEY` the workflow prints a
notice and exits green, so a fresh copy never fails.

**Reading the notes in Obsidian.** Clone your copy and open its `vault/` folder as a vault, or
symlink `vault/YouTube` into the vault you already have. Then let the
[Obsidian Git](https://github.com/Vinzent03/obsidian-git) plugin pull on a timer. If your vault
already lives in its own GitHub repo, set `destinations.obsidian.repo` instead. Each note is
then committed straight into that repo (see below).

### Run it on your machine, from cron or with Docker

Locally, or from cron. Keep secrets in `.env`, which is read automatically and never
committed:

```bash
git clone https://github.com/<you>/<your-copy>.git && cd <your-copy>
python -m venv .venv && . .venv/bin/activate
pip install ".[qdrant]"          # or: pip install .   (without Qdrant)
cp .env.example .env             # add SCRAPEUNBLOCKER_KEY and any destination secrets
youtube-knowledge-base run --dry-run
youtube-knowledge-base run
```

To write straight into a vault on your machine, point the folder at it, for example
`folder: ~/Documents/Notes/YouTube`. A crontab line for every morning at 7:

```cron
0 7 * * * cd /path/to/your-copy && .venv/bin/youtube-knowledge-base run >> run.log 2>&1
```

With Docker:

```bash
docker run --rm -v "$PWD":/app -w /app --env-file .env python:3.13-slim \
  sh -c "pip install -q . && youtube-knowledge-base run"
```

Use one place to run it, either Actions or your machine. Each keeps its own `state/videos.json`.

`--dry-run` fetches the videos and captions and prints exactly what each destination would
get: the note path and text, the Notion block count, the Qdrant passages and every ping. It
saves nothing, sends nothing and leaves the state file alone. Add `--full` to print whole
notes.

## How it works

1. **Collect.** One call per channel reads its newest uploads (the Videos tab, so no Shorts)
   with title, upload time, length, views and description. Then one call per new video fetches
   its captions. Both go through the official Python SDK:
   `client.youtube_channel(...)` and `client.youtube_transcript(...)`.
2. **Pick what is new.** Videos already in `state/videos.json` are skipped, and so are those
   that fail your rules: length, `include` / `exclude` words, age. Live streams and upcoming
   premieres wait until they are out. The **first run of a channel** saves only its
   `first_run` newest uploads (2 by default). It remembers the rest without saving them, so
   your vault does not fill up with the back catalogue.
3. **Wait for captions.** YouTube can add captions hours after an upload. A video without
   captions is checked again on each run until `wait_hours` have passed since the upload.
   After that it is saved with its description only.
4. **Process.**
   * The caption lines are folded into paragraphs of about `paragraph_seconds`. Each
     paragraph breaks after a full stop and gets a link to that second of the video.
   * Chapters come from the description, following YouTube's own rule: the first starts at
     0:00, there are at least three, and they run in order.
   * `mentions` are found as whole words in any case, even when a phrase spans two caption
     lines. Hits less than 30 s apart count once.
   * The summary line is the first paragraph of the description that has no links. Sponsor
     blurbs usually carry a link, so this skips them.
5. **Deliver.** Note destinations (Obsidian, Notion, Qdrant) go first, so the ping can say
   where the note landed. Then come the chat pings. If one destination fails, the others still
   get the video, and the next run retries only the one that failed, up to 3 runs.
6. **Remember.** `state/videos.json` records every video seen, once. The workflow commits it
   back to your repository together with the new notes (commit message `Save new videos
   [skip ci]`), so a second run with nothing new sends nothing. To save a video again,
   delete its entry from the file.

## Config reference

Everything is in [`config.yml`](config.yml). Run `youtube-knowledge-base check` after an edit.
It validates the file, names any setting it does not know (and suggests the one you meant),
lists the destinations that are switched on and prints the cost per run.

| Setting | Default | What it does |
| --- | --- | --- |
| `channels` | 3 channels | `@handle`, `UC...` channel id or channel URL. Write a channel as a mapping (`channel: "@name"`) to override any `videos` rule, add `tags`, or set its own caption `language`. |
| `videos.per_channel` | `15` | Newest uploads read per channel per run (1-200, one call either way) |
| `videos.min_minutes` / `max_minutes` | `2` / `0` | Length limits; `0` = no upper limit |
| `videos.include` | `[]` | If set, keep only videos whose title or description has one of these words |
| `videos.exclude` | `["#shorts"]` | Skip videos whose title has one of these |
| `videos.max_age_days` | `14` | Ignore uploads older than this (`0` = no limit) |
| `videos.first_run` | `2` | First run of a channel: save this many newest, remember the rest |
| `videos.max_new_per_run` | `10` | Cap on videos saved per run (one transcript call each); the rest wait for the next run |
| `transcript.language` | `en` | Preferred caption language; falls back to the track the video has |
| `transcript.paragraph_seconds` | `60` | Paragraph length in the note |
| `transcript.wait_hours` | `24` | How long after an upload to keep checking for captions |
| `mentions` | 3 terms | Words and phrases to find in every transcript |
| `ping` | `all` | `all`, `mentions` (ping only when a mention is said; notes are still saved) or `off` |
| `destinations` | all listed | Which destinations to use; see below |

### Destinations

A destination listed under `destinations:` switches on once its secret is set. The others are
skipped with one line in the log. Secrets always come from environment variables, which on
GitHub Actions are repository secrets.

| Destination | Secrets | What it gets |
| --- | --- | --- |
| `obsidian` | none | One Markdown note per video in `folder` (default `vault/YouTube`, one subfolder per channel), committed by the workflow. With `repo: you/your-vault` (and optional `branch`) the note is committed into that repo through the GitHub API instead. That needs `VAULT_GITHUB_TOKEN`, a fine-grained token with *Contents: read and write* on that one repo. |
| `notion` | `NOTION_TOKEN`, `NOTION_DATABASE_ID` | One page per video: the video embedded, the details as columns (Channel, Published, URL, Minutes, Mentions, Video ID; missing columns are added for you), mentions, chapters, the transcript and the description in a toggle. A video already in the database is not added twice. Create an [internal integration](https://www.notion.so/profile/integrations), then share the database with it (*... > Connections*). The id can be pasted as the database link. |
| `qdrant` | `QDRANT_URL`, `QDRANT_API_KEY` | One point per transcript paragraph. The payload has the video, channel, chapter, start second and a timestamp link, ready for RAG or the `search` command. Embeddings are made locally with [fastembed](https://github.com/qdrant/fastembed) (`BAAI/bge-small-en-v1.5` by default), so no embedding API key is needed. Works with a free Qdrant Cloud cluster, or set `path:` for a local database. Install with `pip install ".[qdrant]"`; the workflow does that when `QDRANT_URL` is set. |
| `slack` | `SLACK_WEBHOOK_URL` | The ping, with the thumbnail ([incoming webhook](https://api.slack.com/messaging/webhooks)) |
| `discord` | `DISCORD_WEBHOOK_URL` | The ping as an embed; never pings `@everyone` |
| `telegram` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | The ping with the video preview, to you, a group or a channel (`TELEGRAM_API_BASE` for a self-hosted Bot API server) |
| `webhook` | `WEBHOOK_URL` | A JSON POST per video with the full transcript text, chapters, mentions with timestamps and where the note was saved, for Zapier, Make, n8n or your own code |

`youtube-knowledge-base test-destinations` sends a short hello to each chat destination. It
also checks that the note destinations can be reached.

## Cost

Each run makes **one API call per channel, plus one per new video** (its captions). With the
default `config.yml` (3 channels):

* a run with no new uploads: **3 calls**;
* each new upload adds **1 call**, and a video waiting for captions adds 1 call per run until
  `wait_hours` have passed;
* the first run saves up to 2 videos per channel: at most **3 + 6 = 9 calls**;
* `videos.max_new_per_run` (10) caps any run at **13 calls**.

On the daily schedule that is about 90 calls a month for the channel checks, plus one call per
video saved. Every run ends with a line like
`Done: 2 of 2 channels read, 2 videos saved. API calls: 4.` The embeddings for Qdrant are made
on the runner and cost nothing.

## Adding another destination

A destination is a small class in
[`src/youtube_knowledge_base/sinks.py`](src/youtube_knowledge_base/sinks.py) with `name`,
`kind` (`"note"` or `"chat"`), `deliver`, `preview` and `test`. For example, a Google Chat
space webhook:

```python
class GoogleChatSink:
    name = "googlechat"
    kind = "chat"

    def __init__(self, url: str, http: Http):
        self.url = url
        self.http = http

    def deliver(self, note, saved):
        call(self.http, "POST", self.url, sink=self.name, json={"text": render.plain(note, saved)})

    def preview(self, note, saved, full=False):
        return render.plain(note, saved)

    def test(self):
        call(self.http, "POST", self.url, sink=self.name, json={"text": CONNECTED})
        return "test message sent"
```

Add `"googlechat"` to `CHAT_SINKS` and `SINK_KEYS` in `config.py`. Then add a branch to
`build()` that reads its secret (`GOOGLE_CHAT_WEBHOOK_URL`), and list `googlechat: {}` under
`destinations:`. A note destination returns a `Saved(label, text, url)` from `deliver`, so
the pings can link to it.

## Development

```bash
make install   # editable install with dev tools and the Qdrant extra
make lint      # ruff check + ruff format --check
make test      # offline tests: the SDK, HTTP and Qdrant are faked, no API credit is spent
```

CI runs lint and tests on Python 3.11 and 3.13.

## Links

* [ScrapeUnblocker](https://scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos), the API behind the collection step
* [ScrapeUnblocker docs](https://docs.scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos): the YouTube channel and transcript endpoints and the Python SDK
* [youtube-search-scraper](https://github.com/ScrapeUnblocker/youtube-search-scraper): the raw-data option if you want YouTube search results as JSON or CSV instead

## License

[MIT](LICENSE). Copyright (c) 2026 ScrapeUnblocker.
