#!/usr/bin/env python3
"""
Chute - send a file to a Telegram bot, it lands in the right folder.

Point it at any folder tree: an Obsidian vault, a Logseq graph, a NAS share, a
plain Downloads folder. Send the bot anything Telegram carries: photos, any
file, audio, video, links, forwarded messages. It lands in your Inbox folder
as it arrives, named by date and type or by your caption, and the buttons on
the reply move it. Send audio, video or a YouTube link and it offers to
transcribe it too, locally with whisper.cpp, marking each speaker if a
diarizer is installed. Summaries are the one optional thing that leaves the
computer, and they are off unless asked for. Polling only, so it works behind
NAT with no public URL.

    chute.py setup     one-time: bot token, root folder, destinations
    chute.py run       long-poll Telegram and file what arrives
    chute.py check     validate config, token and every destination
    chute.py version

Python 3.9+, standard library only. No dependencies.
"""

import collections
import json
import os
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

VERSION = "0.5.0"
APP = "chute"

HERE = Path(__file__).resolve().parent
STAGING = HERE / "staging"
LOCK_PATH = HERE / "chute.lock"
LOG_PATH = HERE / "chute.log"
STATE_PATH = HERE / "state.json"

CLOUD_API = "https://api.telegram.org"
# Telegram's own cap on what a bot may download from its servers. A bot talking
# to a self-hosted Bot API server is not subject to it: there, getFile hands
# back a path on disk and nothing is transferred at all.
CLOUD_CEILING = 20 * 1024 * 1024
TG_LIMIT = 4096                       # Telegram's hard cap on a message
POLL_TIMEOUT = 50                     # seconds Telegram holds getUpdates open
HISTORY_KEEP = 200                    # filings remembered per chat for /history
HISTORY_SHOW = 15                     # lines /history prints at once
FILED_KEEP = 200                      # movable messages remembered per chat
FILED_TTL = 7 * 86400                 # and only for a week


KINDS = ("image", "document", "media", "text")

# Extensions refused by default. These are the ones that do something when a
# person double-clicks them in a file browser. A bot that writes to disk should
# not be able to drop them there.
DEFAULT_BLOCKED_EXT = [
    ".app", ".command", ".scpt", ".scptd", ".workflow", ".pkg", ".dmg",
    ".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".vbs", ".ps1",
    ".jar", ".desktop", ".terminal",
]

# Bidirectional formatting controls. Left in a filename these let text render in
# an order that hides the real extension, the classic filename spoof. Ordinary
# right-to-left script (Hebrew, Arabic) is unaffected: these are the invisible
# override characters only.
BIDI_CONTROLS = "‪‫‬‭‮⁦⁧⁨⁩"

ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
BIDI = re.compile("[%s]" % BIDI_CONTROLS)

# Reserved on Windows. Cheap to avoid, saves grief for anyone syncing there.
RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    *("com%d" % i for i in range(1, 10)),
    *("lpt%d" % i for i in range(1, 10)),
}


# ---------------------------------------------------------------- small helpers

def load_env_file(path=None):
    """KEY=value lines from service/.env into the environment.

    launchd has no EnvironmentFile, and its plist is world readable and gets
    printed by launchctl, so a key carried by the service would be a second
    copy in a worse place. One chmod 600 file instead, read the same way on a
    Mac, a Pi and a VPS. Anything already set wins, so a systemd
    EnvironmentFile or an exported variable still takes precedence.
    """
    path = Path(path) if path else HERE / "service" / ".env"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


class ConfigError(Exception):
    """Raised for anything wrong in config.json, with a message worth reading."""


# What a BotFather token looks like: digits, a colon, then a long code.
TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{25,}$")


def clean_token(raw):
    """Strip the quotes and stray spaces a pasted token often arrives with."""
    return (raw or "").strip().strip("'\"").strip()


# Where package managers put things. Searched after PATH, because a service
# started by launchd or systemd inherits almost none of a login shell's PATH:
# on macOS it is /usr/bin:/bin:/usr/sbin:/sbin, which has no Homebrew in it.
# Without this, an optional helper works when Chute is run from a terminal and
# silently does not exist when it runs the way it is meant to.
BIN_DIRS = [
    "/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
    "~/.local/bin", "/home/linuxbrew/.linuxbrew/bin", "/snap/bin",
]


def which_or_path(name):
    """Find a helper binary: an absolute path as given, else PATH, else the
    usual install folders."""
    if not name:
        return None
    if "/" in str(name):
        path = Path(str(name)).expanduser()
        return path if path.is_file() and os.access(str(path), os.X_OK) else None
    found = shutil.which(str(name))
    if found:
        return Path(found)
    for folder in BIN_DIRS:
        candidate = Path(folder).expanduser() / str(name)
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return candidate
    return None


def cli_name():
    """How to invoke the chute command from where this user sits.

    Every printed follow-up must be typeable as-is: 'chute install' is wrong
    advice for someone who has not linked chute onto their PATH.
    """
    return "chute" if shutil.which("chute") else "./chute"


# ---------------------------------------------------------------- paths

def safe_join(root, relative):
    """Join a config- or user-supplied relative path onto root, safely.

    Refuses anything that escapes root once symlinks are resolved, and refuses
    dot-directories below root so a stray path cannot reach .git, .ssh or an
    editor's config folder.
    """
    # Resolve root here rather than trusting the caller: this is the only thing
    # standing between a config typo and a write outside the tree.
    root = Path(root).resolve()
    rel = str(relative).strip().replace("\\", "/").lstrip("/")
    if not rel or rel == ".":
        return root
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError("path escapes the root folder")
    for part in target.relative_to(root).parts:
        if part.startswith("."):
            raise ValueError("path reaches a dot-folder (%s)" % part)
    return target


TOKENS = ("year", "month", "day", "date", "time", "kind", "ext")


