# Chute

Send anything to a Telegram bot, have it land in a folder on your own computer.
One Python file, no dependencies. It polls Telegram, so it needs no server,
public URL or open port.

## Install

Python 3.9+ is the only requirement.

```
git clone https://github.com/YOU/chute.git
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

Chute files only while your computer is awake. Telegram holds what you send for
24 hours, and Chute collects it on waking.

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
`./chute config`. Per-type routing, filename style, note format and the security
limits are JSON only; see `examples/`.

[SECURITY.md](SECURITY.md) covers the threat model. MIT licensed.
