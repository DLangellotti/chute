# Chute

Send a file to a Telegram bot, have it land in the right folder on a computer you
control.

Chute is a single Python script with no dependencies. It watches a Telegram bot
for anything you send it, asks which folder the item belongs in, asks what to
call it, and writes it to disk. It polls rather than listening, so it needs no
public URL, no port forwarding and no server.

```
You:  [photo] "q3 pricing table"
Bot:  Image · 412 KB · "q3 pricing table"

      Where does this go?
      [ 📡 Work     ] [ 🏠 Personal ]
      [ 🗂 Misc     ] [ 📂 Other    ]
      [ ✖️ Discard  ]

You:  *taps Work*
Bot:  Filing to Work. Name it, or send - for "q3 pricing table".

You:  -
Bot:  ✅ Work/Attachments/q3 pricing table.jpg
```

It was written for an Obsidian vault, but it knows nothing about Obsidian. Any
folder tree works: a Logseq graph, a NAS share, a Syncthing folder, a plain
`~/Downloads`.

## Install

Requires Python 3.9 or newer. Nothing else.

```
git clone https://github.com/YOU/chute.git
cd chute
./chute setup      # bot token, root folder, starting destinations
./chute install    # runs it now and at login
```

`setup` walks you through creating a bot with
[@BotFather](https://t.me/botfather): send `/newbot`, pick a name, paste the
token back.

It then asks where files should go, offering the folder you ran it from or any
path you type. A `~` is expanded, a relative path is resolved against the current
directory, and a folder dragged in from Finder works whether it arrives quoted or
with its spaces backslash-escaped. If the folder does not exist it offers to
create it. It also offers to make a `chute/` subfolder inside your choice, so
filed items stay together instead of landing loose among whatever is already
there.

Finally it asks you to name the buttons. Type a name, press Enter, and a folder
of that name is created under the root. A folder that already exists is reused,
never overwritten. Press Enter on an empty line when you are done, then message
the bot once so it can record your user id. Sub-paths and per-kind routing are
left to `chute config`.

`install` generates a launchd agent on macOS or a systemd user service on Linux,
pointed at wherever you cloned the repo. On anything else, run `./chute fg`
under your own supervisor.

## Configuring

The whole taxonomy lives in `config.json`. A working config is this short:

```json
{
  "bot_token": "123456:ABC...",
  "allowed_user_ids": [123456789],
  "root": "~/Downloads",
  "destinations": [
    { "label": "🧾 Receipts", "path": "Receipts" },
    { "label": "📸 Screenshots", "path": "Screenshots" }
  ]
}
```

Every destination becomes a button. The label is what you see, including the
emoji. The path is relative to `root`.

### Splitting by file type

Add `by_kind` when one button should send different file types to different
places. The four kinds are `image`, `document`, `media` and `text`. Anything not
listed falls back to `path`.

```json
{
  "label": "📡 Work",
  "path": "Work/Attachments",
  "by_kind": {
    "media": "Media/Work",
    "text": "Work/Inbox"
  }
}
```

Images and PDFs go to `Work/Attachments`, voice notes and video to `Media/Work`,
links and forwarded text to `Work/Inbox` as a Markdown note.

### Date trees

Paths accept `{year}`, `{month}`, `{day}`, `{date}`, `{time}`, `{kind}` and
`{ext}`, expanded when the file is written.

```json
{ "label": "📷 Camera", "path": "Photos/{year}/{month}" }
```

### Naming

```json
"naming": {
  "style": "keep-spaces",
  "lowercase": false,
  "date_prefix": false,
  "max_length": 120
}
```

`style` is `keep-spaces`, `kebab` or `snake`. `date_prefix` puts `YYYY-MM-DD ` in
front of the final name. Characters the filesystem rejects are stripped, and
existing files are never overwritten: a second `Router diagram.png` is saved as
`Router diagram 2.png`.

### Text capture

```json
"text_capture": { "format": "markdown", "frontmatter": true, "tags": ["inbox"] }
```

Links and forwarded messages become a note. With `markdown` you get YAML
frontmatter recording the date, the source and who forwarded it. With `txt` you
get the plain text.

More examples in [`examples/`](examples/).

## Using it

Send a photo, tap a folder, send a name. Send `-` to accept the suggestion.

A caption becomes that suggestion, so captioning the photo as you send it turns
filing into two taps. For documents the suggestion is the original filename. For
links it is the site and the URL slug.

After the first file a `🔁 Work, auto-name` button appears. It reuses the last
folder and skips the name prompt, which is the fast path for a batch of
screenshots. Send a whole album and Chute queues them, filing one at a time and
keeping a live count of how many are still waiting.

`📂 Other folder` takes any path under the root, including the date tokens above.

| What you send | Where it goes |
| --- | --- |
| Photo, image, sticker | `path`, or `by_kind.image` |
| PDF and other documents | `path`, or `by_kind.document` |
| Voice note, audio, video | `path`, or `by_kind.media` |
| Link or forwarded text | `path`, or `by_kind.text`, as a note |

Commands: `/status` for what is pending, `/cancel` to drop it, `/help` for a
summary of your own configuration.

## Running it

```
./chute help       # every command, with what it does
./chute status     # is it alive
./chute log        # follow the log
./chute restart    # after editing config.json
./chute check      # validate config, token and every destination folder
./chute test       # run the test suite
./chute fg         # run in the terminal, for debugging
./chute uninstall  # remove the service, leave config and files alone
```

`check` is worth running after any config edit. It resolves every destination
for every file kind, tells you which folders do not exist yet, and refuses paths
that would escape your root.

Config is looked up in this order: `--config PATH`, then `$CHUTE_CONFIG`, then
`config.json` next to the script, then `$XDG_CONFIG_HOME/chute/config.json`.

## When your machine is off

Chute writes to the disk of the machine it runs on, so it files only while that
machine is awake and online. Nothing is lost in the meantime. Telegram holds
updates a bot has not collected yet and keeps them for **24 hours**. Chute
collects the backlog on the next start and walks through it one item at a time,
oldest first, showing how many are still waiting.

| Machine state | What happens to what you send |
| --- | --- |
| Awake and online | Filed straight away |
| Asleep | Held at Telegram, filed on wake |
| Shut down or logged out | Held at Telegram, filed at next login |
| Online, no connection | Chute reconnects with backoff, then catches up |
| Off for more than 24 hours | Telegram drops it. Gone. |

That 24 hour window is a Bot API limit, not a choice Chute makes, and there is
no way to recover an update past it.

### Which approach to pick

**Send and forget, file later.** The default, and enough for most people. You
send things through the day, the machine picks them up whenever it is next
awake. Works as long as you are not away for more than a day. Clearing a backlog
is quick: send each item with a caption and the `🔁` repeat button files it in
two taps.

**Keep the machine awake.** Reasonable for a desktop or a laptop that lives on a
charger. On macOS:

```
sudo pmset -c sleep 0     # on power, never sleep
sudo pmset -c womp 1      # wake for network access
caffeinate -s             # or just hold it awake for one session
```

The tradeoff is power draw, and on a laptop it is a poor default when running on
battery. `pmset -c` only applies while charging, which is usually what you want.

**Run it somewhere that is always up.** A Raspberry Pi, a NAS or a small VPS.
`chute install` generates a systemd user service on Linux, so this is a config
change rather than a port. The tradeoff is that the destination folders have to
exist on that machine, so you need a sync layer such as Syncthing between it and
wherever you actually read your notes. Worth it if you send things at all hours
or travel without the machine; overkill otherwise.

If you know the machine will be off for more than a day, the simplest answer is
to send the items once it is back rather than trusting the queue.

## Limits worth knowing

Telegram's Bot API refuses to hand a bot any file over 20 MB. Chute reports that
rather than failing quietly, but it cannot work around it.

Photos sent the ordinary way are recompressed by Telegram and lose their
filename. Send as a *file* to keep the original.

One instance per bot token. Chute takes a local lock and detects Telegram's
conflict response, so a second copy exits with a message instead of fighting the
first over the update queue.

## Security

Chute writes files to your disk on receipt of a Telegram message. Read
[SECURITY.md](SECURITY.md) before pointing it at anything you care about. The
short version: `allowed_user_ids` is the only thing standing between the internet
and your filesystem, so keep it accurate and keep the token secret.

## License

MIT. See [LICENSE](LICENSE).
