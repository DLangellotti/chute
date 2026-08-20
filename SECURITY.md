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

## What is enforced

**Paths cannot escape the root.** Every destination path from the config, and
every folder path typed into `📂 Other folder`, is resolved and checked for
containment before anything is written. Symlinks are resolved first, so a symlink
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

**Existing files are never overwritten.** A name that is already taken gets a
numeric suffix.

**One instance per token.** A local pid lock plus Telegram's own 409 conflict
response stop two copies from fighting over the update queue.

**Size is capped.** `security.max_file_mb` defaults to 20, which is also the
ceiling the Telegram Bot API imposes on what a bot may download. Chute clamps to
whichever is lower.

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

`📂 Other folder` lets an authorised user write anywhere under the root. Set
`"security": {"allow_custom_paths": false}` to remove the button and restrict
writes to the configured destinations.

## Reporting

Open a GitHub issue for anything non-sensitive. For a vulnerability that should
not be public, use GitHub's private security advisory form on the repository
rather than the public tracker.