def render_path(template, kind="image", ext=".jpg", now=None):
    """Expand {year} {month} {day} {date} {time} {kind} {ext} in a path template."""
    now = now or datetime.now()
    values = {
        "year": now.strftime("%Y"),
        "month": now.strftime("%m"),
        "day": now.strftime("%d"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H%M"),
        "kind": kind,
        "ext": (ext or "").lstrip("."),
    }
    try:
        return string.Formatter().vformat(template, (), values)
    except (KeyError, IndexError) as exc:
        raise ConfigError(
            "unknown token %s in path %r. Available: %s"
            % (exc, template, ", ".join("{%s}" % t for t in TOKENS)))
    except ValueError as exc:
        raise ConfigError("malformed path template %r: %s" % (template, exc))


# ---------------------------------------------------------------- names

def slugify(text, limit=32):
    """Stable ascii key for callback data, derived from a destination label."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:limit] or "dest"


def clean_name(raw, naming=None, fallback=None):
    """Turn arbitrary text into a filename that is safe on disk."""
    naming = naming or {}
    if raw is None:
        raw = ""
    name = unicodedata.normalize("NFC", str(raw))
    name = name.replace("\n", " ").replace("\t", " ")
    name = BIDI.sub("", name)
    name = ILLEGAL.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")

    style = naming.get("style", "keep-spaces")
    if style == "kebab":
        name = re.sub(r"\s+", "-", name)
    elif style == "snake":
        name = re.sub(r"\s+", "_", name)
    if naming.get("lowercase"):
        name = name.lower()

    limit = int(naming.get("max_length", 120))
    if len(name) > limit:
        name = name[:limit].rstrip()
    if name.lower() in RESERVED_STEMS:
        name += "_"
    if not name:
        name = fallback or datetime.now().strftime("%Y-%m-%d %H%M")
    return name


def unique_path(directory, stem, ext):
    """First free path of the form <stem><ext>, <stem> 2<ext>, <stem> 3<ext>..."""
    candidate = directory / (stem + ext)
    n = 2
    while candidate.exists():
        candidate = directory / ("%s %d%s" % (stem, n, ext))
        n += 1
    return candidate


def ext_of(filename, default=""):
    if not filename:
        return default
    suffix = Path(filename).suffix
    if 1 < len(suffix) <= 12:
        return suffix.lower()
    return default


# ---------------------------------------------------------------- config

DERIVED_INBOX = {"label": "📥 Inbox", "path": "Inbox", "catch_all": True}


def resolve_inbox(destinations, taken_keys):
    """The folder everything lands in. Returns (Destination, was_derived).

    An explicit catch_all wins. Failing that a button already called Inbox is
    used, since that is plainly what its owner meant it for. Failing that one
    is invented, so a config written before this existed keeps working and its
    files land somewhere new and obvious rather than mixed into a folder that
    already means something.
    """
    flagged = [d for d in destinations if d.catch_all]
    if len(flagged) > 1:
        raise ConfigError(
            'only one folder can be the landing folder, but %d have '
            '"catch_all": true: %s'
            % (len(flagged), ", ".join(d.label for d in flagged)))
    if flagged:
        return flagged[0], False
    for dest in destinations:
        if dest.key == "inbox":
            return dest, True
    return Destination(DERIVED_INBOX, -1, taken_keys), True


class Destination:
    def __init__(self, raw, index, taken_keys):
        if not isinstance(raw, dict):
            raise ConfigError("destination %d must be an object" % index)
        self.label = str(raw.get("label") or "").strip()
        if not self.label:
            raise ConfigError("destination %d needs a label" % index)
        self.path = str(raw.get("path") or "").strip()
        if not self.path:
            raise ConfigError("destination %r needs a path" % self.label)
        self.by_kind = raw.get("by_kind") or {}
        if not isinstance(self.by_kind, dict):
            raise ConfigError("by_kind for %r must be an object" % self.label)
        for kind in self.by_kind:
            if kind not in KINDS:
                raise ConfigError(
                    "destination %r: unknown kind %r. Use one of %s"
                    % (self.label, kind, ", ".join(KINDS)))
        key = raw.get("key") or slugify(self.label)
        base = key
        n = 2
        while key in taken_keys:
            key = "%s-%d" % (base, n)
            n += 1
        taken_keys.add(key)
        self.key = key
        # The folder everything lands in on arrival. Exactly one destination
        # carries this; it still gets a button, so a file can be moved back.
        self.catch_all = bool(raw.get("catch_all"))

    def template_for(self, kind):
        return self.by_kind.get(kind, self.path)

    def templates(self):
        return [self.path] + list(self.by_kind.values())


class Config:
    def __init__(self, data, source=None):
        self.source = source
        self.token = str(data.get("bot_token") or "").strip()
        if not self.token:
            raise ConfigError("bot_token is empty. Run: chute setup")
        ids = data.get("allowed_user_ids") or []
        if not isinstance(ids, list) or not ids:
            raise ConfigError(
                "allowed_user_ids is empty. Without it anyone who finds the bot "
                "could write files to your disk. Run: chute setup")
        try:
            self.allowed = set(int(x) for x in ids)
        except (TypeError, ValueError):
            raise ConfigError("allowed_user_ids must be numbers")

        self.api_root = str(data.get("api_root") or CLOUD_API).rstrip("/")
        self.local_api = self.api_root != CLOUD_API
        mapping = data.get("local_api") or {}
        self.files_from = str(mapping.get("files_from")
                              or "/var/lib/telegram-bot-api")
        self.files_to = str(Path(str(mapping.get("files_to")
                                     or "~/.telegram-bot-api")).expanduser())

        root = data.get("root") or data.get("vault")
        if not root:
            raise ConfigError("root is not set")
        self.root = Path(str(root)).expanduser().resolve()

        dests = data.get("destinations") or []
        if not dests:
            raise ConfigError("destinations is empty. Add at least one folder.")
        taken = set()
        self.destinations = [Destination(d, i, taken) for i, d in enumerate(dests)]
        self.inbox, self.inbox_derived = resolve_inbox(self.destinations, taken)
        if self.inbox_derived and self.inbox not in self.destinations:
            self.destinations.insert(0, self.inbox)
        self.by_key = {d.key: d for d in self.destinations}

        self.naming = data.get("naming") or {}
        style = self.naming.get("style", "keep-spaces")
        if style not in ("keep-spaces", "kebab", "snake"):
            raise ConfigError(
                "naming.style must be keep-spaces, kebab or snake, got %r" % style)

        self.text_capture = data.get("text_capture") or {}
        self.transcribe = Transcriber(data.get("transcription"))
        self.summary = Summariser(data.get("summary"))
        sec = data.get("security") or {}
        self.blocked_ext = set(
            e.lower() if e.startswith(".") else "." + e.lower()
            for e in sec.get("blocked_extensions", DEFAULT_BLOCKED_EXT))
        wanted = int(sec.get("max_file_mb", 20)) * 1024 * 1024
        # Against Telegram's own servers their limit wins whatever the config
        # says. Against a local one there is no ceiling but the configured one.
        self.max_bytes = wanted if self.local_api else min(wanted, CLOUD_CEILING)
        # Silent by default. Replying confirms the bot is live to anyone who
        # finds it, and lets a stranger burn the bot's rate limit with noise.
        self.reply_to_strangers = bool(sec.get("reply_to_strangers", False))

    def validate_paths(self):
        """Render and containment-check every configured path. Returns warnings."""
        warnings = []
        if not self.root.is_dir():
            raise ConfigError("root folder does not exist: %s" % self.root)
        for dest in self.destinations:
            for template in dest.templates():
                for kind in KINDS:
                    rendered = render_path(template, kind=kind)
                    try:
                        safe_join(self.root, rendered)
                    except ValueError as exc:
                        raise ConfigError(
                            "destination %r path %r is not allowed: %s"
                            % (dest.label, template, exc))
        if len(self.destinations) > 12:
            warnings.append(
                "%d destinations. Telegram keyboards get unwieldy past about 12."
                % len(self.destinations))
        for dest in self.destinations:
            if len(dest.label) > 30:
                warnings.append("label %r is long and will wrap on a phone"
                                % dest.label)
        # Everything is written here before anyone taps anything, so a landing
        # folder that cannot be written to is a total failure, not a per-file one.
        landing = self.resolve_dir(self.inbox, "image", ".jpg")
        probe = landing if landing.is_dir() else landing.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not os.access(str(probe), os.W_OK):
            raise ConfigError("the landing folder %s is not writable" % landing)
        if is_template(self.inbox.path):
            warnings.append(
                "the landing folder path %r changes with the date, so unfinished "
                "downloads can only be tidied from today's folder"
                % self.inbox.path)
        if self.inbox_derived:
            warnings.append(
                "no landing folder is set, so things will arrive in %s. "
                "Choose one with: %s config" % (self.inbox.path, cli_name()))
        return warnings

    def resolve_dir(self, dest, kind, ext):
        rendered = render_path(dest.template_for(kind), kind=kind, ext=ext)
        return safe_join(self.root, rendered)


def config_search_path(explicit=None):
    if explicit:
        return [Path(explicit).expanduser()]
    env = os.environ.get("CHUTE_CONFIG")
    if env:
        return [Path(env).expanduser()]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    home_cfg = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return [HERE / "config.json", home_cfg / APP / "config.json"]


def find_config(explicit=None):
    for candidate in config_search_path(explicit):
        if candidate.is_file():
            return candidate
    return None


def load_config(explicit=None):
    path = find_config(explicit)
    if not path:
        looked = ", ".join(str(p) for p in config_search_path(explicit))
        raise ConfigError("no config file found. Looked in: %s\nRun: chute setup"
                          % looked)
    data = load_json(path)
    if data is None:
        raise ConfigError("%s is not valid JSON" % path)
    return Config(data, source=path)


# ---------------------------------------------------------------- telegram api

def tg_escape(text):
    """Text made safe to put in a message sent with parse_mode HTML.

    Telegram parses a handful of tags and rejects the whole message if what it
    is given does not parse. A filename or a summary with an & or a < in it
    would otherwise take the reply down with it.
    """
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def tg_len(text):
    """What Telegram counts: an emoji is two of these, a Hebrew letter one."""
    return len((text or "").encode("utf-16-le")) // 2


def tg_fit(text, limit=TG_LIMIT):
    """A message cut to what Telegram will accept.

    Over the limit the API refuses the whole message, and an edit that fails
    falls through to a send that raises on a worker thread, after the note is
    already written. Losing the tail is the smaller harm, and the … says so.
    """
    text = text or ""
    if tg_len(text) <= limit:
        return text
    # Telegram counts UTF-16, so the number of characters that fit is not the
    # number of units. Binary search for the most of them that do.
    room = limit - 1                      # the … costs a unit of its own
    low, high = 0, min(len(text), room)
    while low < high:
        mid = (low + high + 1) // 2
        if tg_len(text[:mid]) <= room:
            low = mid
        else:
            high = mid - 1
    cut = text[:low]
    # Never stop inside an entity or a tag: half an &amp; or an unclosed
    # <code> fails to parse for a different reason than the one just fixed.
    for mark in ("&", "<"):
        opened = cut.rfind(mark)
        if opened > -1 and not re.match(r"(&\w{2,6};|<[^<>]{0,40}>)",
                                        cut[opened:]):
            cut = cut[:opened]
    if cut.count("<code>") > cut.count("</code>"):
        cut += "</code>"
    return cut.rstrip() + "…"


class TelegramConflict(Exception):
    """Another getUpdates consumer is live for this token."""


class NetworkError(Exception):
    """Could not reach Telegram at all. Distinct from Telegram saying no."""


def explain_network_error(exc):
    """Turn a urllib failure into something that points at the actual cause."""
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return ("TLS verification failed for api.telegram.org.\n"
                "  Usually this means something on the network is intercepting "
                "the connection:\n"
                "  a DNS content filter, a captive portal, or a corporate proxy.\n"
                "  Check with:  dig +short api.telegram.org\n"
                "  If that returns a block-page address rather than 149.154.x.x, "
                "Telegram is filtered on this network.")
    if isinstance(exc, urllib.error.URLError) and "Name or service not known" in text:
        return "DNS could not resolve api.telegram.org."
    if "timed out" in text.lower():
        return "Connection to api.telegram.org timed out."
    return "Could not reach api.telegram.org: %s" % text


class Telegram:
    def __init__(self, token, api_root=CLOUD_API, files_from=None,
                 files_to=None):
        self.token = token
        self.api_root = (api_root or CLOUD_API).rstrip("/")
        # A local server run in a container reports the path it sees. Chute
        # sees the same file at a different prefix on the host.
        self.files_from = files_from
        self.files_to = files_to

    def local_path(self, file_path):
        """Where a local server's file actually is, from Chute's side."""
        if not str(file_path).startswith("/"):
            return None
        if self.files_from and self.files_to:
            prefix = self.files_from.rstrip("/")
            if str(file_path).startswith(prefix + "/"):
                return Path(self.files_to.rstrip("/")
                            + str(file_path)[len(prefix):])
        return Path(file_path)

    def call(self, method, **params):
        url = "%s/bot%s/%s" % (self.api_root, self.token, method)
        body = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        timeout = params.get("timeout", 20) + 15
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # noqa: B014 - ordered before URLError
            detail = {}
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                pass
            if exc.code == 409:
                raise TelegramConflict(detail.get("description") or "conflict")
            if exc.code == 401:
                raise ConfigError("Telegram rejected the bot token.")
            raise RuntimeError("%s failed: HTTP %s %s"
                               % (method, exc.code, detail.get("description", "")))
        except (urllib.error.URLError, OSError) as exc:
            raise NetworkError(explain_network_error(exc))
        if not payload.get("ok"):
            raise RuntimeError("%s failed: %s" % (method, payload.get("description")))
        return payload["result"]

    def send(self, chat_id, text, keyboard=None):
        params = {"chat_id": chat_id, "text": tg_fit(text),
                  "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", **params)

    def edit(self, chat_id, message_id, text, keyboard=None):
        params = {"chat_id": chat_id, "message_id": message_id,
                  "text": tg_fit(text), "parse_mode": "HTML",
                  "disable_web_page_preview": True,
                  "reply_markup": {"inline_keyboard": keyboard or []}}
        try:
            return self.call("editMessageText", **params)
        except (RuntimeError, urllib.error.URLError):
            return None      # a stale or unchanged message is not worth failing on

    def ack(self, callback_id, text=None):
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id,
                      text=text or "")
        except (RuntimeError, urllib.error.URLError):
            pass

    def download(self, file_id, dest, max_bytes):
        info = self.call("getFile", file_id=file_id)
        size = info.get("file_size") or 0
        if size > max_bytes:
            raise ValueError("file is %.1f MB, over the %.0f MB limit"
                             % (size / 1048576.0, max_bytes / 1048576.0))
        path = info["file_path"]
        local = self.local_path(path)
        if local:
            # A local server has already written the file. Move it out rather
            # than copy: in local mode those files are the caller's to clean
            # up, and a 2 GB copy would sit on disk twice.
            if not local.is_file():
                raise ValueError(
                    "the local Bot API server reported %s, which is not there. "
                    "Check that its files folder is shared with Chute." % local)
            try:
                shutil.move(str(local), str(dest))
            except OSError:
                shutil.copyfile(str(local), str(dest))
                try:
                    local.unlink()
                except OSError:
                    pass
            return path
        url = "%s/file/bot%s/%s" % (self.api_root, self.token,
                                    urllib.parse.quote(path, safe="/"))
        with urllib.request.urlopen(url, timeout=180) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh, 256 * 1024)
        return path


# ---------------------------------------------------------------- items

def forward_meta(msg):
    meta = {}
    origin = msg.get("forward_origin") or {}
    if origin:
        kind = origin.get("type")
        if kind == "user":
            u = origin.get("sender_user") or {}
            meta["from"] = " ".join(
                x for x in [u.get("first_name"), u.get("last_name")] if x
            ) or u.get("username")
        elif kind == "chat":
            meta["from"] = (origin.get("sender_chat") or {}).get("title")
        elif kind == "channel":
            ch = origin.get("chat") or {}
            meta["from"] = ch.get("title")
            if ch.get("username") and origin.get("message_id"):
                meta["url"] = "https://t.me/%s/%s" % (ch["username"],
                                                      origin["message_id"])
        elif kind == "hidden_user":
            meta["from"] = origin.get("sender_user_name")
        if origin.get("date"):
            meta["forwarded"] = datetime.fromtimestamp(
                origin["date"]).strftime("%Y-%m-%d")
    urls = re.findall(r"https?://\S+", msg.get("text") or msg.get("caption") or "")
    if urls:
        meta["links"] = urls[:10]
    return meta


def extract_item(msg, tg, staging, cfg):
    """Turn an incoming message into a filing item, downloading any file."""
    caption = (msg.get("caption") or "").strip()
    item = {"id": "%s-%s" % (msg.get("message_id"),
                             int(time.time() * 1000) % 100000),
            "caption": caption,
            "meta": forward_meta(msg)}

    file_id = orig_name = None
    default_ext = ""

    if msg.get("photo"):
        file_id = msg["photo"][-1]["file_id"]     # sizes ascend, last is largest
        item["kind"] = "image"
        default_ext = ".jpg"
    elif msg.get("document"):
        doc = msg["document"]
        file_id, orig_name = doc["file_id"], doc.get("file_name")
        mime = doc.get("mime_type") or ""
        item["kind"] = ("image" if mime.startswith("image/")
                        else "media" if mime.startswith(("audio/", "video/"))
                        else "document")
        default_ext = ".bin"
    elif msg.get("voice"):
        file_id, item["kind"], default_ext = msg["voice"]["file_id"], "media", ".ogg"
    elif msg.get("audio"):
        aud = msg["audio"]
        file_id, orig_name = aud["file_id"], aud.get("file_name")
        item["kind"], default_ext = "media", ".mp3"
        if aud.get("title") and not caption:
            item["caption"] = aud["title"]
    elif msg.get("video"):
        file_id, orig_name = msg["video"]["file_id"], msg["video"].get("file_name")
        item["kind"], default_ext = "media", ".mp4"
    elif msg.get("video_note"):
        file_id, item["kind"], default_ext = msg["video_note"]["file_id"], "media", ".mp4"
    elif msg.get("sticker"):
        file_id, item["kind"], default_ext = msg["sticker"]["file_id"], "image", ".webp"
    elif (msg.get("text") or "").strip():
        item["kind"] = "text"
        item["text"] = msg["text"]
        link = youtube_url(msg["text"])
        if link:
            item["youtube"] = link
        return item
    else:
        return None

    ext = ext_of(orig_name, default_ext)
    if ext in cfg.blocked_ext:
        raise ValueError("%s files are blocked for safety" % ext)

    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / ("%s%s" % (item["id"], ext))
    tg_path = tg.download(file_id, staged, cfg.max_bytes)
    if not ext:
        ext = ext_of(tg_path, default_ext)
        renamed = staged.with_suffix(ext)
        staged.rename(renamed)
        staged = renamed
    if ext in cfg.blocked_ext:
        staged.unlink(missing_ok=True)
        raise ValueError("%s files are blocked for safety" % ext)

    item.update({"staged": str(staged), "ext": ext, "orig_name": orig_name,
                 "size": staged.stat().st_size})
    return item


AUDIO_EXT = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav",
             ".flac", ".wma"}


def kind_word(item):
    """The conventional word for what this is: Image, Document, Audio..."""
    kind = item.get("kind")
    if kind == "image":
        return "Image"
    if kind == "document":
        return "Document"
    if kind == "text":
        return "Note"
    if kind == "media":
        ext = (item.get("ext") or "").lower()
        return "Audio" if ext in AUDIO_EXT else "Video"
    return "File"


def auto_name(item, now=None):
    """Every file is named the same way: date, time, and what it is.

    No naming step in the chat; a second file in the same minute gets a
    numeric suffix from unique_path.
    """
    now = now or datetime.now()
    return "%s %s" % (now.strftime("%Y-%m-%d %H%M"), kind_word(item))


def name_for(item, naming=None):
    """The filename: the sender's caption if there is one, else the auto name.

    A caption is the one deliberate act of naming left in the flow, so it
    wins. A caption that cleans away to nothing falls back to the auto name
    rather than to a bare date stamp with no type word.
    """
    first_line = (item.get("caption") or "").strip().splitlines()
    if first_line:
        candidate = BIDI.sub("", first_line[0])
        if ILLEGAL.sub("", candidate).strip().strip(". "):
            return clean_name(first_line[0], naming)
    return auto_name(item)


# ---------------------------------------------------------------- writing

def note_body(item, title, opts):
    text = (item.get("text") or "").strip()
    # Frontmatter is a markdown convention. A .txt capture should be plain text
    # unless the config explicitly asks otherwise.
    default_frontmatter = opts.get("format", "markdown") != "txt"
    if not opts.get("frontmatter", default_frontmatter):
        return text + "\n"
    meta = item.get("meta") or {}
    lines = ["---", "created: %s" % datetime.now().strftime("%Y-%m-%d"),
             "source: telegram"]
    if meta.get("from"):
        lines.append("forwarded-from: %s" % json.dumps(meta["from"],
                                                       ensure_ascii=False))
    if meta.get("forwarded"):
        lines.append("forwarded-date: %s" % meta["forwarded"])
    tags = opts.get("tags") or ["inbox"]
    if tags:
        lines.append("tags:")
        lines += ["  - %s" % t for t in tags]
    lines += ["---", "", "# %s" % title, ""]
    if meta.get("url"):
        lines += ["Source: %s" % meta["url"], ""]
    lines += [text, ""]
    return "\n".join(lines)


def sidecar_worthy(item):
    """Whether saving just the file would lose part of the message.

    True when the message was forwarded (the origin dies with the Telegram
    chat), when the caption carries a link (filename cleaning mangles URLs),
    or when the caption runs past the first line (only that line becomes the
    filename).
    """
    meta = item.get("meta") or {}
    if meta.get("from") or meta.get("forwarded") or meta.get("url") \
            or meta.get("links"):
        return True
    rest = (item.get("caption") or "").strip().splitlines()[1:]
    return bool("".join(rest).strip())


def sidecar_body(item, filename):
    """A companion note for a media file: the forward origin and the full
    caption, written next to the file so the pair travels together."""
    meta = item.get("meta") or {}
    lines = ["---",
             "created: %s" % datetime.now().strftime("%Y-%m-%d"),
             "source: telegram",
             "file: %s" % json.dumps(filename, ensure_ascii=False)]
    if meta.get("from"):
        lines.append("forwarded-from: %s" % json.dumps(meta["from"],
                                                       ensure_ascii=False))
    if meta.get("forwarded"):
        lines.append("forwarded-date: %s" % meta["forwarded"])
    lines += ["---", ""]
    # Angle brackets keep a filename with spaces valid CommonMark, and
    # Obsidian renders both forms.
    if item.get("kind") == "image":
        lines += ["![](<%s>)" % filename, ""]
    else:
        lines += ["[%s](<%s>)" % (filename, filename), ""]
    if meta.get("url"):
        lines += ["Source: %s" % meta["url"], ""]
    caption = (item.get("caption") or "").strip()
    if caption:
        lines += [caption, ""]
    return "\n".join(lines)


def file_item(item, directory, name, cfg):
    directory.mkdir(parents=True, exist_ok=True)
    stem = clean_name(name, cfg.naming)
    if item["kind"] == "text":
        opts = cfg.text_capture
        ext = ".txt" if opts.get("format") == "txt" else ".md"
        dest = unique_path(directory, stem, ext)
        # The heading comes from the name the file actually got: a second note
        # in the same minute lands as "... Note 2" and must not be titled
        # "... Note".
        dest.write_text(note_body(item, dest.stem, opts), encoding="utf-8")
    else:
        ext = item.get("ext") or ""
        if ext and stem.lower().endswith(ext.lower()):
            stem = stem[: -len(ext)].rstrip() or stem
        dest = unique_path(directory, stem, ext)
        shutil.move(item["staged"], str(dest))
        if sidecar_worthy(item):
            note = unique_path(directory, dest.stem, ".md")
            note.write_text(sidecar_body(item, dest.name), encoding="utf-8")
            item["sidecar"] = str(note)
            item["sidecar_tail"] = note.stem[len(dest.stem):]
    return dest


def discard(item):
    staged = (item or {}).get("staged")
    if staged and os.path.exists(staged):
        try:
            os.remove(staged)
        except OSError:
            pass


def rel_to(root, path):
    """Path as the user thinks of it: relative to their root, if it is inside."""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


class NotAsFiled(Exception):
    """The file is not the one Chute wrote, so Chute will not touch it."""


def verify_filed(record, root, strict=True):
    """Return the file's path, if it is still the one Chute wrote.

    Always refuses when the file has been moved away or deleted. With strict,
    also refuses when the bytes have changed since Chute wrote them: nothing is
    removed on the strength of a stale record. Moving is deliberately lenient,
    because a photo edited in place should still be filable.
    """
    path = Path(record["path"])
    shown = rel_to(root, path)
    try:
        st = path.stat()
    except OSError:
        raise NotAsFiled("<code>%s</code> is not where I left it, so I have "
                         "not touched anything." % shown)
    if strict and (st.st_size != record.get("size")
                   or int(st.st_mtime) != record.get("mtime")):
        raise NotAsFiled("<code>%s</code> has changed since I filed it, so I "
                         "have left it alone." % shown)
    return path


def retitle(text, old, new):
    """Rewrite a note's top heading when its file is renamed under it.

    Only touches a heading that still says exactly what the file used to be
    called, so a heading someone wrote themselves is left alone.
    """
    if not new or old == new:
        return text
    return re.sub(r"(?m)^# %s[ \t]*$" % re.escape(old), "# %s" % new, text, 1)


def rename_to(path, directory, stem):
    """Rename a file to stem within its folder. Returns where it ended up.

    A name already taken gets a numeric suffix, and a file already correctly
    named is left alone rather than renamed to "name 2".
    """
    path = Path(path)
    if path.parent == Path(directory) and path.stem == stem:
        return path
    target = unique_path(directory, stem, path.suffix)
    try:
        path.rename(target)
    except OSError:
        return path
    return target


def sidecar_stat(path, tail=None):
    """The record entry for a companion note: where it is, what it looks like.

    A tail is the part of the note's name that follows the file's own stem,
    empty for the companion note and " transcript" for a transcript. It is how
    the note keeps its meaning when the file it belongs to is renamed on a
    move. A note named independently of the file carries no tail and keeps the
    name it has.
    """
    st = Path(path).stat()
    entry = {"path": str(path), "size": st.st_size, "mtime": int(st.st_mtime)}
    if tail is not None:
        entry["tail"] = tail
    return entry


def sidecars_of(record):
    """Every companion note on a record, reading the pre-0.4 single one too."""
    notes = list(record.get("sidecars") or [])
    legacy = record.get("sidecar")
    if legacy:
        notes.append(dict(legacy, tail=legacy.get("tail", "")))
    return notes


def set_sidecars(record, notes):
    record.pop("sidecar", None)
    if notes:
        record["sidecars"] = notes
    else:
        record.pop("sidecars", None)


def add_sidecar(record, path, tail=None):
    set_sidecars(record, sidecars_of(record) + [sidecar_stat(path, tail)])


def move_sidecar(record, directory, stem):
    """Bring the companion notes along when their file moves. Best effort:
    a note the user renamed or deleted by hand is simply let go."""
    kept = []
    for sc in sidecars_of(record):
        note = Path(sc["path"])
        if not note.is_file():
            continue
        tail = sc.get("tail")
        name = stem + tail if tail is not None else note.stem
        target = unique_path(directory, name, note.suffix)
        try:
            shutil.move(str(note), str(target))
            kept.append(sidecar_stat(target, tail))
        except OSError:
            kept.append(sc)
    set_sidecars(record, kept)


def delete_sidecar(record):
    """Remove the companion notes with their file, unless they were edited.

    An edited note holds the user's own words now, so it survives the 🗑.
    Returns the notes left standing.
    """
    notes = sidecars_of(record)
    set_sidecars(record, [])
    survivors = []
    for sc in notes:
        note = Path(sc["path"])
        try:
            st = note.stat()
        except OSError:
            continue
        if st.st_size != sc.get("size") or int(st.st_mtime) != sc.get("mtime"):
            survivors.append(note)
            continue
        try:
            note.unlink()
        except OSError:
            survivors.append(note)
    return survivors


def move_filed(record, directory, root):
    """Move an already-filed file into directory. Returns its new path.

    Moving into the folder it already sits in is a no-op rather than a rename
    to "name 2": tapping the folder a file is already in means leave it there,
    and a double tap must not multiply the file.
    """
    src = verify_filed(record, root, strict=False)
    directory.mkdir(parents=True, exist_ok=True)
    if src.parent.resolve() == directory.resolve():
        return src
    dest = unique_path(directory, record.get("stem") or src.stem, src.suffix)
    shutil.move(str(src), str(dest))
    move_sidecar(record, directory, dest.stem)
    return dest


def delete_filed(record, root):
    """Remove a filed file, but only if it is untouched since Chute wrote it."""
    path = verify_filed(record, root, strict=True)
    path.unlink()
    return path


def restat(record, path, dest_key):
    """Re-record where the file is and what it looks like, after every write.

    The stat is always read from the file that now exists rather than carried
    across from the source: a move onto a filesystem with coarser timestamps
    lands on a different integer second, and a record that disagreed with the
    disk would refuse every later tap.
    """
    st = path.stat()
    record.update({"path": str(path), "size": st.st_size,
                   "mtime": int(st.st_mtime), "dest": dest_key,
                   "at": int(time.time())})
    return record


def remember(filed, message_id, record, now=None):
    """Store a record against its message, and forget the stale ones.

    Bounded twice over: nothing older than a week, and never more than
    FILED_KEEP messages. Telegram will not let a bot edit a message older than
    48 hours anyway, so a week is already generous.
    """
    now = int(now if now is not None else time.time())
    filed[str(message_id)] = record
    for key, rec in list(filed.items()):
        if now - int(rec.get("at") or 0) > FILED_TTL:
            del filed[key]
    if len(filed) > FILED_KEEP:
        oldest = sorted(filed.items(), key=lambda kv: int(kv[1].get("at") or 0))
        for key, _ in oldest[:len(filed) - FILED_KEEP]:
            del filed[key]
    return filed


# ---------------------------------------------------------------- transcription

# Video counts as transcribable: the sound is pulled out of it first.
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpg", ".mpeg",
             ".wmv", ".3gp", ".ts"}

# youtube.com/watch, youtu.be, /shorts, /live and /embed, with or without the
# tracking parameters a share sheet adds.
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^\s&]*&)*v=|shorts/|live/|embed/|v/)"
    r"|youtu\.be/)([A-Za-z0-9_-]{11})")

# Whisper reports a two-letter code. Written out, a transcript's frontmatter
# says something to a person reading it a year later.
LANGUAGE_NAMES = {
    "ar": "Arabic", "cs": "Czech", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "es": "Spanish", "fa": "Persian",
    "fi": "Finnish", "fr": "French", "he": "Hebrew", "hi": "Hindi",
    "hu": "Hungarian", "id": "Indonesian", "it": "Italian", "ja": "Japanese",
    "ko": "Korean", "nl": "Dutch", "no": "Norwegian", "pl": "Polish",
    "pt": "Portuguese", "ro": "Romanian", "ru": "Russian", "sv": "Swedish",
    "th": "Thai", "tr": "Turkish", "uk": "Ukrainian", "vi": "Vietnamese",
    "yi": "Yiddish", "zh": "Chinese",
}

# Where a whisper.cpp model tends to sit. Checked in order, and within a
# folder the largest model wins, since that is the one deliberately fetched.
MODEL_DIRS = [
    "~/.cache/whisper",
    "~/Library/Application Support/chute/models",
    "~/.local/share/chute/models",
    "/opt/homebrew/share/whisper-cpp/models",
    "/usr/local/share/whisper-cpp/models",
    "/usr/share/whisper.cpp/models",
]

PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")

# Whisper writes in the style of whatever it decodes first, and carries that
# forward as context through the rest of the file. A recording that opens over
# music, or with a stylised cold open, can set it writing in one unpunctuated
# lowercase run for an hour. An initial prompt showing ordinary punctuation
# settles it: on a 50 minute talk this is the difference between 1 full stop
# and 300. It does not change what language is detected, because that is read
# from the audio before any decoding happens, and it does not change the words.
DEFAULT_PROMPT = ("Hello, and welcome. This is a transcript written with full "
                  "punctuation, commas, and capital letters.")


def youtube_url(text):
    """The canonical watch URL for the first YouTube link in some text."""
    match = YOUTUBE_RE.search(text or "")
    return "https://www.youtube.com/watch?v=%s" % match.group(1) if match else None


def language_label(code):
    code = (code or "").strip().lower()
    if not code or code == "auto":
        return ""
    short = code.split("-")[0]
    name = LANGUAGE_NAMES.get(short)
    return "%s (%s)" % (name, short) if name else short


# A line of transcript and everything known about it. Speaker is None until a
# diarizer says otherwise, and words is empty unless whisper was asked for the
# token detail that splitting a line at a change of speaker needs. One type
# rather than a tuple that would have to grow three times, and every place that
# unpacks it break three times with it.
Segment = collections.namedtuple(
    "Segment", "start end text speaker words", defaults=(None, ()))


def whisper_words(chunk):
    """Token times out of one line of whisper.cpp's -ojf JSON.

    Only there to say where inside a line a word starts, so a line spoken by
    two people can be cut between them rather than guessed at. Whisper's own
    special tokens carry no text anyone said and are dropped.
    """
    words = []
    for token in chunk.get("tokens") or []:
        text = token.get("text") or ""
        if not text.strip() or text.startswith("[_"):
            continue
        at = ((token.get("offsets") or {}).get("from") or 0) / 1000.0
        words.append((at, text))
    return tuple(words)


def hhmmss(seconds):
    seconds = int(seconds or 0)
    return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def paragraphs(sentences, width=600):
    """Group a run of transcript text into paragraphs a person can read.

    Speech has no paragraph breaks in it, so this invents them at sentence
    ends, which is the only honest place to put one.
    """
    out, buf = [], ""
    for piece in sentences:
        piece = piece.strip()
        if not piece:
            continue
        buf = (buf + " " + piece).strip()
        if len(buf) >= width and re.search(r"[.!?。？！]['\")\]]?$", buf):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


# Whisper decides where a line ends by punctuation and pauses; a diarizer
# decides where a turn ends by who is making the sound. The two models are
# looking at the same audio and still disagree by tenths of a second, so the
# merge below works in shares of a line rather than in "first turn wins".
SPLIT_MIN_SECONDS = 1.0   # a shorter runner-up is a backchannel, not a turn
DOMINANT_SHARE = 0.75     # one speaker holding this much of a line owns it all
ORPHAN_WINDOW = 2.0       # a line further than this from a turn belongs to nobody


def parse_rttm(raw):
    """Speaker turns out of an RTTM file: [(start, end, label)], in time order.

    RTTM is the one format every diarizer writes, and none of them writes it
    quite the same way, so only the SPEAKER lines are read and only three of
    their ten columns. Anything malformed is skipped rather than raised: a
    diarizer that writes something unexpected must cost the transcript nothing.
    """
    turns = []
    for line in (raw or "").splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[0] != "SPEAKER":
            continue
        label = fields[7]
        if not label or label == "<NA>":
            continue
        try:
            start, length = float(fields[3]), float(fields[4])
        except ValueError:
            continue
        if length <= 0:
            continue
        turns.append((start, start + length, label))
    turns.sort(key=lambda t: (t[0], t[1]))
    return turns


def speaker_names(turns, label="Speaker"):
    """RTTM labels to what a reader sees, numbered by who spoke first.

    A diarizer's own numbering is arbitrary and per-recording: its SPEAKER_00
    is not necessarily the first voice, and is a different person in the next
    file. Someone opening the note expects Speaker 1 to be whoever started.
    """
    names = {}
    for _, _, who in turns:
        if who not in names:
            names[who] = "%s %d" % (label, len(names) + 1)
    return names


def split_text_at(text, fraction):
    """Cut a line in two at the word boundary nearest a point through it.

    The fallback for when there are no word times. Speech rate is near enough
    constant inside one whisper line for a cut by character count to land
    within a word or so of the right place.
    """
    text = text.strip()
    target = max(1, min(len(text) - 1, int(round(len(text) * fraction))))
    before = text.rfind(" ", 0, target)
    after = text.find(" ", target)
    if before == -1 and after == -1:
        return text, ""
    if before == -1:
        cut = after
    elif after == -1:
        cut = before
    else:
        cut = before if target - before <= after - target else after
    return text[:cut].strip(), text[cut:].strip()


def overlap(a_start, a_end, b_start, b_end):
    """Seconds two stretches of time have in common, or zero."""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers(segments, turns, label="Speaker"):
    """Put a name on every line of the transcript.

    A line that runs across a handover is cut in two, or one person is
    credited with the other's words. With no turns at all this returns what it
    was given, which is what makes a missing diarizer cost nothing.
    """
    if not turns:
        return list(segments)
    names = speaker_names(turns, label)
    out = []
    for seg in segments:
        # Caption lines have no times, so they never reach the arithmetic.
        if seg.start is None:
            out.append(seg)
            continue
        start = seg.start
        end = max(seg.end if seg.end is not None else start, start + 0.01)
        shares = {}
        for turn_start, turn_end, who in turns:
            if turn_start >= end:
                break
            got = overlap(start, end, turn_start, turn_end)
            if got > 0:
                shares[who] = shares.get(who, 0.0) + got
        ranked = sorted(shares.items(), key=lambda kv: kv[1], reverse=True)

        if not ranked:
            # Whisper writes lines over music and silence too. One near a turn
            # is the same person either side of a breath; one a minute from
            # anybody speaking should not be put in someone's mouth.
            gap, nearest = None, None
            for turn_start, turn_end, who in turns:
                away = max(turn_start - end, start - turn_end, 0.0)
                if gap is None or away < gap:
                    gap, nearest = away, who
            near = gap is not None and gap <= ORPHAN_WINDOW
            out.append(seg._replace(speaker=names[nearest] if near else None))
            continue

        best, best_share = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if (len(ranked) == 1 or runner_up < SPLIT_MIN_SECONDS
                or best_share / (end - start) >= DOMINANT_SHARE):
            # The common case, and it must not be clever. Two people at once
            # lands here too: whisper wrote one line, so one name goes on it,
            # and it goes to whoever held the floor longest.
            out.append(seg._replace(speaker=names[best]))
            continue

        pieces = split_segment(seg, start, end, turns, names)
        out.extend(pieces if pieces else [seg._replace(speaker=names[best])])
    return out


def split_segment(seg, start, end, turns, names):
    """One line spoken by two people, cut at the handovers inside it."""
    cuts = sorted({t for turn_start, turn_end, _ in turns
                   for t in (turn_start, turn_end) if start < t < end})
    if not cuts:
        return []
    bounds = [start] + cuts + [end]
    pieces = []
    for i in range(len(bounds) - 1):
        piece_start, piece_end = bounds[i], bounds[i + 1]
        if piece_end - piece_start <= 0:
            continue
        who, held = None, 0.0
        for turn_start, turn_end, name in turns:
            got = overlap(piece_start, piece_end, turn_start, turn_end)
            if got > held:
                who, held = name, got
        if who is None:
            # A pause between two turns, landing mid-line. Somebody said these
            # words; the nearest turn is the best guess as to which of them.
            gap = None
            for turn_start, turn_end, name in turns:
                away = max(turn_start - piece_end, piece_start - turn_end, 0.0)
                if gap is None or away < gap:
                    gap, who = away, name
        pieces.append([piece_start, piece_end, names.get(who)])

    if len(pieces) < 2:
        return []
    # Neighbouring pieces the same person holds are one piece again, so a turn
    # boundary that only clipped the edge of a line does not chop it up.
    merged = [pieces[0]]
    for piece in pieces[1:]:
        if piece[2] == merged[-1][2]:
            merged[-1][1] = piece[1]
        else:
            merged.append(piece)
    if len(merged) < 2:
        return []

    texts = cut_text(seg, [(p[0], p[1]) for p in merged], start, end)
    out = []
    for piece, (text, words) in zip(merged, texts):
        if not text:
            return []          # a split that loses a side is worse than none
        out.append(Segment(piece[0], piece[1], text, piece[2], words))
    return out


def cut_text(seg, spans, start, end):
    """The words of a line handed out to the stretches of time it spans."""
    if seg.words:
        out = []
        for span_start, span_end in spans:
            taken = [w for w in seg.words if span_start <= w[0] < span_end]
            out.append((" ".join(t.strip() for _, t in taken).strip(),
                        tuple(taken)))
        # Anything before the first span or after the last (whisper's token
        # times drift a little past its own line) goes to the nearest end.
        early = [w for w in seg.words if w[0] < spans[0][0]]
        late = [w for w in seg.words if w[0] >= spans[-1][1]]
        if early:
            out[0] = ((" ".join(t.strip() for _, t in early) + " "
                       + out[0][0]).strip(), tuple(early) + out[0][1])
        if late:
            out[-1] = ((out[-1][0] + " "
                        + " ".join(t.strip() for _, t in late)).strip(),
                       out[-1][1] + tuple(late))
        return out
    # No token times, so cut by how far through the line each boundary falls.
    # Each share is measured against what is left rather than the whole, since
    # the text shrinks as pieces are taken off the front of it.
    out, rest, at = [], seg.text, start
    for _, span_end in spans[:-1]:
        left = end - at
        share = (span_end - at) / left if left > 0 else 0.5
        taken, rest = split_text_at(rest, min(1.0, max(0.0, share)))
        out.append((taken, ()))
        at = span_end
    out.append((rest.strip(), ()))
    return out


def speaker_blocks(segments):
    """Consecutive lines by the same person: [(name, [segments])].

    A line nobody was named for keeps the block it is in rather than opening a
    nameless one. An unattributed interjection mid-turn is almost always still
    the same person, and a named / unlabelled / named sandwich reads as broken.
    """
    blocks = []
    for seg in segments:
        if blocks and (seg.speaker is None or seg.speaker == blocks[-1][0]):
            blocks[-1][1].append(seg)
            continue
        blocks.append((seg.speaker, [seg]))
    return blocks


# Captioners write ">>" for a change of speaker and ">> NAME:" when they know
# who it is. Left alone those marks land in the note as punctuation.
CAPTION_SPEAKER_RE = re.compile(r"^>>+\s*(?:([^:>]{1,40}):)?\s*(.*)$")


def caption_speakers(lines):
    """(speaker, text) out of the >> marks broadcast captions carry.

    The best attribution Chute will ever have, because a person did it. A
    named mark sets who is talking until the next one; a bare ">>" says only
    that somebody else started, which is a change Chute cannot put a name to.
    """
    out, who = [], None
    for line in lines:
        found = CAPTION_SPEAKER_RE.match(line)
        if found:
            name, rest = found.group(1), found.group(2).strip()
            who = " ".join(w.capitalize() for w in name.split()) if name else None
            if not rest:
                continue
            line = rest
        out.append((who, line))
    return out


def vtt_to_lines(raw):
    """Plain caption lines from a WebVTT file, with the rolling repeats gone.

    YouTube's captions repeat the previous line at the top of the next cue so
    the text scrolls. Kept as-is that doubles every sentence in the file.
    """
    lines, previous = [], ""
    for line in (raw or "").splitlines():
        text = line.strip()
        if (not text or "-->" in text or text.isdigit()
                or text.startswith(("WEBVTT", "Kind:", "Language:", "NOTE",
                                    "STYLE", "REGION"))):
            continue
        text = re.sub(r"<[^>]*>", "", text)
        for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                             ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
            text = text.replace(entity, char)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or text == previous:
            continue
        # A cue that merely extends the one before it: keep only the new part.
        if previous and text.startswith(previous):
            addition = text[len(previous):].strip()
            if addition:
                lines.append(addition)
            previous = text
            continue
        lines.append(text)
        previous = text
    return lines


def explain_ytdlp_error(text):
    """Turn a yt-dlp failure into something worth reading in a chat.

    YouTube blocks the player clients yt-dlp impersonates, and yt-dlp rotates
    to working ones with each release. A 403 on the media almost always means
    the installed copy has fallen behind rather than anything about the video,
    so say that rather than printing the status code.
    """
    lowered = (text or "").lower()
    if "403" in lowered or "forbidden" in lowered:
        return ("YouTube refused the download. This is nearly always yt-dlp "
                "having fallen behind: upgrade it (brew upgrade yt-dlp) and "
                "tap again")
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return ("YouTube wants to check this is not a bot. Upgrading yt-dlp "
                "usually clears it; otherwise the video needs a signed-in "
                "session")
    if "private video" in lowered or "members-only" in lowered:
        return "that video is not public"
    if "video unavailable" in lowered or "removed" in lowered:
        return "that video is not available"
    if "age" in lowered and "confirm" in lowered:
        return "that video is age restricted, so it cannot be fetched"
    if "live event will begin" in lowered or "premieres in" in lowered:
        return "that has not been broadcast yet"
    return None


class TranscribeError(Exception):
    """Something in the transcription chain said no, with a reason to show."""


class Transcriber:
    """Speech to text on this computer: whisper.cpp, with yt-dlp for YouTube.

    Neither binary is a dependency of Chute. When they are missing the button
    never appears and everything else works exactly as before.
    """

    def __init__(self, data=None):
        data = data or {}
        self.enabled = data.get("enabled", True) is not False
        self.whisper = which_or_path(data.get("whisper_bin") or "whisper-cli")
        self.ffmpeg = which_or_path(data.get("ffmpeg_bin") or "ffmpeg")
        self.ytdlp = which_or_path(data.get("ytdlp_bin") or "yt-dlp")
        self.model = self.find_model(data.get("model"))
        self.language = str(data.get("language") or "auto").strip() or "auto"
        self.threads = int(data.get("threads") or 0)
        prompt = data.get("prompt", DEFAULT_PROMPT)
        self.prompt = "" if prompt is None else str(prompt)
        self.timestamps = bool(data.get("timestamps"))
        self.max_minutes = int(data.get("max_minutes") or 240)
        self.max_download_mb = int(data.get("max_download_mb") or 2000)
        keep = str(data.get("keep", "video")).lower()
        if keep in ("false", "no"):
            keep = "none"
        if keep not in ("none", "audio", "video"):
            raise ConfigError(
                "transcription.keep must be none, audio or video, got %r"
                % data.get("keep"))
        self.keep = keep
        captions = str(data.get("youtube_captions") or "manual").lower()
        if captions in ("true", "yes"):
            captions = "manual"
        if captions in ("false", "no"):
            captions = "off"
        if captions not in ("manual", "any", "off"):
            raise ConfigError(
                "transcription.youtube_captions must be manual, any or off, "
                "got %r" % data.get("youtube_captions"))
        self.captions = captions
        self.diarize_on = data.get("diarize", False) is True
        self.diarizer = which_or_path(data.get("diarize_bin") or "diarize")
        args = data.get("diarize_args") or ["{wav}", "{rttm}"]
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ConfigError(
                "transcription.diarize_args must be a list of strings, got %r"
                % (data.get("diarize_args"),))
        joined = " ".join(args)
        for slot in ("{wav}", "{rttm}"):
            if slot not in joined:
                raise ConfigError(
                    "transcription.diarize_args has to say %s somewhere, or "
                    "there is nowhere to %s" % (slot, "put the audio"
                                                if slot == "{wav}"
                                                else "read the answer"))
        self.diarize_args = args
        self.diarize_name = str(data.get("diarize_label") or "").strip()
        self.speakers = int(data.get("speakers") or 0)
        self.speaker_label = (str(data.get("speaker_label")
                                  or "Speaker").strip() or "Speaker")

    @staticmethod
    def find_model(configured):
        """The whisper model file: the configured one, or the biggest found."""
        if configured:
            path = Path(str(configured)).expanduser()
            return path if path.is_file() else None
        env = os.environ.get("WHISPER_MODEL")
        if env and Path(env).expanduser().is_file():
            return Path(env).expanduser()
        for folder in MODEL_DIRS:
            try:
                found = sorted(
                    (p for p in Path(folder).expanduser().glob("ggml-*.bin")
                     if p.is_file()),
                    key=lambda p: p.stat().st_size, reverse=True)
            except OSError:
                continue
            if found:
                return found[0]
        return None

    def env(self):
        """The environment for a helper: PATH with our own finds on the front.

        yt-dlp looks up ffmpeg and ffprobe by name. Under a service PATH it
        would not find them even though Chute just did.
        """
        env = dict(os.environ)
        folders = [str(b.parent) for b in (self.whisper, self.ffmpeg, self.ytdlp)
                   if b]
        seen, ordered = set(), []
        for folder in folders + env.get("PATH", "").split(os.pathsep):
            if folder and folder not in seen:
                seen.add(folder)
                ordered.append(folder)
        env["PATH"] = os.pathsep.join(ordered)
        return env

    def engine_label(self):
        return "whisper.cpp %s" % (self.model_name() or "whisper")

    def model_name(self):
        if not self.model:
            return ""
        return re.sub(r"^ggml-", "", self.model.stem)

    def audio_ready(self):
        return bool(self.enabled and self.whisper and self.model and self.ffmpeg)

    def diarize_ready(self):
        return bool(self.enabled and self.diarize_on and self.diarizer)

    def diarize_label(self):
        return self.diarize_name or (self.diarizer.name if self.diarizer
                                     else "a diarizer")

    def diarize_missing(self):
        """What stands between asking for names and getting them.

        Kept apart from missing(), which is only ever read to explain why the
        transcribe button is absent. A diarizer nobody installed must never
        make a transcript look unavailable.
        """
        if not self.diarize_on or self.diarizer:
            return []
        return ["a diarizer that writes RTTM. See \"Who is speaking\" in the "
                "README"]

    def youtube_ready(self):
        if not (self.enabled and self.ytdlp):
            return False
        return self.captions != "off" or self.audio_ready()

    def missing(self):
        """What is not installed, in the order worth fixing it."""
        gaps = []
        if not self.whisper:
            gaps.append("whisper-cli (brew install whisper-cpp)")
        if not self.model:
            gaps.append("a whisper model in ~/.cache/whisper "
                        "(ggml-large-v3-turbo-q5_0.bin from "
                        "huggingface.co/ggerganov/whisper.cpp)")
        if not self.ffmpeg:
            gaps.append("ffmpeg (brew install ffmpeg)")
        if not self.ytdlp:
            gaps.append("yt-dlp, for YouTube links (brew install yt-dlp)")
        return gaps

    # -- running things

    def run(self, argv, timeout, workdir=None, progress=None, extra_env=None):
        """Run a command, returning its stdout.

        Both pipes are drained by threads of their own rather than by
        communicate(), because stderr has to be read line by line as it
        arrives: that is where whisper reports how far through it is, and a
        pipe nobody empties fills up and stops the program that is writing it.
        """
        env = self.env()
        env.update(extra_env or {})
        proc = subprocess.Popen(
            [str(a) for a in argv], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            cwd=str(workdir) if workdir else None, env=env)
        tail, out = [], []

        def read_err():
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                # ffmpeg prefixes every line with its internal object address,
                # which means nothing to anyone reading the failure in a chat.
                line = re.sub(r"\[[^\]]*@ 0x[0-9a-f]+\]\s*", "", line)
                if line:
                    tail.append(line)
                    del tail[:-40]
                if progress:
                    found = PROGRESS_RE.search(line)
                    if found:
                        progress(int(found.group(1)))

        def read_out():
            out.append(proc.stdout.read())

        pumps = [threading.Thread(target=fn, daemon=True)
                 for fn in (read_err, read_out)]
        for pump in pumps:
            pump.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise TranscribeError("%s gave up after %d minutes"
                                  % (Path(argv[0]).name, max(1, timeout // 60)))
        finally:
            for pump in pumps:
                pump.join(timeout=10)
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
        if proc.returncode != 0:
            name = Path(argv[0]).name
            reason = " / ".join(tail[-3:])
            if name.startswith("yt-dlp"):
                plain = explain_ytdlp_error(" ".join(tail))
                if plain:
                    raise TranscribeError(plain)
            raise TranscribeError("%s failed: %s" % (name, reason or "no output"))
        return b"".join(out).decode("utf-8", "replace")

    # -- audio

    def to_wav(self, source, workdir):
        """16 kHz mono, which is the only thing whisper.cpp listens to."""
        wav = Path(workdir) / "audio.wav"
        self.run([self.ffmpeg, "-nostdin", "-y", "-i", str(source), "-vn",
                  "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                 timeout=3600)
        if not wav.is_file() or wav.stat().st_size < 1024:
            raise TranscribeError("there is no sound in that file")
        return wav

    def whisper_run(self, wav, workdir, progress=None):
        """Returns (segments, language) as Segment records without a speaker."""
        base = Path(workdir) / "out"
        argv = [self.whisper, "-m", str(self.model), "-f", str(wav),
                "-l", self.language, "-oj", "-of", str(base), "-pp"]
        # Token times are only wanted for cutting a line at a change of
        # speaker. They roughly triple the JSON, so an undiarized run asks for
        # exactly what it always asked for.
        if self.diarize_ready():
            argv.append("-ojf")
        if self.prompt:
            argv += ["--prompt", self.prompt]
        if self.threads:
            argv += ["-t", str(self.threads)]
        seconds = wav.stat().st_size / 32000.0        # 16 kHz, 16 bit, mono
        self.run(argv, timeout=int(seconds * 4) + 600, progress=progress)
        payload = load_json(str(base) + ".json")
        if not payload:
            raise TranscribeError("whisper wrote no transcript")
        language = ((payload.get("result") or {}).get("language") or "").strip()
        chunks = payload.get("transcription") or []
        segments = []
        for i, chunk in enumerate(chunks):
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            offsets = chunk.get("offsets") or {}
            start = (offsets.get("from") or 0) / 1000.0
            end = (offsets.get("to") or 0) / 1000.0
            if end <= start:
                # A line with no length wins no overlap against any turn and
                # so would be left unnamed. Borrow the next line's start, and
                # failing that give it a hair's width of its own.
                nxt = chunks[i + 1] if i + 1 < len(chunks) else {}
                after = ((nxt.get("offsets") or {}).get("from") or 0) / 1000.0
                end = after if after > start else start + 0.01
            segments.append(Segment(start, end, text,
                                    words=whisper_words(chunk)))
        if not segments:
            raise TranscribeError("nothing was said in that recording")
        return segments, language

    def diarize(self, wav, workdir, seconds):
        """Who spoke when, as [(start, end, label)]. Never raises.

        A transcript with nobody's name on it is still the transcript.
        Everything that can go wrong in here - no diarizer, one that dies, one
        that writes nothing, one that writes something that is not RTTM - is
        logged, and the words are written exactly as they always were.
        """
        if not self.diarize_ready():
            return []
        rttm = Path(workdir) / "speakers.rttm"
        argv = [self.diarizer] + [
            a.replace("{wav}", str(wav)).replace("{rttm}", str(rttm))
            for a in self.diarize_args]
        try:
            # Generous, because a diarizer is slower than nothing and faster
            # than whisper, but bounded, so a stuck one cannot hold the job
            # open for ever.
            self.run(argv, timeout=min(int(seconds * 2) + 600, 14400),
                     workdir=workdir,
                     extra_env={"CHUTE_SPEAKERS": str(self.speakers)})
            turns = parse_rttm(rttm.read_text(encoding="utf-8",
                                              errors="replace"))
        except (TranscribeError, OSError, ValueError) as exc:
            log("diarization skipped: %s" % exc)
            return []
        if not turns:
            log("diarization found nobody")
        return turns

    def transcribe_audio(self, source, workdir, progress=None):
        """A media file on disk to (segments, language, seconds)."""
        if not self.audio_ready():
            raise TranscribeError("transcription is not set up on this computer")
        wav = self.to_wav(source, workdir)
        seconds = wav.stat().st_size / 32000.0
        if self.max_minutes and seconds > self.max_minutes * 60:
            raise TranscribeError(
                "that is %s long, past the %d minute limit"
                % (hhmmss(seconds), self.max_minutes))
        segments, language = self.whisper_run(wav, workdir, progress)
        if self.diarize_ready():
            # The diarizer wants the same wav whisper just read, so it is only
            # thrown away below rather than above.
            if progress:
                progress("Finding the speakers")
            segments = assign_speakers(
                segments, self.diarize(wav, workdir, seconds),
                self.speaker_label)
        wav.unlink(missing_ok=True)
        return segments, language, seconds

    # -- youtube

    def youtube_info(self, url):
        raw = self.run([self.ytdlp, "-J", "--no-playlist", "--no-warnings",
                        "--no-progress", url], timeout=300)
        try:
            return json.loads(raw)
        except ValueError:
            raise TranscribeError("yt-dlp did not describe that video")

    def caption_track(self, info):
        """Which subtitle track to take, and whether it is an automatic one.

        Manual subtitles are what someone wrote for the video. Automatic ones
        are only used when the config asks for them, because whisper punctuates
        better than YouTube's recogniser does.
        """
        spoken = (info.get("language") or "").split("-")[0]
        for auto, tracks in ((False, info.get("subtitles") or {}),
                             (True, info.get("automatic_captions") or {})):
            if auto and self.captions != "any":
                break
            usable = [k for k in tracks if k != "live_chat" and tracks[k]]
            if not usable:
                continue
            # An "-orig" track is the language actually spoken; everything
            # else in a long automatic list is machine-translated from it.
            for candidate in usable:
                if candidate.endswith("-orig"):
                    return candidate, auto
            if spoken:
                for candidate in usable:
                    if candidate.split("-")[0] == spoken:
                        return candidate, auto
            if not auto:
                return usable[0], False
        return None, False

    def youtube_captions(self, url, info, workdir):
        """(lines, language) from the video's own subtitles, or (None, None)."""
        if self.captions == "off":
            return None, None
        track, auto = self.caption_track(info)
        if not track:
            return None, None
        base = Path(workdir) / "subs"
        self.run([self.ytdlp, "--skip-download", "--no-playlist",
                  "--no-warnings", "--no-progress",
                  "--write-auto-subs" if auto else "--write-subs",
                  "--sub-langs", track, "--sub-format", "vtt/best",
                  "--convert-subs", "vtt", "-o", str(base), url], timeout=600)
        files = sorted(Path(workdir).glob("subs*.vtt"))
        if not files:
            return None, None
        lines = vtt_to_lines(files[0].read_text(encoding="utf-8",
                                                errors="replace"))
        if not lines:
            return None, None
        return lines, track.split("-")[0]

    def youtube_media(self, url, workdir, progress=None):
        """Fetch the video, or its audio, or a bare wav to read and throw away.

        What comes back is kept beside the transcript unless keep is none, so
        the format is the one worth having on disk rather than the one whisper
        wants. The wav it does want is made from this afterwards.
        """
        base = Path(workdir) / "yt"
        argv = [self.ytdlp, "--no-playlist", "--no-warnings", "--no-progress",
                "--max-filesize", "%dM" % self.max_download_mb,
                "-o", str(base) + ".%(ext)s"]
        if self.keep == "video":
            # Capped at 1080p on purpose: 4K triples the size and whisper
            # never looks at a single pixel of it.
            argv += ["-f", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
                     "--merge-output-format", "mp4"]
        elif self.keep == "audio":
            argv += ["-f", "bestaudio/best"]
        else:
            argv += ["-f", "bestaudio/best", "-x", "--audio-format", "wav",
                     "--postprocessor-args", "ffmpeg:-ac 1 -ar 16000"]
        self.run(argv + [url], timeout=3600, progress=progress)
        found = sorted(Path(workdir).glob("yt.*"))
        if not found:
            raise TranscribeError(
                "nothing downloaded, which usually means the video is over the "
                "%d MB limit" % self.max_download_mb)
        return found[0]

    def transcribe_youtube(self, url, workdir, progress=None):
        """Captions first. Returns (segments, language, seconds, info, engine,
        media), where media is the file worth keeping, or None."""
        if not self.ytdlp:
            raise TranscribeError("yt-dlp is not installed on this computer")
        info = self.youtube_info(url)
        seconds = float(info.get("duration") or 0)
        if self.max_minutes and seconds > self.max_minutes * 60:
            raise TranscribeError(
                "that video is %s long, past the %d minute limit"
                % (hhmmss(seconds), self.max_minutes))
        lines, language = self.youtube_captions(url, info, workdir)
        if lines:
            # Captions spared us the transcription, not the download: keeping
            # the video is a separate wish from how the words were got.
            media = (self.youtube_media(url, workdir, progress)
                     if self.keep != "none" else None)
            spoken = caption_speakers(lines)
            engine = "the video's own captions"
            if any(who for who, _ in spoken):
                engine += ", which say who is speaking"
            return ([Segment(None, None, text, who) for who, text in spoken],
                    language, seconds, info, engine, media)
        if not self.audio_ready():
            raise TranscribeError(
                "that video has no subtitles and whisper is not set up here")
        media = self.youtube_media(url, workdir, progress)
        segments, language, seconds = self.transcribe_audio(
            media, workdir, progress)
        if self.keep == "none":
            media.unlink(missing_ok=True)
            media = None
        return segments, language, seconds, info, self.engine_label(), media


# ---------------------------------------------------------------- summaries

# A summary needs a model too big to run beside the bot, so the words go to
# Anthropic and come back as a headline and a few bullets. Off unless
# config.json asks, and the only part of Chute that makes a network call for
# anything but Telegram.
SUMMARY_HEADING = "## Summary"
API_VERSION = "2023-06-01"
# The scalar form of server-side fallbacks: when a recording trips a safety
# classifier the same request is re-run on another model rather than coming
# back empty. Its header and the older list form are not interchangeable.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        # No minimum worth speaking of: a forty-second voice note does not
        # contain four points, and a schema that insists on them makes the
        # model invent one.
        "bullets": {"type": "array", "items": {"type": "string"},
                    "minItems": 1},
    },
    "required": ["headline", "bullets"],
    "additionalProperties": False,
}

