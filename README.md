# Chute

Send anything to a Telegram bot, have it land in a folder on your own
computer. One Python file, no dependencies. It polls, so it needs no server,
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

Send a photo, file, voice note or link and it lands in your Inbox at once.
The reply carries your folder buttons: tap one to move it, tap 🗑 to delete.
Nothing waits, so a backlog is filed by the time you look.

Files are named `2026-08-20 1848 Image.jpg`, or by your caption. A forwarded
item keeps its sender and source link in a note beside it. Chute files only
while the computer is awake; Telegram holds anything sent for 24 hours.

## Transcripts

Send a recording or a YouTube link and the reply offers 📝 Transcribe. The
words go into that file's note. Speech to text runs here, with whisper.cpp:
the recording never leaves the computer.

Install a diarizer and each speaker is marked. Turn summaries on and each
transcript gains a headline and bullets — that one sends the words to
Anthropic, and is off until asked for.

## Commands

| | |
| --- | --- |
| `./chute status` | is it running |
| `./chute log` | watch files arrive |
| `./chute config` | folders, root, users, token |
| `./chute restart` | apply changes |
| `./chute check` | validate settings, and say what leaves |
| `./chute help` | everything else |

In Telegram: `/history` and `/help`.

[SETTINGS.md](SETTINGS.md): setup and every config key.
[SECURITY.md](SECURITY.md): the threat model. MIT licensed.
