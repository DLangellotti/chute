# Chute

Send anything to a Telegram bot, have it land in a folder on your own computer.
One Python file, no dependencies. It polls Telegram, so it needs no server,
public URL or open port.

## Install

Python 3.9+ is the only requirement.

```
git clone https://github.com/DLangellotti/chute.git
cd chute
./chute setup      # bot token, folders, your Telegram account
./chute install    # start now, and at every login
```

## How it works

Send a photo, file, voice note or link. It is saved straight away, into your
Inbox folder. The reply carries your folder buttons:

- tap one to move the file there
- tap another to move it again
- tap 🗑 to delete it

Nothing waits for an answer, so a backlog is already filed by the time you look.

Files are named `2026-08-20 1848 Image.jpg`. Add a caption and that becomes the
filename instead.

Forward a photo or file from another chat and nothing is lost: who it came
from, the source link and the full caption land in a small `.md` note saved
next to the file. The buttons move and delete the pair together, though a note
you have edited survives the 🗑.

Chute files only while your computer is awake. Telegram holds what you send for
24 hours, and Chute collects it on waking.

## Transcripts

Send a voice note, an audio or video file, or a YouTube link, and the reply
carries one more button: 📝 Transcribe. Tap it and the words are added to the
note that file already has, or a note is made for it. One markdown file per
thing you send, never two: a link is already a note, so the words go into it,
and a forwarded recording's note gains a Transcript section under what was
already there. The note is then named for what it is a transcript of and when
it was made, like `Root of Trust transcript 2026-08-23 0012.md`.

A YouTube video is kept, not just its words. It lands next to the note under
the video's own title, and the two move and delete together:

```
Root of Trust.mp4
Root of Trust transcript 2026-08-23 0012.md
```

Reckon on 500 MB to 1 GB for an hour at 1080p. Set `transcription.keep` to
`audio` for the sound alone, or `none` to keep only the words.

The language is worked out from the recording, so nothing has to be set in
advance, and it is recorded in the note's frontmatter along with the duration
and what produced it. YouTube links use the video's own subtitles when it has
them, and fall back to transcribing the audio when it does not.

Speech to text runs on your own computer with whisper.cpp. Nothing is uploaded.
It is optional: without it everything else works the same and the button does
not appear.

```
brew install whisper-cpp ffmpeg yt-dlp
mkdir -p ~/.cache/whisper && curl -L -o ~/.cache/whisper/ggml-large-v3-turbo-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
```

That model is 574 MB and handles about 100 languages. Any `ggml-*.bin` in
`~/.cache/whisper` is found on its own; point at another one with
`transcription.model`. `./chute check` says which one it picked up.

An hour of audio takes a few minutes on an Apple Silicon Mac, and the bot keeps
filing everything else while it runs.

## Bigger files

Telegram caps what a bot may download at 20 MB. That is their limit, not a
setting here, and no config raises it: about two minutes of video. The way past
it is to run a Bot API server of your own, which removes the download limit and
raises sending to 2000 MB. `getFile` then returns a path on disk, so Chute
picks the file up locally and nothing is transferred over HTTP at all.

It is optional and off by default. `service/telegram-bot-api.yml` has the
container and the four steps. Two things to know before starting:

- It needs an `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org),
  which are separate from the bot token, and Docker running whenever Chute is.
- `./chute logout` deregisters the bot from Telegram's servers so yours can
  take it over. Telegram then refuses the bot for 10 minutes, so bring the
  container up first. Anything sent in that window is lost rather than queued.

`./chute check` names whichever server is in use and the size cap that follows
from it.

## Commands

| | |
| --- | --- |
| `./chute status` | is it running |
| `./chute log` | watch files arrive |
| `./chute config` | folders, root, users, token |
| `./chute restart` | apply changes |
| `./chute check` | validate settings |
| `./chute logout` | hand the bot to your own Bot API server |
| `./chute help` | everything else |

In Telegram: `/history` and `/help`.

## Settings

`config.json` sits next to the script, written by `setup` and edited with
`./chute config`. Per-type routing, filename style, note format, transcription
and the security limits are JSON only; see `examples/`.

Transcription settings, all optional:

| | |
| --- | --- |
| `enabled` | `false` turns the button off entirely |
| `model` | path to a `ggml-*.bin`, if the ones found are not the one you want |
| `language` | `auto` by default. A code like `he` skips detection |
| `timestamps` | `true` writes `[0:04:12]` against each line |
| `max_minutes` | refuse anything longer. 240 by default |
| `youtube_captions` | `manual` (default), `any` to accept YouTube's automatic ones, `off` to always transcribe the audio |
| `keep` | `video` (default), `audio`, or `none`. What is kept from a YouTube link |
| `max_download_mb` | Refuse a download bigger than this. 2000 by default |
| `prompt` | The opening line shown to whisper. It writes in the style of what it decodes first and carries that forward, so a recording that opens over music can come out as one unpunctuated lowercase run. The default prompt shows ordinary punctuation and settles it. `""` turns it off |
| `threads` | passed to whisper.cpp. Its own default otherwise |
| `whisper_bin`, `ffmpeg_bin`, `ytdlp_bin` | paths, if they are not on `PATH` |

[SECURITY.md](SECURITY.md) covers the threat model. MIT licensed.