SUMMARY_SYSTEM = (
    "You summarise transcripts of recordings: meetings, interviews, talks, "
    "voice notes.\n\n"
    "The headline is at most twelve words. It names the thing, it is not a "
    "sentence about the thing, and it goes in a note's title bar where a long "
    "one is cut off.\n\n"
    "Then up to %d bullets, at most twenty-five words each, covering what "
    "actually mattered: what was decided, claimed, asked for or agreed, and "
    "any names, numbers and dates worth keeping. Where the transcript marks "
    "who is speaking, say who said what.\n\n"
    "Fewer bullets is better than padding. A short recording may hold only "
    "one point, and a bullet saying that nothing was decided, or that no "
    "figures were given, is worth nothing to anyone - leave it out.\n\n"
    "Write both in %s, the language of the recording.\n\n"
    "Do not open with \"This transcript\", \"The speaker\" or \"In this "
    "recording\" - say the thing itself. Use no markdown, no asterisks and no "
    "bullet characters: the headline and each bullet are plain text, and "
    "punctuation inside them is fine.\n\n"
    "A transcript is machine-made and may be garbled or cut short. Summarise "
    "what is there and do not guess at what is missing."
)


class Summariser:
    """A headline and a few bullets for a transcript, from the Claude API.

    Written against urllib rather than the anthropic SDK for one reason: Chute
    is one file with no dependencies, and it already speaks HTTP this way to
    Telegram. Everything here is optional and off by default.
    """

    def __init__(self, data=None):
        data = data or {}
        for named in ("api_key", "key"):
            if named in data:
                raise ConfigError(
                    "summary.%s does not exist, and a key does not belong in "
                    "config.json. Put ANTHROPIC_API_KEY in service/.env "
                    "instead. See \"Summaries\" in the README." % named)
        self.enabled = data.get("enabled", False) is True
        self.model = str(data.get("model") or "claude-opus-5").strip()
        self.key_env = str(data.get("api_key_env") or "ANTHROPIC_API_KEY")
        key_file = str(data.get("api_key_file") or "").strip()
        self.key_file = Path(key_file).expanduser() if key_file else None
        self.bullets = int(data.get("bullets") or 4)
        if not 1 <= self.bullets <= 12:
            raise ConfigError("summary.bullets must be between 1 and 12, got %r"
                              % data.get("bullets"))
        self.max_chars = int(data.get("max_chars") or 120000)
        self.timeout = int(data.get("timeout") or 120)
        self.base_url = str(data.get("base_url")
                            or "https://api.anthropic.com").rstrip("/")
        if not self.base_url.startswith(("https://", "http://localhost",
                                         "http://127.0.0.1")):
            raise ConfigError(
                "summary.base_url must be https, or a proxy on this machine. "
                "Transcripts are not sent in clear text over a network.")

    def key(self):
        """The API key, from the environment or a file. Never raises.

        A file is the one that works under a service: launchd and systemd hand
        a job almost no environment, so an exported variable that works from a
        shell is silently absent once installed.
        """
        found = os.environ.get(self.key_env, "").strip()
        if found:
            return found
        if self.key_file:
            try:
                return self.key_file.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return ""

    def ready(self):
        return bool(self.enabled and self.key())

    def host(self):
        return urllib.parse.urlsplit(self.base_url).netloc or self.base_url

    def missing(self):
        if not self.enabled or self.key():
            return []
        where = "%s in the environment" % self.key_env
        if self.key_file:
            where += ", or %s" % self.key_file
        else:
            where += ", or set summary.api_key_file"
        return ["an API key from console.anthropic.com: %s" % where]

    def body(self, text, language):
        """The request, kept apart from sending it so a test can read it."""
        return {
            "model": self.model,
            # Thinking is on by default on this model and this caps thinking
            # and answer together, so a small number does not buy a short
            # summary: it buys a truncated one that will not parse. A summary
            # is a couple of hundred tokens; the rest is headroom.
            "max_tokens": 4096,
            "system": SUMMARY_SYSTEM % (self.bullets,
                                        language or "the language spoken"),
            "messages": [{"role": "user", "content": text[:self.max_chars]}],
            # The schema is what makes this parseable rather than scraped: the
            # headline arrives already apart from the bullets, which is what
            # lets one line of it go into YAML frontmatter.
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA},
            },
            "fallbacks": "default",
        }

    def post(self, body):
        """One request, returning the parsed reply. The seam the tests fake."""
        request = urllib.request.Request(
            self.base_url + "/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json",
                     "x-api-key": self.key(),
                     "anthropic-version": API_VERSION,
                     "anthropic-beta": FALLBACK_BETA})
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def summarise(self, text, language=""):
        """A (headline, bullets) for some words, or (None, []). Never raises.

        A transcript nobody summarised is still the transcript. No key, no
        network, an error from the API, a refusal, or an answer that is not the
        shape the schema promised: each is logged in one line and the words are
        written exactly as they always were.
        """
        if not (self.ready() and (text or "").strip()):
            return None, []
        try:
            reply = self.post(self.body(text, language))
            if reply.get("stop_reason") == "refusal":
                detail = reply.get("stop_details") or {}
                raise ValueError("declined (%s)"
                                 % (detail.get("category") or "no reason given"))
            if reply.get("stop_reason") == "max_tokens":
                raise ValueError("the answer was cut off")
            said = ""
            for block in reply.get("content") or []:
                if block.get("type") == "text":
                    said = block.get("text") or ""
                    break
            found = json.loads(said)
            headline = str(found["headline"]).strip()
            bullets = [str(b).strip() for b in found["bullets"] if str(b).strip()]
        except urllib.error.HTTPError as exc:
            log("summary skipped: HTTP %s %s"
                % (exc.code, api_error(exc) or exc.reason))
            return None, []
        except Exception as exc:
            # Deliberately everything: http.client raises errors that are not
            # OSError, and no summary is worth losing a finished transcript.
            log("summary skipped: %s" % exc)
            return None, []
        if not headline:
            log("summary skipped: nothing came back")
            return None, []
        return headline, bullets


