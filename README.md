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
carries one more button: 📝 Transcribe. Tap it and the words are written as a
markdown note in the same folder as the file. Move the file later and the
transcript goes with it.

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

## Commands

| | |
| --- | --- |
| `./chute status` | is it running |
| `./chute log` | watch files arrive |
| `./chute config` | folders, root, users, token |
| `./chute restart` | apply changes |
| `./chute check` | validate settings |
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
| `threads` | passed to whisper.cpp. Its own default otherwise |
| `whisper_bin`, `ffmpeg_bin`, `ytdlp_bin` | paths, if they are not on `PATH` |

[SECURITY.md](SECURITY.md) covers the threat model. MIT licensed.
