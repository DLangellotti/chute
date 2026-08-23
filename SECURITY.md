# Security

Chute writes files to your disk in response to Telegram messages. That is the
whole point of it, and it is also the reason to read this page before running it
against a folder you care about.

## The trust boundary

Anyone can find a Telegram bot and message it. The only thing separating a
stranger from your filesystem is `allowed_user_ids`. Chute refuses every message
and every button press from an id not on that list, and refuses to start at all
if the list is empty.

Keep the bot token secret. The token alone does not let anyone write files,
because the allowlist still applies, but it does let whoever holds it read the
messages sent to that bot and send messages as it. If a token leaks, run
`/revoke` in @BotFather and put the new one in `config.json`.

`config.json` is written `chmod 600` by `setup`. It holds the token in
plain text. Do not commit it. The shipped `.gitignore` already excludes it, along
with `state.json`, the staging folder and the log.

It is also a list of programs Chute will run. `whisper_bin`, `ffmpeg_bin` and
`ytdlp_bin` are paths it executes, and `transcription.diarize_args` is a whole
argv rather than a single path. Nothing there is passed through a shell, and
nothing in it comes from a Telegram message — only from the file itself. But
someone who can write `config.json` can already run what they like as you, and
that was true before diarization existed.

## Files are written before anyone approves them

This is the change most worth understanding. Chute writes what it receives into
your Inbox folder the moment it arrives; the buttons on the reply move a file
that already exists. Nothing waits for a human.

So an id wrongly present in `allowed_user_ids` no longer merely gets to *ask*
for a file to be filed. Everything it sends lands in your Inbox unattended,
bounded only by the extension blocklist, the 20 MB cap and root containment
below. Keep that list to accounts you control, and treat the Inbox as the one
folder a mistake can fill.

## What is enforced

**Paths cannot escape the root.** Every destination path from the config is
resolved and checked for containment before anything is written. Symlinks are resolved first, so a symlink
inside the root cannot be used to step outside it. `chute check` runs the same
validation, so a bad config is caught before the bot starts.

**Dot-folders are refused.** No path may reach a component beginning with `.`
below the root. That keeps `.git`, `.ssh` and editor config folders out of reach
even when the root is a repository.

**Executable file types are refused by default.** `.app`, `.command`, `.exe`,
`.scpt`, `.desktop` and similar are rejected before the download completes,
because they are the extensions that do something when a person double-clicks
them in a file browser. Override with `security.blocked_extensions` if you have a
reason to.

**Filenames are sanitised.** Path separators, control characters and characters
the filesystem rejects are stripped. So are Unicode bidirectional override
characters, which can otherwise make `photo‮gpj.exe` render as
`photo‮exe.jpg` in a file browser and hide the real extension. Ordinary
right-to-left text is untouched. Windows device names get a trailing underscore.

**Delete is the only thing that removes a file, and it is strict.** 🗑 unlinks
a file only while it is exactly the one Chute wrote: same path, same size, same
modification time. Edit it, move it or rename it and the tap is refused with the
reason. Nothing else in Chute deletes from your tree.

**Moving is deliberately more permissive than deleting.** A tap on a folder
button moves a file whose bytes have changed since Chute wrote it, because
cropping a photo in place should not strip its message of every button forever.
It still refuses to chase a file that has been moved or renamed by hand.

**Existing files are never overwritten.** A name that is already taken gets a
numeric suffix.

**One instance per token.** A local pid lock plus Telegram's own 409 conflict
response stop two copies from fighting over the update queue.

**Size is capped.** `security.max_file_mb` defaults to 20, which is also the
ceiling the Telegram Bot API imposes on what a bot may download. Chute clamps to
whichever is lower.

## Transcription

Transcription is the part of Chute that runs other programs and, for a YouTube
link, reaches out to a third party. It is worth knowing what that means.

**The recording never leaves the computer.** whisper.cpp runs locally against a
local model file, and a diarizer, if you install one, is another local program
reading the same audio. No transcription service is involved and no audio is
uploaded.

**A YouTube link is fetched by yt-dlp.** That contacts YouTube from your
address, and downloads what the link points to. Only links you send the bot
yourself are ever fetched, and only when you tap the button. What is fetched is
never the text you sent: an eleven character video id is read out of it and a
canonical `youtube.com/watch?v=` URL is rebuilt around that, so nothing else in
the message reaches yt-dlp.

**Nothing is passed to a shell.** Every external program is run with an argument
list, never a command string, so a URL or a filename cannot become a command.

**It only runs on a tap.** Arriving audio is filed and nothing more. A
transcription starts when you press the button, on a thread of its own, and a
long one cannot stall the filing of anything else.

**Whisper writes only into the staging folder.** Its temporary audio and JSON
go there and are removed when the job ends, whether it worked or not. The
transcript itself is written into the same folder as its file, through the same
containment and naming rules as everything else.

**Duration is capped.** `transcription.max_minutes` defaults to 240. A longer
recording is refused rather than left grinding.

**It is optional and it is off when absent.** Missing binaries mean the button
never appears; `transcription.enabled: false` turns it off with them present.

## A Bot API server of your own

Optional, off by default, and the one part of Chute that puts a service on your
machine. Worth reading before turning it on.

**It holds the bot token and the api_hash.** The container is given both. The
token lets whoever has it read everything sent to the bot; the api_hash belongs
to your Telegram account, not just the bot. Keep them out of shell history and
out of git.

**It listens on localhost only** as shipped, bound to `127.0.0.1:8081`. Do not
publish that port. There is no authentication in front of it beyond the token
in the URL path.

**Everything sent passes through a folder on disk.** The server writes each
file into `~/.telegram-bot-api` before Chute sees it. Chute moves the file out
rather than copying, so it does not linger, but anything the server wrote and
Chute never collected stays there until removed.

**Chute trusts the path the server reports.** It is translated from the
container prefix and read from disk. That is a local service you started, so
the trust is the same trust you place in the container image.

**Logging out is not reversible for 10 minutes.** `chute logout` deregisters
the bot from Telegram's servers, and they refuse it for 10 minutes afterwards.
Messages sent in that window are lost rather than queued.

**The size cap is yours alone now.** Telegram's 20 MB ceiling no longer
applies, so `security.max_file_mb` is the only thing between an authorised
account and your free disk space.

## What is not

Chute does not scan file contents. If you send it a malicious PDF, it files a
malicious PDF. The blocked extension list stops the obvious double-click
hazards, not everything.

There is no rate limiting. An authorised user, or anyone who has taken over an
authorised user's Telegram account, can fill the disk.

There is no encryption at rest and no audit trail beyond the log.

Telegram is not end-to-end encrypted for bot chats. Everything you send passes
through Telegram's servers in a form they can read. Do not use Chute to move
material where that matters.

Writes only ever go to the folders named in the config. There is no way to
type a path from Telegram, so a compromised account can misfile within your
configured folders but cannot reach outside them.

## Reporting

Open a GitHub issue for anything non-sensitive. For a vulnerability that should
not be public, use GitHub's private security advisory form on the repository
rather than the public tracker.