def api_error(exc):
    """The message out of an API error body, if it says anything useful."""
    try:
        detail = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return ""
    return str((detail.get("error") or {}).get("message") or "")


def summary_lines(headline, bullets):
    """The summary as a block for the top of the note, above the words."""
    if not headline:
        return []
    lines = ["", SUMMARY_HEADING, "", headline, ""]
    return lines + ["- %s" % b for b in bullets] + [""] if bullets else lines


# Every filed item has at most one note beside it, and a transcript is added
# to that note rather than becoming a second one.
TRANSCRIPT_HEADING = "## Transcript"


def transcript_stem(base, now=None):
    """What a transcript note is called: what it is of, and when it was made.

    The stamp is the transcription's own time, not the recording's, so two
    passes over one talk sit side by side rather than one replacing the other.
    """
    now = now or datetime.now()
    return "%s transcript %s" % (base, now.strftime("%Y-%m-%d %H%M"))


def transcript_body(segments, timestamps):
    """The words themselves, with or without a time against each line."""
    lines = []
    if timestamps and any(s.start is not None for s in segments):
        for seg in segments:
            stamp = "[%s] " % hhmmss(seg.start) if seg.start is not None else ""
            lines += ["%s%s" % (stamp, seg.text), ""]
    else:
        for para in paragraphs([s.text for s in segments]):
            lines += [para, ""]
    return lines


