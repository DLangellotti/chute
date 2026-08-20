# Chute

Send a file to a Telegram bot, have it land in the right folder on a computer you
control.

Chute is a single Python script with no dependencies. Send its Telegram bot
anything: photos, files, audio, video, links, forwarded messages. Tap a folder
button and it is written to that folder on your machine. It polls rather than
listening, so it needs no public URL, no port forwarding and no server.

```
You:  [photo] "q3 pricing table"
Bot:  Image · 412 KB · "q3 pricing table"

      Where does this go?
      [ 📡 Work     ] [ 🏠 Personal ]
      [ 🗂 Misc     ] [ ✖️ Discard  ]

You:  *taps Work*
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
path you type. A `~` is expanded, and a folder dragged in from Finder works
whether it arrives quoted or with its spaces backslash-escaped. If the folder
does not exist it offers to create it. (When you run setup from Chute's own
folder, it suggests a fresh `~/Chute` instead, so files never mix with the
program.)

Then come the buttons. If the folder already has folders inside it, each one is
offered as a button: turn on the ones you want. If it is empty, a starter list
(Inbox, Photos, Receipts...) is offered instead, and you can type names of your
own alongside. After that, type any further names, one per line; each becomes a
folder under the root, and folders that already exist are reused, never
overwritten. Press Enter on an empty line when you are done, then message the
bot once so it can record your user id. Sub-paths and per-kind routing are left
to `chute config`.

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

Files name themselves: date, time and type, like `2026-08-20 1848 Image.jpg`
or `2026-08-20 1851 Document.pdf`. A caption overrides that, so captioning a
photo `contract p3` files it as `contract p3.jpg`. A second file under the
same name gets a numeric suffix rather than overwriting anything.

```json
"naming": {
  "style": "keep-spaces",
  "lowercase": false,
  "max_length": 120
}
```

`style` is `keep-spaces`, `kebab` or `snake` and shapes how those names are
written (`2026-08-20-1848-image.jpg` under kebab plus lowercase).

### Text capture

```json
"text_capture": { "format": "markdown", "frontmatter": true, "tags": ["inbox"] }
```

Links and forwarded messages become a note. With `markdown` you get YAML
frontmatter recording the date, the source and who forwarded it. With `txt` you
get the plain text.

More examples in [`examples/`](examples/).

## Using it

Send anything, tap a folder. That is the whole flow: the file is saved the
moment you tap, named by date, time and type (`2026-08-20 1848 Image.jpg`). A
caption overrides the name, as in the example above. A taken name gets a
numeric suffix rather than overwriting.

Send a whole album and Chute queues it, filing one item per tap and keeping a
live count of how many are still waiting.

### Changing your mind

`✖️ Discard` drops the item before filing.

Every confirmation carries `↩️ Undo`, and `/undo` works too. Undo takes the file
back out of your folder tree and puts the item at the front of the queue, so you
can file it somewhere else. It only ever touches the last thing filed, and only
while that file is exactly as Chute wrote it: if the size or the modification
time has changed, or you have moved or renamed it, Chute says so and leaves it
alone. Anything else is a job for your file manager.

| What you send | Where it goes |
| --- | --- |
| Photo, image, sticker | `path`, or `by_kind.image` |
| PDF and other documents | `path`, or `by_kind.document` |
| Voice note, audio, video | `path`, or `by_kind.media` |
| Link or forwarded text | `path`, or `by_kind.text`, as a note |

Commands: `/history` for what was filed where and when, `/status` for what is
pending, `/cancel` to drop it, `/help` for a summary of your own configuration.

## Changing settings

Everything lives behind one menu; no file editing needed:

```
./chute config
```

Folders and buttons, the root folder, filename style, how notes are saved, who
may use the bot, the privacy switches, and the bot token itself. It validates
before saving, so a broken combination is refused with a reason rather than
saved.

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

## Moving to a new computer

If you still have the old `config.json`, no setup is needed. Copy it into the
Chute folder on the new machine, then:

```
./chute check      # confirms the token, folders and settings still hold
./chute install
```

If the files folder lives at a different path on the new machine, `check` says
so and `./chute config` lets you point at the new location without losing your
buttons.

Without the old config, run `./chute setup` as usual, but don't create a second
bot: send BotFather `/mybots`, pick your bot, and choose API Token to get the
same token again. If the folder tree came across too, setup offers every folder
it finds as a button, so rebuilding takes a few keystrokes.

Either way, stop Chute on the old machine first (`./chute stop` there). Telegram
lets one computer hold a bot at a time; setup on the new machine says so plainly
if the old one is still connected. Two computers filing at once needs two bots,
one config each.

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
is quick: one tap files each item.

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