def transcript_section(segments, meta, timestamps=False, summary=None):
    """The transcript as a block to append to the note a file already has.

    The summary, when there is one, rides at the front of the same string, so
    it reaches a grown note and a brand-new one by the one path the words
    already take.
    """
    lines = summary_lines(*(summary or (None, [])))
    lines += ["", TRANSCRIPT_HEADING, ""]
    for key, label in (("title", "Source"), ("channel", "Channel"),
                       ("published", "Published"), ("language", "Language"),
                       ("duration", "Length"), ("speakers", "Speakers"),
                       ("transcribed-with", "Transcribed with"),
                       ("diarized-with", "Speakers found with")):
        if meta.get(key):
            lines.append("- %s: %s" % (label, meta[key]))
    lines.append("")
    named = {s.speaker for s in segments if s.speaker}
    if len(named) < 2:
        # One voice, or none named, reads exactly as it always did. A name
        # written once at the top and never again is noise, not information.
        return "\n".join(lines + transcript_body(segments, timestamps))
    for who, block in speaker_blocks(segments):
        if who:
            lines += ["**%s**" % who, ""]
        # The name sits on a line of its own so that what is under it is the
        # same paragraphs, or the same stamped lines, as an undiarized note.
        lines += transcript_body(block, timestamps)
    return "\n".join(lines)


def add_frontmatter(text, fields, tag=None):
    """Insert keys into a note's YAML frontmatter, if it has any.

    Only ever inserts lines, never rewrites what is already there, so a note
    edited by hand comes through unharmed. Text with no frontmatter is
    returned untouched rather than given some.
    """
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    lines = text[:end].split("\n")
    if tag:
        for i, line in enumerate(lines):
            if line.strip() != "tags:":
                continue
            # After the last item already in the list, so the tags a note
            # arrived with keep the order they were written in.
            last = i + 1
            while last < len(lines) and lines[last].lstrip().startswith("- "):
                last += 1
            if tag not in [x.strip()[2:].strip() for x in lines[i + 1:last]]:
                lines.insert(last, "  - %s" % tag)
            break
    for key, value in fields:
        if value:
            lines.append("%s: %s" % (key, json.dumps(str(value),
                                                     ensure_ascii=False)))
    return "\n".join(lines) + text[end:]


def transcript_note(filename, meta, section, embed=False):
    """A note for a file that had none, holding the file link and the words."""
    lines = ["---",
             "created: %s" % datetime.now().strftime("%Y-%m-%d"),
             "source: telegram",
             "file: %s" % json.dumps(filename, ensure_ascii=False)]
    for key, value in (("transcript-language", meta.get("language")),
                       ("transcript-length", meta.get("duration")),
                       ("transcript-speakers", meta.get("speakers")),
                       ("transcript-summary", meta.get("headline"))):
        if value:
            lines.append("%s: %s" % (key, json.dumps(str(value),
                                                     ensure_ascii=False)))
    lines += ["tags:", "  - inbox", "  - transcript", "---", ""]
    lines.append(("![](<%s>)" if embed else "[%s](<%s>)")
                 % ((filename,) if embed else (filename, filename)))
    return "\n".join(lines) + "\n" + section


# ---------------------------------------------------------------- bot

class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tg = Telegram(cfg.token, cfg.api_root, cfg.files_from,
                           cfg.files_to)
        self.state = load_json(STATE_PATH, {}) or {}
        self.state.setdefault("offset", 0)
        self.state.setdefault("chats", {})
        # Transcription runs off the poll loop, so state is written from two
        # threads. Everything that touches it takes this first.
        self.lock = threading.RLock()
        # A job cannot outlive the process that started it, so a flag left set
        # by a restart is a lie that would hide the button for good.
        for chat in self.state["chats"].values():
            for record in (chat.get("filed") or {}).values():
                record.pop("transcribing", None)

    def chat_state(self, chat_id):
        cs = self.state["chats"].setdefault(str(chat_id), {})
        cs.setdefault("filed", {})
        cs.setdefault("history", [])
        # Shed the queue bookkeeping a pre-0.2 state file carries.
        for gone in ("queue", "active", "stage", "dest", "prompt_id",
                     "last_filed"):
            cs.pop(gone, None)
        return cs

    def persist(self):
        save_json(STATE_PATH, self.state)

    # -- keyboards

    def keyboard(self, current=None, record=None):
        """Every folder, with the one the file is in right now marked."""
        rows, row = [], []
        for dest in self.cfg.destinations:
            label = ("• %s" % dest.label) if dest.key == current else dest.label
            row.append({"text": label, "callback_data": "b:%s" % dest.key})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        if self.offers_transcript(record):
            rows.append([{"text": "📝 Transcribe",
                          "callback_data": "b:__transcribe"}])
        rows.append([{"text": "🗑 Delete", "callback_data": "b:__delete"}])
        return rows

    def transcribable(self, record):
        """Whether there are words in this to find, and a way to find them."""
        if not record:
            return False
        if record.get("yt"):
            return self.cfg.transcribe.youtube_ready()
        ext = (record.get("ext") or "").lower()
        if record.get("kind") == "media" or ext in AUDIO_EXT or ext in VIDEO_EXT:
            return self.cfg.transcribe.audio_ready()
        return False

    def offers_transcript(self, record):
        """The button shows until the transcript exists, and not while it runs."""
        if not record or record.get("transcript") or record.get("transcribing"):
            return False
        return self.transcribable(record)

    def help_text(self):
        lines = ["<b>%s</b> %s" % (APP.title(), VERSION), "",
                 "Send me anything: photos, files, audio, video, links, "
                 "forwarded messages. It is saved the moment it arrives, in "
                 "<b>%s</b>. Tap a folder on the reply to move it there, tap "
                 "again to move it somewhere else, 🗑 to delete it."
                 % self.cfg.inbox.label, "",
                 "<b>Folders</b>"]
        for dest in self.cfg.destinations:
            mark = "  ← lands here" if dest.key == self.cfg.inbox.key else ""
            lines.append("· %s → <code>%s</code>%s"
                         % (dest.label, dest.path, mark))
        if self.cfg.transcribe.audio_ready() or \
                self.cfg.transcribe.youtube_ready():
            what = []
            if self.cfg.transcribe.audio_ready():
                what.append("a voice note, audio or video file")
            if self.cfg.transcribe.youtube_ready():
                what.append("a YouTube link")
            lines += ["", "<b>Transcripts</b>",
                      "Send %s and the reply offers 📝 Transcribe. Tap it and "
                      "the words are written as a markdown note in the same "
                      "folder as the file, and move with it. The language is "
                      "worked out from the recording, so nothing needs saying "
                      "in advance." % " or ".join(what)]
            if self.cfg.transcribe.audio_ready():
                lines.append("It runs on my computer with whisper.cpp (%s). "
                             "A long recording takes a few minutes and "
                             "everything else keeps filing while it runs."
                             % self.cfg.transcribe.model_name())
                if self.cfg.transcribe.diarize_ready():
                    lines.append("When more than one person is talking, each "
                                 "of them is marked in the note.")
                # Said here because this is the one place a person reads
                # rather than a config file they had to go looking for.
                if self.cfg.summary.ready():
                    lines.append("The words are then sent to Anthropic to be "
                                 "summarised, and the summary goes at the top "
                                 "of the note. Nothing else leaves the "
                                 "computer: not the recording, not the file.")
                else:
                    lines.append("Nothing is sent anywhere.")
        lines += ["", "Names are date plus type, like <code>2026-08-20 1848 "
                      "Image.jpg</code>. A caption overrides that: caption a "
                      "photo <code>contract p3</code> and that is its "
                      "filename. Links and text become notes. A forwarded "
                      "photo or file keeps who it came from, the source link "
                      "and its full caption in a note saved next to it.", "",
                  "I only answer while my computer is awake. Send anyway: "
                  "Telegram keeps things for 24 hours and I file them when I "
                  "wake up.", "",
                  "Limits: 20 MB per file (Telegram's cap), no executable "
                  "file types.", "",
                  "/history  what was filed where, and when",
                  "/help  this message"]
        return "\n".join(lines)

    # -- loop

    def register_ui(self):
        """Telegram's built-in instruction surfaces: the / command menu and
        the description shown before a chat starts. Best effort."""
        try:
            self.tg.call("setMyCommands", commands=[
                {"command": "help", "description": "How Chute works"},
                {"command": "history",
                 "description": "What was filed where, and when"},
            ])
            self.tg.call(
                "setMyDescription",
                description="Send anything: photos, files, audio, video, "
                            "links. It is saved to the owner's computer as it "
                            "arrives; tap a folder on the reply to move it "
                            "there, or 📝 to transcribe a recording. Answers "
                            "only while that computer is awake, and only to "
                            "its owner.")
            self.tg.call(
                "setMyShortDescription",
                short_description="Files what you send into folders on your "
                                  "own computer.")
        except Exception as exc:
            log("could not register the command menu: %s" % exc)

    def run(self):
        STAGING.mkdir(parents=True, exist_ok=True)
        me = self.tg.call("getMe")
        log("%s %s connected as @%s, filing into %s"
            % (APP, VERSION, me.get("username"), self.cfg.root))
        self.register_ui()
        backoff = 1
        while True:
            try:
                updates = self.tg.call(
                    "getUpdates", offset=self.state["offset"], timeout=POLL_TIMEOUT,
                    allowed_updates=["message", "callback_query"])
                backoff = 1
            except TelegramConflict as exc:
                log("another Chute is already polling this bot token (%s).\n"
                    "  Stop the other one first: chute stop" % exc)
                raise SystemExit(3)
            except (NetworkError, urllib.error.URLError, OSError,
                    RuntimeError, ValueError) as exc:
                log("poll error: %s (retry in %ds)" % (exc, backoff))
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            for update in updates:
                # One turn of the lock per update. A transcription running on
                # its own thread writes to the same state file.
                with self.lock:
                    self.state["offset"] = update["update_id"] + 1
                    try:
                        self.handle(update)
                    except Exception as exc:              # never kill the loop
                        log("handler error: %r" % (exc,))
                    self.persist()

    def handle(self, update):
        if "callback_query" in update:
            return self.on_callback(update["callback_query"])
        if update.get("message"):
            return self.on_message(update["message"])

    def authorised(self, user, chat_id):
        uid = (user or {}).get("id")
        if uid in self.cfg.allowed:
            return True
        log("rejected user %s (@%s)" % (uid, (user or {}).get("username")))
        if self.cfg.reply_to_strangers:
            try:
                self.tg.send(chat_id, "Not authorised.")
            except Exception:
                pass
        return False

    # -- messages

    def on_message(self, msg):
        chat_id = msg["chat"]["id"]
        if not self.authorised(msg.get("from"), chat_id):
            return
        cs = self.chat_state(chat_id)
        text = (msg.get("text") or "").strip()

        if text.startswith("/"):
            return self.on_command(chat_id, cs, text)
        try:
            item = extract_item(msg, self.tg, STAGING, self.cfg)
        except ValueError as exc:
            return self.tg.send(chat_id, "Skipped: %s" % exc)
        except Exception as exc:
            log("download failed: %r" % (exc,))
            return self.tg.send(chat_id, "Could not download that: %s" % exc)
        if not item:
            return self.tg.send(chat_id, "Nothing to file in that message.")
        self.land(chat_id, cs, item)

    def land(self, chat_id, cs, item):
        """Write an arriving item into the landing folder and offer to move it."""
        inbox = self.cfg.inbox
        try:
            directory = self.cfg.resolve_dir(inbox, item["kind"],
                                             item.get("ext", ""))
            path = file_item(item, directory, name_for(item, self.cfg.naming),
                             self.cfg)
        except Exception as exc:
            log("filing failed: %r" % (exc,))
            discard(item)
            return self.tg.send(chat_id, "Could not save that: %s" % exc)

        record = restat({"stem": path.stem, "ext": path.suffix,
                         "kind": item["kind"]}, path, inbox.key)
        if item.get("youtube"):
            record["yt"] = item["youtube"]
        if item.get("sidecar"):
            try:
                add_sidecar(record, item["sidecar"], item.get("sidecar_tail"))
            except OSError:
                pass
        self.note_history(cs, path, item["kind"], "filed")
        log("filed -> %s" % rel_to(self.cfg.root, path))
        sent = self.tg.send(chat_id, self.filed_text(record),
                            self.keyboard(inbox.key, record))
        remember(cs["filed"], sent.get("message_id"), record)

    def filed_text(self, record, verb="Filed", note=None):
        # Escaped because a filename may hold an &: clean_name strips < > and
        # " but has no reason to touch &, and unescaped it takes the whole
        # reply down with a parse error.
        rel = tg_escape(rel_to(self.cfg.root, record["path"]))
        text = "%s\n<code>%s</code>" % (verb, rel)
        for sc in sidecars_of(record):
            text += "\n+ <code>%s</code>" % tg_escape(
                rel_to(self.cfg.root, sc["path"]))
        if note:
            text += "\n%s" % note
        elif self.offers_transcript(record):
            text += "\nTranscribe it?"
        return text

    def note_history(self, cs, path, kind, action):
        history = cs.setdefault("history", [])
        history.append({"at": int(time.time()),
                        "path": rel_to(self.cfg.root, path),
                        "kind": kind_word({"kind": kind}), "action": action})
        del history[:-HISTORY_KEEP]

    def on_command(self, chat_id, cs, text):
        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        if cmd in ("start", "help"):
            return self.tg.send(chat_id, self.help_text())
        if cmd in ("undo", "cancel", "back", "status"):
            return self.tg.send(
                chat_id, "Nothing waits for an answer any more: what you send "
                         "is filed as it arrives. Tap a folder on its message "
                         "to move it, or 🗑 to delete it.")
        if cmd == "history":
            history = cs.get("history") or []
            if not history:
                return self.tg.send(chat_id, "Nothing filed yet.")
            lines = ["<b>Filed</b> (newest first)"]
            for entry in reversed(history[-HISTORY_SHOW:]):
                stamp = datetime.fromtimestamp(entry["at"]).strftime(
                    "%d %b %H:%M")
                mark = {"moved": "→", "deleted": "🗑"}.get(
                    entry.get("action"), " ")
                lines.append("%s %s <code>%s</code>"
                             % (stamp, mark, entry["path"]))
            older = len(history) - len(history[-HISTORY_SHOW:])
            if older > 0:
                lines.append("<i>and %d older</i>" % older)
            return self.tg.send(chat_id, "\n".join(lines))
        return self.tg.send(chat_id, "Unknown command. Try /help.")

    # -- flow

    def on_callback(self, cq):
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id is None:
            return
        if not self.authorised(cq.get("from"), chat_id):
            # Acknowledge so their client stops spinning, but say nothing.
            return self.tg.ack(cq["id"],
                               "Not authorised" if self.cfg.reply_to_strangers
                               else None)
        cs = self.chat_state(chat_id)
        self.tg.ack(cq["id"])
        message_id = msg.get("message_id")
        record = (cs.get("filed") or {}).get(str(message_id))
        _, _, value = (cq.get("data") or "").partition(":")

        if record is None:
            return self.confirm(
                chat_id, cs, message_id, None,
                "I no longer have a record of that file, so I have not touched "
                "anything. Move it by hand.")
        if value == "__transcribe":
            return self.start_transcript(chat_id, cs, message_id, record)
        if value == "__delete":
            return self.remove(chat_id, cs, message_id, record)
        if value in self.cfg.by_key:
            return self.move(chat_id, cs, message_id, record, value)
        # Anything else is a button from a config that has since changed.

    def move(self, chat_id, cs, message_id, record, dest_key):
        dest = self.cfg.by_key[dest_key]
        try:
            directory = self.cfg.resolve_dir(dest, record["kind"],
                                             record.get("ext", ""))
            path = move_filed(record, directory, self.cfg.root)
        except NotAsFiled as exc:
            cs["filed"].pop(str(message_id), None)
            return self.confirm(chat_id, cs, message_id, None, str(exc))
        except Exception as exc:
            log("move failed: %r" % (exc,))
            return self.tg.send(chat_id, "Could not move that: %s" % exc)

        went_somewhere = str(path) != record["path"]
        restat(record, path, dest_key)
        if went_somewhere:
            self.note_history(cs, path, record["kind"], "moved")
            log("moved -> %s" % rel_to(self.cfg.root, path))
        self.confirm(chat_id, cs, message_id, record,
                     self.filed_text(record,
                                     "Moved" if went_somewhere else "Filed"))

    def remove(self, chat_id, cs, message_id, record):
        try:
            path = delete_filed(record, self.cfg.root)
        except NotAsFiled as exc:
            cs["filed"].pop(str(message_id), None)
            return self.confirm(chat_id, cs, message_id, None, str(exc))
        except Exception as exc:
            log("delete failed: %r" % (exc,))
            return self.tg.send(chat_id, "Could not delete that: %s" % exc)
        kept = delete_sidecar(record)
        self.note_history(cs, path, record["kind"], "deleted")
        log("deleted -> %s" % rel_to(self.cfg.root, path))
        cs["filed"].pop(str(message_id), None)
        text = "🗑 Deleted <code>%s</code>" % rel_to(self.cfg.root, path)
        for note in kept:
            text += ("\nIts note <code>%s</code> has your edits, so it stays."
                     % rel_to(self.cfg.root, note))
        self.confirm(chat_id, cs, message_id, None, text)

    # -- transcription

    def start_transcript(self, chat_id, cs, message_id, record):
        """Begin a transcription and hand the chat straight back.

        A button on an older message can still be tapped after the transcript
        exists, so the offer is checked here and not only when drawing it.

        The work runs on a thread of its own. A talk an hour long takes
        minutes to transcribe, and nothing else that arrives in the meantime
        should have to wait behind it.
        """
        if not self.offers_transcript(record):
            return self.confirm(chat_id, cs, message_id, record,
                                self.filed_text(record))
        record["transcribing"] = True
        self.confirm(chat_id, cs, message_id, record,
                     self.filed_text(record, note="📝 Transcribing…"))
        threading.Thread(target=self.transcript_job, daemon=True,
                         args=(chat_id, message_id)).start()

    def live_record(self, chat_id, message_id):
        """The record as it stands now. It may have moved, or gone."""
        return (self.chat_state(chat_id).get("filed") or {}).get(str(message_id))

    def transcript_progress(self, chat_id, message_id):
        """One edit every 20 seconds at most, so a long job still looks alive."""
        last = [0.0, -1]

        def show(percent):
            now = time.time()
            # A stage, rather than a percentage, is a step that happens once
            # and reports nothing while it runs. It always shows, or the chat
            # sits at 100% looking hung while the speakers are worked out.
            stage = isinstance(percent, str)
            if not stage and (percent == last[1] or now - last[0] < 20):
                return
            last[0], last[1] = now, percent
            with self.lock:
                record = self.live_record(chat_id, message_id)
                if not record:
                    return
                text = self.filed_text(
                    record, note="📝 %s…" % percent if stage
                    else "📝 Transcribing… %d%%" % percent)
                keyboard = self.keyboard(record["dest"], record)
            self.tg.edit(chat_id, message_id, text, keyboard)

        return show

    def transcript_job(self, chat_id, message_id):
        """Off the poll loop: fetch, transcribe, write the note, say so."""
        with self.lock:
            record = dict(self.live_record(chat_id, message_id) or {})
        if not record:
            return
        engine = self.cfg.transcribe
        progress = self.transcript_progress(chat_id, message_id)
        workdir = STAGING / ("transcript-%s-%d" % (message_id, int(time.time())))
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            if record.get("yt"):
                segments, language, seconds, info, label, media = \
                    engine.transcribe_youtube(record["yt"], workdir, progress)
                title = (info.get("title") or "").strip()
                meta = {"title": title or "YouTube transcript",
                        "url": record["yt"],
                        "channel": info.get("uploader") or info.get("channel")}
                stamp = str(info.get("upload_date") or "")
                if len(stamp) == 8:
                    meta["published"] = "%s-%s-%s" % (stamp[:4], stamp[4:6],
                                                      stamp[6:])
                base = clean_name(title, self.cfg.naming) if title else None
            else:
                source = Path(record["path"])
                if not source.is_file():
                    raise TranscribeError(
                        "that file is not where I left it any more")
                segments, language, seconds = engine.transcribe_audio(
                    source, workdir, progress)
                label = engine.engine_label()
                meta = {"file": source.name}
                media, base = None, source.stem
            meta["language"] = language_label(language) or "undetermined"
            meta["duration"] = hhmmss(seconds) if seconds else ""
            meta["transcribed-with"] = label
            names = {s.speaker for s in segments if s.speaker}
            if len(names) > 1:
                meta["speakers"] = str(len(names))
                meta["diarized-with"] = (
                    "the captions' own speaker marks" if record.get("yt")
                    and "captions" in label else engine.diarize_label())
        except TranscribeError as exc:
            return self.transcript_failed(chat_id, message_id, str(exc))
        except Exception as exc:
            log("transcription failed: %r" % (exc,))
            return self.transcript_failed(chat_id, message_id,
                                          "it went wrong: %s" % exc)

        # Before the note is written, not after: the note is then written once
        # and completely, and a second pass would append a duplicate
        # transcript-summary line, since add_frontmatter only ever inserts.
        section = transcript_section(segments, meta, engine.timestamps)
        summary = (None, [])
        if self.cfg.summary.ready():
            try:
                progress("Summarising")
                # The same block that goes in the note, so the model sees who
                # was speaking and how long it ran. One renderer, one thing to
                # keep right; the second call only puts the answer on top.
                summary = self.cfg.summary.summarise(section,
                                                     meta.get("language"))
            except Exception as exc:
                # summarise() swallows its own failures; this catches the chat
                # edit beside it. The words are already won by here and a note
                # that cannot be announced is still a note worth writing.
                log("summary skipped: %s" % exc)
            if summary[0]:
                meta["headline"] = summary[0]
                section = transcript_section(segments, meta,
                                             engine.timestamps, summary)
        try:
            with self.lock:
                live = self.live_record(chat_id, message_id)
                cs = self.chat_state(chat_id)
                try:
                    path, where, kept = self.write_transcript(
                        live or record, section, meta, base, media)
                except OSError as exc:
                    return self.transcript_failed(
                        chat_id, message_id, "it could not be saved: %s" % exc)
        finally:
            # Only now: the kept video is still in here until write_transcript
            # has moved it out.
            shutil.rmtree(str(workdir), ignore_errors=True)

        with self.lock:
            live = self.live_record(chat_id, message_id)
            cs = self.chat_state(chat_id)
            log("transcribed (%s) -> %s"
                % (meta["language"], rel_to(self.cfg.root, path)))
            if not live:
                self.note_history(cs, path, "text", "filed")
                self.persist()
                said = ("\n\n%s" % tg_escape(summary[0])) if summary[0] else ""
                return self.tg.send(
                    chat_id, "📝 Transcript in %s\n<code>%s</code>%s"
                    % (meta["language"], rel_to(self.cfg.root, path), said))
            live.pop("transcribing", None)
            live["transcript"] = True
            # The note grew and was renamed, so whatever record points at it
            # has to be restated or the 🗑 refuses it as changed since filing
            # and a folder tap chases the old name.
            if where == "self":
                live["stem"] = path.stem
                restat(live, path, live["dest"])
                notes = []
            else:
                # The tail is how the note keeps following its recording when
                # a later move renames the pair.
                anchor = Path(live["path"]).stem
                tail = (path.stem[len(anchor):]
                        if path.stem.startswith(anchor) else None)
                notes = [sidecar_stat(path, tail)]
                if where == "new":
                    self.note_history(cs, path, "text", "filed")
            if kept:
                # Named for the video, not for the note, so it keeps its own
                # name rather than following the note's stem.
                notes.append(sidecar_stat(kept, None))
                self.note_history(cs, kept, "media", "filed")
            set_sidecars(live, notes)
            note = "📝 Transcript in %s" % meta["language"]
            if summary[0]:
                note += "\n\n%s" % tg_escape(summary[0])
                # Enough of the points to be worth reading in the chat, and
                # not so much that the message becomes the note.
                for bullet in summary[1]:
                    line = "\n• %s" % tg_escape(bullet)
                    if len(note) + len(line) > 700:
                        break
                    note += line
            text = self.filed_text(live, note=note)
            self.confirm(chat_id, cs, message_id, live, text)
            self.persist()

    def write_transcript(self, record, section, meta, base=None, media=None):
        """Put the transcript in the one note this item has, making it if
        there is none. Returns (path, "self" | "sidecar" | "new", kept),
        where kept is the downloaded media now on disk, or None.

        One markdown file per thing filed. A link arrives as a note already,
        so the words go into it. A forwarded recording has a note holding
        where it came from, so they go in there. A bare recording has none,
        so one is written. Whichever it is, the note ends up named for what it
        is a transcript of and when it was made, because the name it arrived
        with says nothing about the words now in it.
        """
        directory = Path(record["path"]).parent
        directory.mkdir(parents=True, exist_ok=True)
        kept = self.keep_media(media, directory, base) if media else None
        if kept:
            meta["file"] = kept.name
        fields = [("transcript-language", meta.get("language")),
                  ("transcript-length", meta.get("duration")),
                  ("transcript-speakers", meta.get("speakers")),
                  ("transcript-summary", meta.get("headline"))]
        stem = transcript_stem(base or Path(record["path"]).stem)

        def grow(note):
            body = add_frontmatter(note.read_text(encoding="utf-8"), fields,
                                   tag="transcript")
            if kept:
                body = add_frontmatter(body, [("file", kept.name)])
            body = retitle(body, note.stem, meta.get("title") or base)
            note.write_text(body + section, encoding="utf-8")
            return rename_to(note, directory, stem)

        if record.get("kind") == "text":
            target = Path(record["path"])
            if target.is_file():
                return grow(target), "self", kept
        notes = sidecars_of(record)
        if notes:
            target = Path(notes[0]["path"])
            if target.is_file():
                return grow(target), "sidecar", kept
        source = Path(record["path"])
        target = unique_path(directory, stem, ".md")
        target.write_text(
            transcript_note(kept.name if kept else source.name, meta, section,
                            embed=record.get("kind") == "image"),
            encoding="utf-8")
        return target, "new", kept

    def keep_media(self, media, directory, base):
        """Move a downloaded video or audio file in beside its transcript."""
        try:
            target = unique_path(directory,
                                 base or clean_name(media.stem, self.cfg.naming),
                                 media.suffix)
            shutil.move(str(media), str(target))
        except (OSError, ValueError) as exc:
            log("could not keep the download: %r" % (exc,))
            return None
        log("kept -> %s" % rel_to(self.cfg.root, target))
        return target

    def transcript_failed(self, chat_id, message_id, reason):
        """Put the button back: a failure worth retrying usually is."""
        log("transcription: %s" % reason)
        with self.lock:
            record = self.live_record(chat_id, message_id)
            if not record:
                return self.tg.send(chat_id, "Transcription: %s" % reason)
            record.pop("transcribing", None)
            self.confirm(chat_id, self.chat_state(chat_id), message_id, record,
                         self.filed_text(record, note="Not transcribed: %s"
                                                      % reason))
            self.persist()

    def confirm(self, chat_id, cs, message_id, record, text):
        """Update a message in place, or post a fresh one if it is too old.

        Telegram refuses edits to messages older than 48 hours, but the buttons
        on such a message still fire. Without this fallback the file would move
        and the user would see nothing happen at all.
        """
        keyboard = self.keyboard(record["dest"], record) if record else None
        if message_id and self.tg.edit(chat_id, message_id, text, keyboard):
            return message_id
        sent = self.tg.send(chat_id, text, keyboard)
        new_id = sent.get("message_id")
        if record is not None and new_id:
            # The new message is the live handle now; the old one is dead.
            cs["filed"].pop(str(message_id), None)
            remember(cs["filed"], new_id, record)
        return new_id


# ---------------------------------------------------------------- single instance

def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # it exists, it just is not ours to signal
    except OSError:
        return False
    return True


def acquire_lock():
    """Stop a second local Chute before it fights the first over getUpdates."""
    try:
        existing = int(LOCK_PATH.read_text().strip())
    except (OSError, ValueError):
        existing = None
    if existing is not None and existing != os.getpid() and pid_alive(existing):
        raise SystemExit(
            "Chute is already running (pid %d).\n"
            "  Stop it first with:  %s stop\n"
            "  If you are sure nothing is running, delete %s and try again."
            % (existing, cli_name(), LOCK_PATH))
    LOCK_PATH.write_text(str(os.getpid()))


def release_lock():
    try:
        if LOCK_PATH.is_file() and LOCK_PATH.read_text().strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- interactive

def ask(prompt, default=""):
    shown = " [%s]" % default if default else ""
    return input("%s%s: " % (prompt, shown)).strip() or default


def ask_bool(prompt, default=False):
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input("%s [%s]: " % (prompt, hint)).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Answer y or n.")


def ask_menu(options, prompt="Choice", default=None):
    """options: list of (key, text). Returns the chosen key."""
    for key, text in options:
        print("    %-4s %s" % (key, text))
    keys = [k.lower() for k, _ in options]
    while True:
        shown = " [%s]" % default if default else ""
        answer = input("\n  %s%s: " % (prompt, shown)).strip().lower()
        if not answer and default:
            return default.lower()
        if answer in keys:
            return answer
        print("  Not one of the options.")


def is_template(path):
    return "{" in str(path)


def show_destinations(dests, root):
    print("\n  Buttons, in the order they appear in Telegram:\n")
    if not dests:
        print("    (none yet)")
        return
    for i, d in enumerate(dests, 1):
        print("    %2d. %-22s -> %s" % (i, d["label"], d["path"]))
        for kind, path in sorted((d.get("by_kind") or {}).items()):
            nice = {"media": "audio and video", "text": "links and notes",
                    "image": "images", "document": "documents"}[kind]
            print("        %-18s -> %s" % (nice, path))


# ---------------------------------------------------------------- commands

def resolve_root(raw, cwd=None):
    """Turn typed input into an absolute folder path. Touches no disk.

    Copes with the three things people actually paste: a leading ~, a relative
    path, and a path dragged from a file manager, which arrives either wrapped
    in quotes or with its spaces backslash-escaped.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("no path given")
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1].strip()
    raw = raw.replace("\\ ", " ")
    if not raw:
        raise ValueError("no path given")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    return Path(os.path.normpath(str(path)))


def looks_like_path(text):
    """Distinguish a pasted path from a stray word like 'back' or 'help'."""
    return (text.startswith(("/", "~", ".", "'", '"'))
            or "/" in text or "\\" in text)


def prompt_root():
    """Ask where files should go. Returns a usable folder, or None to abort."""
    cwd = Path.cwd()
    # Running setup from Chute's own folder is the common first run; filing
    # into the program folder is almost never what anyone wants, so suggest
    # a fresh folder in their home instead.
    in_repo = cwd == HERE
    suggested = (Path.home() / "Chute") if in_repo else cwd
    print("\nWhere should files go on this computer?\n")
    print("  1. %s" % suggested)
    if in_repo:
        print("     (a new folder in your home folder; it will be created)")
    else:
        print("     (the folder you are running setup from)")
    print("  2. Somewhere else, and I'll type the path")
    print("\nPick 1 or 2, or just paste a path. Type q to quit setup.")

    while True:
        answer = input("Choice [1]: ").strip() or "1"
        low = answer.lower()
        if low in ("q", "quit", "b", "back"):
            print("  Nothing saved. Run setup again whenever you like.")
            return None
        if answer == "1":
            root = suggested
        elif answer == "2" or looks_like_path(answer):
            raw = answer if answer != "2" else input("Path: ").strip()
            try:
                root = resolve_root(raw)
            except ValueError as exc:
                print("  %s. Try again." % exc)
                continue
        else:
            # A bare word here is a typo or a guessed command, not a path.
            # Turning it into a folder offer next to the current directory
            # helps nobody.
            print("  Pick 1 or 2, paste a full path (starting with / or ~),")
            print("  or type q to quit.")
            continue

        if root.exists() and not root.is_dir():
            print("  %s is a file, not a folder. Try again." % root)
            continue
        if not root.exists():
            # Creating the folder we suggested needs no caution; a typed path
            # that does not exist is more often a typo, so default to no.
            if in_repo and root == suggested:
                make = input("  %s does not exist. Create it? [Y/n]: "
                             % root).strip().lower() or "y"
            else:
                make = input("  %s does not exist. Create it? [y/N]: "
                             % root).strip().lower()
            if make not in ("y", "yes"):
                print("  Nothing created. Pick somewhere else.")
                continue
            try:
                root.mkdir(parents=True, exist_ok=True)
                print("  created %s" % root)
            except OSError as exc:
                print("  could not create it: %s" % exc)
                continue
        if not os.access(str(root), os.W_OK):
            print("  %s is not writable by you. Pick somewhere else." % root)
            continue

        return root.resolve()


def subfolders_of(path):
    try:
        return sorted(p.name for p in path.iterdir()
                      if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def toggle_list(entries, chosen=(), allow_new=False):
    """A checklist. entries is [(name, hint)]; returns chosen names in order.

    Numbers toggle, several at once is fine, a is all, n is none, d or a
    bare Enter finishes. With allow_new, anything that is not a number or a
    command is taken as a new name: it joins the list, already turned on.
    """
    entries = list(entries)
    chosen = set(chosen)
    added = set()          # names typed in this session, removable with r
    while True:
        print()
        for i, (name, hint) in enumerate(entries, 1):
            line = "    %2d. [%s] %s" % (i, "x" if name in chosen else " ", name)
            if hint:
                line += "   (%s)" % hint
            print(line)
        print()
        print("    %-8s turn one on or off, several at once is fine: 2 4 5"
              % "number")
        print("    %-8s turn on every one" % "a")
        print("    %-8s turn them all off" % "n")
        print("    %-8s done, %d selected" % ("d", len(chosen)))
        if added:
            print("    %-8s remove a name you typed, e.g. r %d"
                  % ("r", len(entries)))
        if allow_new:
            print("\n  Or type a name of your own and it becomes a button,")
            print("  e.g. Tax 2026")
        raw = input("\n  > ").strip()
        answer = raw.lower()
        names = [n for n, _ in entries]

        if answer == "a":
            chosen = set(names)
            continue
        if answer == "n":
            chosen = set()
            continue
        if answer in ("d", ""):
            return [n for n, _ in entries if n in chosen]

        tokens = [t for t in answer.replace(",", " ").split() if t]
        if allow_new and tokens and tokens[0] == "r":
            num = tokens[1] if len(tokens) > 1 else input(
                "  Remove which number? ").strip()
            if num.isdigit() and 1 <= int(num) <= len(entries):
                name = entries[int(num) - 1][0]
                if name in added:
                    entries.pop(int(num) - 1)
                    added.discard(name)
                    chosen.discard(name)
                    print("  Removed %s." % name)
                else:
                    print("  %s is part of the list; leaving it unticked "
                          "is enough." % name)
            else:
                print("  There is no %s in that list." % num)
            continue
        if allow_new and not all(t.isdigit() for t in tokens):
            # Not numbers, so it is a name for a new button.
            if "/" in raw or "\\" in raw:
                print("  One name per button, and no slashes.")
                continue
            if not ILLEGAL.sub("", raw).strip().strip(". "):
                print("  There is nothing in that name I can make a "
                      "folder from.")
                continue
            match = next((n for n in names if n.lower() == answer), None)
            if match:
                chosen.add(match)      # already listed: just turn it on
                continue
            # "a personal" is far more often the a-command plus a name than a
            # button genuinely called that. Check before taking it literally.
            first, _, rest = raw.partition(" ")
            if rest.strip() and first.lower() in ("a", "n", "d", "r"):
                if not ask_bool('  Add a button called "%s"?' % raw, False):
                    print("  To add one, type just the name:  %s"
                          % rest.strip())
                    continue
            entries.append((raw, ""))
            added.add(raw)
            chosen.add(raw)
            continue

        typed_word = False
        for token in tokens:
            if token.isdigit() and 1 <= int(token) <= len(names):
                name = names[int(token) - 1]
                if name in chosen:
                    chosen.remove(name)
                else:
                    chosen.add(name)
            else:
                print("  There is no %s in that list." % token)
                typed_word = typed_word or not token.isdigit()
        if typed_word and not allow_new:
            print("  This list is only for choosing; new buttons with their")
            print("  own names come in the next step, after d.")


def pick_existing_folders(root, names):
    """Offer each folder already in the root as a button, on or off.

    Nothing here creates or renames anything: these folders exist already.
    """
    print("\n  %s already has %d folder(s) in it." % (root, len(names)))
    print("  Turn on the ones you want as buttons. This is only a first pass:")
    print("  the next step lets you add brand-new buttons with names of your")
    print("  own, and %s config can change everything later." % cli_name())
    return [{"label": n, "path": n}
            for n in toggle_list([(n, "") for n in names])]


# Offered when the chosen folder is empty, so the first buttons are a matter
# of reacting to examples rather than inventing names from nothing.
STARTER_BUTTONS = [
    ("Inbox", "anything you'll sort out later"),
    ("Photos", ""),
    ("Documents", ""),
    ("Receipts", ""),
    ("Notes", "links and text you send"),
]


def pick_starter_buttons(root):
    """Suggest first buttons for an empty root. Returns the chosen names."""
    print("\n  %s is empty, so let's create your first buttons." % root)
    print("\n  Each button is a folder. When you send the bot a photo or a")
    print("  file, it shows your buttons; you tap one and the file lands in")
    print("  that folder.")
    print("\n  Here are some common ones. Turn on any you want, or type names")
    print("  of your own; a folder is created for each. Not sure? Inbox alone")
    print("  is a fine start, and everything can be changed later.")
    return toggle_list(STARTER_BUTTONS, allow_new=True)


def ensure_inbox(destinations, root):
    """Guarantee somewhere for things to land, and mark it.

    A button already called Inbox is used as-is. Otherwise one is added at the
    front, because every arrival needs a folder before anyone taps anything.
    """
    for dest in destinations:
        if slugify(dest["label"]) == "inbox":
            dest["catch_all"] = True
            return destinations
    for dest in destinations:
        dest.pop("catch_all", None)
    try:
        safe_join(root, DERIVED_INBOX["path"]).mkdir(parents=True,
                                                     exist_ok=True)
    except (ValueError, OSError) as exc:
        print("  Could not create the Inbox folder: %s" % exc)
    return [dict(DERIVED_INBOX)] + destinations


def setup_destinations(root):
    """First-run buttons: type a name, get a folder of that name. Nothing else.

    A slash makes a sub-path. Per-kind routing is config.json only.

    Returns the list of destinations, or None if the user asked to go back and
    choose a different root folder.
    """
    dests = []
    made = {}   # label -> folder this run created, so undo can remove it again

    existing = subfolders_of(root)
    if existing:
        dests = pick_existing_folders(root, existing)
        if dests:
            print("\n  %d button(s) from folders already there." % len(dests))
    else:
        for label in pick_starter_buttons(root):
            folder = clean_name(label)
            try:
                target = safe_join(root, folder)
                target.mkdir(parents=True, exist_ok=True)
            except (ValueError, OSError) as exc:
                print("  Could not create %s: %s" % (label, exc))
                continue
            dests.append({"label": label, "path": folder})
            made[label] = target
        if dests:
            print("\n  Created: %s" % ", ".join(d["label"] for d in dests))

    if dests:
        print("\n  Now you can add brand-new buttons: type a name, and a folder")
        print("  is created for it inside %s." % root)
        print("  Press Enter on its own if you don't need any more.")
    else:
        print("\n  Type a button name and press Enter. Each one becomes a folder")
        print("  of the same name inside %s." % root)
        print("  A folder that is already there is reused, never overwritten.")
    print("\n    undo    take back the last button")
    print("    back    choose a different root folder")
    print("    done    finish, or just press Enter on an empty line\n")
    while True:
        try:
            label = input("  Button %d: " % (len(dests) + 1)).strip()
        except EOFError:
            label = ""

        if label.lower() in ("undo", "u"):
            if not dests:
                print("  Nothing to undo yet.")
                continue
            gone = dests.pop()
            folder = made.pop(gone["label"], None)
            if folder is None:
                print("  Took back %s. The folder was already there, so it "
                      "stays." % gone["label"])
            else:
                try:
                    folder.rmdir()
                    print("  Took back %s and removed the empty folder."
                          % gone["label"])
                except OSError:
                    print("  Took back %s. Something is in %s already, so the "
                          "folder stays." % (gone["label"], folder))
            continue

        if label.lower() in ("back", "b"):
            if made:
                print("  Going back. The %d folder(s) just created stay on "
                      "disk; nothing has been saved to a config yet."
                      % len(made))
            return None

        if not label or label.lower() in ("done", "d"):
            if dests:
                break
            print("  You need at least one button. Type 'back' to choose a "
                  "different root folder.")
            continue

        if any(d["label"].lower() == label.lower() for d in dests):
            print("  There is already a button called %s." % label)
            continue
        # clean_name falls back to a date stamp when nothing usable is left,
        # which would be a baffling folder to end up with. Catch that here.
        if not ILLEGAL.sub("", label.replace("/", "")).strip().strip(". "):
            print("  There is nothing in that name I can make a folder from.")
            continue
        # A slash makes a sub-path: "Work/Attachments" files into that folder
        # and puts "Attachments" on the button, since the full path would not
        # fit one.
        raw_parts = [part.strip() for part in
                     label.replace("\\", "/").split("/") if part.strip()]
        # clean_name would turn ".." into a date stamp, quietly inventing a
        # folder instead of refusing to climb out of the root.
        if any(part in (".", "..") for part in raw_parts):
            print("  A folder cannot step outside your root.")
            continue
        parts = [clean_name(part) for part in raw_parts]
        folder = "/".join(parts)
        if len(parts) > 1:
            label = parts[-1]
            if any(d["label"].lower() == label.lower() for d in dests):
                print("  There is already a button called %s. Rename it or "
                      "pick another folder." % label)
                continue
        try:
            target = safe_join(root, folder)
        except ValueError as exc:
            print("  Cannot use that as a folder name: %s" % exc)
            continue
        if target == root:
            print("  That leaves nothing to name a folder with.")
            continue
        existed = target.is_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print("  Could not create %s: %s" % (target, exc))
            continue
        dests.append({"label": label, "path": folder})
        if not existed:
            made[label] = target
        print("      -> %s%s  (type 'undo' to take it back)"
              % (target, "" if not existed else "  [already there]"))
        if len(dests) == 12:
            print("\n  That is 12 buttons, about as many as fits a phone screen.")
    show_destinations(dests, root)
    return dests


def landing_label(data):
    """Which folder a config would land things in, without building a Config."""
    dests = data.get("destinations") or []
    for dest in dests:
        if dest.get("catch_all"):
            return dest.get("path") or dest.get("label")
    for dest in dests:
        if slugify(dest.get("label") or "") == "inbox":
            return dest.get("path") or dest.get("label")
    return DERIVED_INBOX["path"]


def cmd_config(args):
    """Edit an existing config without opening a text editor."""
    path = find_config(args.config)
    if not path:
        print("No config yet. Run:  chute setup")
        return 1
    data = load_json(path)
    if data is None:
        print("%s is not valid JSON. Fix or delete it, then run: chute setup" % path)
        return 1

    root = Path(str(data.get("root") or data.get("vault") or "")).expanduser()
    if not root.is_dir():
        print("Root folder %s is missing." % root)
        if not ask_bool("Pick a new one now?", True):
            return 1
        root = prompt_root()
        if root is None:
            return 1
        data["root"] = str(root)
    root = root.resolve()

    while True:
        print("\n" + "=" * 62)
        print("Chute configuration            %s" % path)
        print("=" * 62)
        print("  Root folder    %s" % root)
        print("  Buttons        %d" % len(data.get("destinations") or []))
        print("  Things land in %s" % landing_label(data))
        print("  Allowed Telegram ids   %s"
              % ", ".join(str(i) for i in data.get("allowed_user_ids") or []))
        print()
        choice = ask_menu([
            ("1", "folders and buttons"),
            ("2", "where files go on this computer"),
            ("3", "who may use the bot"),
            ("4", "bot token"),
            ("s", "save and exit"),
            ("q", "quit without saving"),
        ], default="s")

        if choice == "1":
            print("\n  This rebuilds your buttons. Folders you keep hold on to "
                  "any\n  per-type routing set in config.json.")
            rebuilt = setup_destinations(root)
            if rebuilt is not None:
                old = {d.get("path"): d for d in data.get("destinations") or []}
                for dest in rebuilt:
                    kept = old.get(dest["path"]) or {}
                    for extra in ("by_kind", "key"):
                        if extra in kept:
                            dest[extra] = kept[extra]
                data["destinations"] = ensure_inbox(rebuilt, root)
        elif choice == "2":
            new_root = prompt_root()
            if new_root:
                root = new_root
                data["root"] = str(root)
                print("  Buttons still point at the same relative paths. "
                      "Check them with option 1.")
        elif choice == "3":
            ids = list(data.get("allowed_user_ids") or [])
            print("\n  Only these Telegram user ids may use the bot: %s"
                  % ", ".join(str(i) for i in ids))
            print("  Anyone else is ignored in silence.")
            sub = ask_menu([("a", "add someone"), ("r", "remove someone"),
                            ("d", "done")], default="d")
            if sub == "a":
                print("  They can get their id from @userinfobot in Telegram.")
                new = ask("  Their numeric id")
                if new.isdigit():
                    ids.append(int(new))
                else:
                    print("  That is not a number.")
            elif sub == "r":
                gone = ask("  Which id to remove")
                if gone.isdigit() and int(gone) in ids:
                    if len(ids) == 1:
                        print("  That is the only one left. Add another first.")
                    else:
                        ids.remove(int(gone))
            data["allowed_user_ids"] = ids
        elif choice == "4":
            tok = str(data.get("bot_token") or "")
            print("\n  Current token ends in ...%s" % tok[-6:] if tok
                  else "\n  No token stored.")
            print("  You only need a new one if you revoked the old token in")
            print("  BotFather, or you are switching to a different bot.")
            new = clean_token(input("  Paste the new token, or press Enter "
                                    "to keep the current one: "))
            if not new:
                pass
            elif not TOKEN_RE.match(new):
                print("  That does not look like a token; keeping the "
                      "current one.")
            else:
                try:
                    me = Telegram(new).call("getMe")
                    data["bot_token"] = new
                    print("  Connected to @%s. Token updated."
                          % me.get("username"))
                except NetworkError as exc:
                    print("  Could not reach Telegram to check it: %s" % exc)
                    if ask_bool("  Save it anyway?", False):
                        data["bot_token"] = new
                except Exception:
                    print("  Telegram rejected that token; keeping the "
                          "current one.")
        elif choice == "q":
            print("Nothing saved.")
            return 0
        elif choice == "s":
            try:
                cfg = Config(data)
                cfg.validate_paths()
            except ConfigError as exc:
                print("\nNot saved, the config is not valid yet:\n  %s" % exc)
                continue
            save_json(path, data)
            os.chmod(path, 0o600)
            print("\nSaved %s" % path)
            print("If Chute is running, apply the change with:  %s restart"
                  % cli_name())
            return 0


def ask_token():
    """Prompt for a bot token until one works. Returns (token, tg, me) or None."""
    while True:
        raw = clean_token(input("Bot token (or q to quit): "))
        if raw.lower() in ("q", "quit"):
            print("  Nothing saved. Run setup again whenever you like.")
            return None
        if not raw:
            print("  Paste the whole line BotFather sent you.")
            continue
        if not TOKEN_RE.match(raw):
            print("  That does not look like a token. It is one long line of")
            print("  numbers, a colon, then letters, like")
            print("      1234567890:AAF6yzXqML2wp3...")
            print("  Copy the whole thing from BotFather and paste it here.")
            continue
        tg = Telegram(raw)
        try:
            me = tg.call("getMe")
        except NetworkError as exc:
            print("\nCould not reach Telegram, so the token was never "
                  "checked.\n  %s" % exc)
            return None
        except ConfigError:
            print("  Telegram says that token is not valid. Check you copied")
            print("  the whole line, then paste it again.")
            continue
        except Exception as exc:
            print("  That token did not work: %s" % exc)
            continue
        return raw, tg, me


def wait_for_first_message(tg, offset, seconds=300):
    """Poll until any message arrives. Returns (user_id, name, offset).

    A TelegramConflict is re-raised: it means a Chute somewhere else holds
    this bot, and no amount of waiting here fixes that.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            updates = tg.call("getUpdates", offset=offset, timeout=30)
        except TelegramConflict:
            raise
        except Exception as exc:
            print("  ...%s" % exc)
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            frm = (u.get("message") or {}).get("from") or {}
            if frm.get("id"):
                name = frm.get("username") or frm.get("first_name") or "you"
                return frm["id"], name, offset
        print("  still waiting...")
    return None, None, offset


def cmd_setup(args):
    cli = cli_name()
    existing = find_config(args.config)
    if existing:
        print("Chute is already set up (%s)." % existing)
        print("To change folders, buttons or anything else:  %s config" % cli)
        if not ask_bool("\nThrow that away and start over from scratch?", False):
            return 0
        print()

    print("Chute setup\n")
    print("First you need a bot of your own on Telegram. It takes a minute:\n")
    print("  1. In Telegram, search for BotFather and open it.")
    print("  2. Send it the message:  /newbot")
    print("  3. It asks for a display name. Anything you like.")
    print("  4. It asks for a username, which must end in 'bot'.")
    print("  5. It replies with a token, one long line of numbers and letters.")
    print("     Copy it and paste it here.\n")
    print("Used Chute before, maybe on another computer? Don't make a second")
    print("bot. Send BotFather /mybots, pick your bot, and choose API Token")
    print("to see the same token again.\n")
    got = ask_token()
    if got is None:
        return 1
    token, tg, me = got
    print("\nConnected to your bot, @%s." % me.get("username"))

    # Loops so that 'back' inside the button step returns to the root choice.
    destinations = None
    while destinations is None:
        root = prompt_root()
        if root is None:
            return 1
        print("\nFiling into %s" % root)

        print("\n" + "-" * 62)
        print("Now name the buttons you'll see in Telegram. Each one is a folder.")
        print("You can change all of this later with:  chute config")
        print("-" * 62)
        destinations = setup_destinations(root)
        if destinations is not None:
            destinations = ensure_inbox(destinations, root)
            print("\n  Everything you send lands in %s first. The buttons "
                  "move it\n  from there." % DERIVED_INBOX["path"])

    print("\nLast step: Chute needs to know which Telegram account is yours,")
    print("so that nobody else can ever use this bot.")
    print("\nOpen this link and send the bot any message, even just 'hi':")
    print("\n    https://t.me/%s\n" % me.get("username"))
    print("Waiting for it...")
    offset, user_id = 0, None
    while user_id is None:
        try:
            user_id, name, offset = wait_for_first_message(tg, offset)
        except TelegramConflict:
            print("\nYour bot is still connected to Chute on another computer.")
            print("Only one computer can hold a bot at a time.")
            print("\nOn that computer, run:  chute stop")
            print("(If it is gone for good, revoke the token with BotFather's")
            print("/revoke and run setup again with the new one.)")
            if not ask_bool("\nStopped it? Try again now?", True):
                print("Nothing saved. Run setup again whenever you like.")
                return 1
            continue
        if user_id is None:
            if not ask_bool("\nFive minutes and no message yet. Keep waiting?",
                            True):
                print("Nothing saved. Run setup again whenever you like.")
                return 1
            print("Waiting...")
    print("\nGot it. This bot now answers only to %s (id %s)." % (name, user_id))

    target = Path(args.config).expanduser() if args.config else HERE / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    save_json(target, {
        "bot_token": token,
        "allowed_user_ids": [user_id],
        "root": str(root),
        "destinations": destinations,
        "naming": {"style": "keep-spaces"},
        "text_capture": {"format": "markdown", "frontmatter": True},
    })
    os.chmod(target, 0o600)
    save_json(STATE_PATH, {"offset": offset, "chats": {}})
    print("\nSetup is done. Your settings are in %s (readable only by you)."
          % target)
    print("\nOne more command starts Chute now and at every login:")
    print("\n    %s install" % cli)
    print("\nThat is the only start you ever do by hand. From then on Chute")
    print("comes back on its own every time you log in, including after a")
    print("restart. Day to day:")
    print("\n    %s status     is it running?" % cli)
    print("    %s log        watch files arrive" % cli)
    print("    %s stop       pause it; %s start resumes" % (cli, cli))
    print("    %s config     change folders, buttons, anything" % cli)
    print("\nOnce it is running, filing works like this:")
    print("\n    1. Send the bot anything: a photo, a file, audio, video,")
    print("       or a link. It is saved to your Inbox straight away.")
    print("    2. Tap a folder on the reply to move it there. Tap another")
    print("       to move it again, or 🗑 to delete it.")
    print("\nNames are date plus type, like '2026-08-20 1848 Image.jpg'.")
    print("A caption becomes the filename instead.")
    print("\nChute only runs while this computer is awake. Send things any")
    print("time: Telegram holds them for 24 hours and Chute files them when")
    print("the computer wakes. /help in the chat repeats all of this.")
    print("\nRun %s install now, then send your bot a photo to try it out."
          % cli)
    return 0


def cmd_check(args):
    load_env_file()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print("Config: FAILED\n  %s" % exc)
        return 1
    print("Config: %s" % cfg.source)
    print("Root:   %s  %s" % (cfg.root, "ok" if cfg.root.is_dir() else "MISSING"))
    try:
        warnings = cfg.validate_paths()
    except ConfigError as exc:
        print("Paths:  FAILED\n  %s" % exc)
        return 1
    try:
        me = Telegram(cfg.token, cfg.api_root).call("getMe")
        print("Bot:    @%s  ok" % me.get("username"))
    except ConfigError as exc:
        print("Bot:    FAILED\n  %s" % exc)
        print("  Update the token with:  %s config" % cli_name())
        return 1
    except NetworkError as exc:
        print("Bot:    unreachable\n  %s" % exc)
        return 1
    except Exception as exc:
        print("Bot:    unreachable - %s" % exc)
    print("Users:  %s" % ", ".join(str(x) for x in sorted(cfg.allowed)))
    tr = cfg.transcribe
    if not tr.enabled:
        print("Speech: off (transcription.enabled is false)")
    elif tr.audio_ready():
        print("Speech: whisper.cpp %s, %s captions for YouTube%s"
              % (tr.model_name(), tr.captions,
                 "" if tr.ytdlp else "  (yt-dlp missing, so links are skipped)"))
    else:
        print("Speech: unavailable, so the transcribe button will not appear")
        for gap in tr.missing():
            print("  needs %s" % gap)
    # Nothing at all when nobody asked: silence is the default state.
    if tr.diarize_ready():
        print("Names:  %s, via %s" % (tr.diarize_label(), tr.diarizer))
    elif tr.diarize_on:
        print("Names:  asked for, but nothing will be marked")
        for gap in tr.diarize_missing():
            print("  needs %s" % gap)
    sm = cfg.summary
    # Named rather than described, because check is where someone looks to
    # find out what this thing does with their files.
    if sm.ready():
        print("Sent:   %s, so transcripts go to %s"
              % (sm.model, sm.host()))
    elif sm.enabled:
        print("Sent:   asked for, but nothing will be summarised")
        for gap in sm.missing():
            print("  needs %s" % gap)
    if cfg.local_api:
        print("Server: %s  (your own, so no 20 MB download limit)"
              % cfg.api_root)
        print("        files at %s, seen by the server as %s"
              % (cfg.files_to, cfg.files_from))
        if not Path(cfg.files_to).is_dir():
            print("        WARNING: that folder does not exist yet. Nothing "
                  "sent will be readable until the server has written to it.")
    else:
        print("Server: Telegram's own, which caps downloads at 20 MB")
    print("Limits: %.0f MB max, %d blocked extensions"
          % (cfg.max_bytes / 1048576.0, len(cfg.blocked_ext)))
    print("\nDestinations:")
    for dest in cfg.destinations:
        for kind in ("image", "document", "media", "text"):
            target = cfg.resolve_dir(dest, kind, ".jpg")
            rel = target.relative_to(cfg.root) if target != cfg.root else "."
            if kind == "image" or dest.by_kind.get(kind):
                print("  %-22s %-8s %-40s %s"
                      % (dest.label if kind == "image" else "", kind, rel,
                         "exists" if target.is_dir() else "will be created"))
    for w in warnings:
        print("\nWarning: %s" % w)
    print("\nEverything looks good.")
    return 0


def cmd_logout(args):
    """Deregister the bot from Telegram's servers, so a local one can have it.

    Deliberately its own command and never a side effect of editing the
    config. A bot logged in on two servers is not guaranteed to receive
    anything, and coming back to Telegram's servers is barred for 10 minutes
    after this runs.
    """
    cfg = load_config(args.config)
    if cfg.local_api:
        print("api_root already points at %s." % cfg.api_root)
        print("Set it back to %s before logging out, or you will log out of "
              "your own server instead." % CLOUD_API)
        return 1
    print("This deregisters @your bot from %s so that your own Bot API server "
          "can take it over." % CLOUD_API)
    print()
    print("  · Telegram will refuse the bot for 10 minutes afterwards, so if "
          "the local server is not ready, the bot is simply down until then.")
    print("  · Point api_root at the local server as soon as this finishes.")
    print("  · Anything sent in between is lost, not queued.")
    print()
    if not ask_bool("Log out of Telegram's servers now?", False):
        print("Nothing done.")
        return 1
    try:
        Telegram(cfg.token, CLOUD_API).call("logOut")
    except Exception as exc:
        print("logOut failed: %s" % exc, file=sys.stderr)
        return 1
    print("\nDone. The bot is now yours to point somewhere else.")
    print("Set api_root in %s, then:  %s restart" % (cfg.source, cli_name()))
    return 0


def cmd_run(args):
    load_env_file()
    cfg = load_config(args.config)
    cfg.validate_paths()
    acquire_lock()
    try:
        Bot(cfg).run()
    finally:
        release_lock()
    return 0


class Args:
    def __init__(self, argv):
        self.config = None
        rest = []
        i = 0
        while i < len(argv):
            if argv[i] in ("-c", "--config") and i + 1 < len(argv):
                self.config = argv[i + 1]
                i += 2
                continue
            rest.append(argv[i])
            i += 1
        self.action = rest[0] if rest else "run"


def main():
    if sys.version_info < (3, 9):
        print("Chute needs Python 3.9 or newer; this is Python %d.%d."
              % sys.version_info[:2], file=sys.stderr)
        return 2
    args = Args(sys.argv[1:])
    if args.action in ("version", "--version", "-v"):
        print("%s %s" % (APP, VERSION))
        return 0
    if args.action in ("help", "--help", "-h"):
        print(__doc__)
        return 0
    try:
        if args.action == "setup":
            return cmd_setup(args)
        if args.action == "check":
            return cmd_check(args)
        if args.action == "logout":
            return cmd_logout(args)
        if args.action == "config":
            return cmd_config(args)
        if args.action == "run":
            return cmd_run(args)
    except KeyboardInterrupt:
        if args.action in ("setup", "config"):
            print("\nStopped. Nothing was saved.")
            return 1
        raise
    except ConfigError as exc:
        print("Problem with your settings: %s" % exc, file=sys.stderr)
        return 2
    print("Unknown command %r. Commands: setup, config, check, logout, run, "
          "version." % args.action, file=sys.stderr)
    print("Full help:  %s help" % cli_name(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        release_lock()
        print("\nChute stopped.")
