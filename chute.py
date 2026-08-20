#!/usr/bin/env python3
"""
Chute - send a file to a Telegram bot, it lands in the right folder.

Point it at any folder tree: an Obsidian vault, a Logseq graph, a NAS share, a
plain Downloads folder. Send the bot an image, a PDF, a voice note or a link.
It asks which folder the item belongs to, asks for a name, and writes it to
disk. Polling only, so it works behind NAT with no public URL.

    chute.py setup     one-time: bot token, root folder, destinations
    chute.py run       long-poll Telegram and file what arrives
    chute.py check     validate config, token and every destination
    chute.py version

Python 3.9+, standard library only. No dependencies.
"""

import json
import os
import re
import shutil
import string
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

VERSION = "0.1.0"
APP = "chute"

HERE = Path(__file__).resolve().parent
STAGING = HERE / "staging"
LOCK_PATH = HERE / "chute.lock"
LOG_PATH = HERE / "chute.log"
STATE_PATH = HERE / "state.json"

API_ROOT = "https://api.telegram.org"
POLL_TIMEOUT = 50                     # seconds Telegram holds getUpdates open
TG_DOWNLOAD_CEILING = 20 * 1024 * 1024   # Bot API hard limit, not ours to raise

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

    def template_for(self, kind):
        return self.by_kind.get(kind, self.path)

    def templates(self):
        return [self.path] + list(self.by_kind.values())


class Config:
    def __init__(self, data, source=None):
        self.source = source
        self.raw = data
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

        root = data.get("root") or data.get("vault")
        if not root:
            raise ConfigError("root is not set")
        self.root = Path(str(root)).expanduser().resolve()

        dests = data.get("destinations") or []
        if not dests:
            raise ConfigError("destinations is empty. Add at least one folder.")
        taken = set()
        self.destinations = [Destination(d, i, taken) for i, d in enumerate(dests)]
        self.by_key = {d.key: d for d in self.destinations}

        self.naming = data.get("naming") or {}
        style = self.naming.get("style", "keep-spaces")
        if style not in ("keep-spaces", "kebab", "snake"):
            raise ConfigError(
                "naming.style must be keep-spaces, kebab or snake, got %r" % style)

        self.text_capture = data.get("text_capture") or {}
        sec = data.get("security") or {}
        self.blocked_ext = set(
            e.lower() if e.startswith(".") else "." + e.lower()
            for e in sec.get("blocked_extensions", DEFAULT_BLOCKED_EXT))
        self.max_bytes = min(
            int(sec.get("max_file_mb", 20)) * 1024 * 1024, TG_DOWNLOAD_CEILING)
        self.allow_custom = bool(sec.get("allow_custom_paths", True))
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
    def __init__(self, token):
        self.token = token

    def call(self, method, **params):
        url = "%s/bot%s/%s" % (API_ROOT, self.token, method)
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
                raise ConfigError(
                    "Telegram rejected the bot token. Check config.json, or "
                    "create a new bot with @BotFather.")
            raise RuntimeError("%s failed: HTTP %s %s"
                               % (method, exc.code, detail.get("description", "")))
        except (urllib.error.URLError, OSError) as exc:
            raise NetworkError(explain_network_error(exc))
        if not payload.get("ok"):
            raise RuntimeError("%s failed: %s" % (method, payload.get("description")))
        return payload["result"]

    def send(self, chat_id, text, keyboard=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", **params)

    def edit(self, chat_id, message_id, text, keyboard=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True,
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
        url = "%s/file/bot%s/%s" % (API_ROOT, self.token,
                                    urllib.parse.quote(path, safe="/"))
        with urllib.request.urlopen(url, timeout=180) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh, 256 * 1024)
        return path


# ---------------------------------------------------------------- items

def describe_kind(kind):
    return {"image": "Image", "document": "Document",
            "media": "Audio/Video", "text": "Note"}.get(kind, "File")


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
    urls = re.findall(r"https?://\S+", msg.get("text") or "")
    if urls:
        meta["links"] = urls[:10]
    return meta


def extract_item(msg, tg, staging, cfg):
    """Turn an incoming message into a filing item, downloading any file."""
    caption = (msg.get("caption") or "").strip()
    item = {"id": "%s-%s" % (msg.get("message_id"), int(time.time() * 1000) % 100000),
            "caption": caption, "media_group_id": msg.get("media_group_id")}

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
        item["meta"] = forward_meta(msg)
        return item
    else:
        return None

    ext = ext_of(orig_name, default_ext)
    if ext in cfg.blocked_ext:
        raise ValueError("%s files are blocked by config" % ext)

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
        raise ValueError("%s files are blocked by config" % ext)

    item.update({"staged": str(staged), "ext": ext, "orig_name": orig_name,
                 "size": staged.stat().st_size})
    return item


def suggested_name(item, naming=None):
    """Best guess at a filename, offered to the user behind the '-' shortcut."""
    naming = naming or {}
    if item.get("caption"):
        return clean_name(item["caption"].splitlines()[0], naming)
    if item.get("kind") == "text":
        text = (item.get("text") or "").strip()
        urls = re.findall(r"https?://\S+", text)
        stripped = re.sub(r"https?://\S+", "", text).strip()
        if stripped:
            return clean_name(stripped.splitlines()[0], naming)
        if urls:
            parsed = urllib.parse.urlparse(urls[0])
            host = parsed.netloc.replace("www.", "")
            slug = re.sub(r"[-_]+", " ", parsed.path.strip("/").split("/")[-1]).strip()
            return clean_name(("%s %s" % (host, slug)).strip(), naming)
    if item.get("orig_name"):
        return clean_name(Path(item["orig_name"]).stem, naming)
    return clean_name("", naming)


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


def file_item(item, directory, name, cfg):
    directory.mkdir(parents=True, exist_ok=True)
    stem = clean_name(name, cfg.naming)
    # Applied here and nowhere else, so a name that round-trips through the
    # suggestion prompt does not collect a second date.
    if cfg.naming.get("date_prefix"):
        stem = "%s %s" % (datetime.now().strftime("%Y-%m-%d"), stem)
    if item["kind"] == "text":
        opts = cfg.text_capture
        ext = ".txt" if opts.get("format") == "txt" else ".md"
        dest = unique_path(directory, stem, ext)
        dest.write_text(note_body(item, stem, opts), encoding="utf-8")
    else:
        ext = item.get("ext") or ""
        if ext and stem.lower().endswith(ext.lower()):
            stem = stem[: -len(ext)].rstrip() or stem
        dest = unique_path(directory, stem, ext)
        shutil.move(item["staged"], str(dest))
    return dest


def discard(item):
    staged = (item or {}).get("staged")
    if staged and os.path.exists(staged):
        try:
            os.remove(staged)
        except OSError:
            pass


# ---------------------------------------------------------------- bot

class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tg = Telegram(cfg.token)
        self.state = load_json(STATE_PATH, {}) or {}
        self.state.setdefault("offset", 0)
        self.state.setdefault("chats", {})

    def chat_state(self, chat_id):
        return self.state["chats"].setdefault(str(chat_id), {
            "queue": [], "active": None, "stage": None, "dest": None,
            "prompt_id": None, "last_dest": None, "custom_dir": None,
            "last_filed": None})

    def persist(self):
        save_json(STATE_PATH, self.state)

    # -- keyboards

    def keyboard(self, cs):
        rows, row = [], []
        for dest in self.cfg.destinations:
            row.append({"text": dest.label, "callback_data": "b:%s" % dest.key})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        tail = []
        last = cs.get("last_dest")
        if last in self.cfg.by_key:
            tail.append({"text": "🔁 %s, auto-name" % self.cfg.by_key[last].label,
                         "callback_data": "q:%s" % last})
        if self.cfg.allow_custom:
            tail.append({"text": "📂 Other folder", "callback_data": "b:__custom"})
        if tail:
            rows.append(tail)
        rows.append([{"text": "✖️ Discard", "callback_data": "b:__cancel"}])
        return rows

    @staticmethod
    def back_keyboard():
        """Shown once a folder is picked, so a wrong tap costs one more tap."""
        return [[{"text": "⬅️ Back", "callback_data": "b:__back"},
                 {"text": "✖️ Discard", "callback_data": "b:__cancel"}]]

    @staticmethod
    def undo_keyboard():
        return [[{"text": "↩️ Undo", "callback_data": "z:last"}]]

    def help_text(self):
        lines = ["<b>%s</b> %s" % (APP.title(), VERSION), "",
                 "Send an image, a PDF, a voice note or a link and I'll ask "
                 "where it goes, then write it into your folder tree.", "",
                 "<b>Folders</b>"]
        for dest in self.cfg.destinations:
            lines.append("· %s → <code>%s</code>" % (dest.label, dest.path))
        lines += ["", "A caption becomes the suggested filename, so you can send "
                      "the picture with its name attached and just tap a folder, "
                      "then send <code>-</code>.", "",
                  "Tapped the wrong folder? Use ⬅️ Back, or /back. Filed "
                  "something by mistake? ↩️ Undo on the confirmation, or "
                  "/undo, puts it back in the queue.", "",
                  "/status  what's pending", "/back  return to the folder list",
                  "/undo  take back the last filed item",
                  "/cancel  drop everything pending",
                  "/help  this message"]
        return "\n".join(lines)

    # -- loop

    def run(self):
        STAGING.mkdir(parents=True, exist_ok=True)
        me = self.tg.call("getMe")
        log("%s %s connected as @%s, filing into %s"
            % (APP, VERSION, me.get("username"), self.cfg.root))
        self.recover()
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
                self.state["offset"] = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception as exc:                  # never kill the loop
                    log("handler error: %r" % (exc,))
                self.persist()

    def recover(self):
        """After a restart, re-prompt for anything caught mid-flow."""
        for chat_id, cs in self.state["chats"].items():
            if cs.get("active"):
                cs.update({"stage": "bucket", "dest": None, "custom_dir": None})
                try:
                    self.prompt(int(chat_id), cs, "Picking up where we left off.\n")
                except Exception as exc:
                    log("recover failed for %s: %r" % (chat_id, exc))
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
        if cs["stage"] == "name" and text:
            return self.finish(chat_id, cs, text)
        if cs["stage"] == "custom" and text:
            return self.on_custom_path(chat_id, cs, text)

        try:
            item = extract_item(msg, self.tg, STAGING, self.cfg)
        except ValueError as exc:
            return self.tg.send(chat_id, "Skipped: %s" % exc)
        except Exception as exc:
            log("download failed: %r" % (exc,))
            return self.tg.send(chat_id, "Could not download that: %s" % exc)
        if not item:
            return self.tg.send(chat_id, "Nothing to file in that message.")

        cs["queue"].append(item)
        self.advance(chat_id, cs)

    def on_command(self, chat_id, cs, text):
        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        if cmd in ("start", "help"):
            return self.tg.send(chat_id, self.help_text())
        if cmd == "status":
            lines = ["Root: <code>%s</code>" % self.cfg.root]
            active = cs.get("active")
            if active:
                lines.append("In progress: %s, waiting on %s"
                             % (describe_kind(active["kind"]), cs.get("stage")))
            else:
                lines.append("Nothing in progress.")
            lines.append("Queued: %d" % len(cs["queue"]))
            if cs.get("last_dest") in self.cfg.by_key:
                lines.append("Last folder: %s" % self.cfg.by_key[cs["last_dest"]].label)
            return self.tg.send(chat_id, "\n".join(lines))
        if cmd == "undo":
            return self.undo_last(chat_id, cs)
        if cmd == "back":
            if cs.get("active") and cs.get("stage") in ("name", "custom"):
                cs.update({"stage": "bucket", "dest": None, "custom_dir": None})
                return self.prompt(chat_id, cs)
            return self.tg.send(chat_id, "Nothing to go back to. /undo puts the "
                                         "last filed item back.")
        if cmd == "cancel":
            dropped = 0
            if cs.get("active"):
                discard(cs["active"])
                dropped += 1
            for queued in cs["queue"]:
                discard(queued)
                dropped += 1
            cs.update({"queue": [], "active": None, "stage": None, "dest": None,
                       "prompt_id": None, "custom_dir": None})
            return self.tg.send(chat_id, "Cleared %d item(s)." % dropped)
        return self.tg.send(chat_id, "Unknown command. Try /help.")

    # -- flow

    def advance(self, chat_id, cs):
        if cs.get("active"):
            return self.refresh_prompt(chat_id, cs)
        if not cs["queue"]:
            return
        cs["active"] = cs["queue"].pop(0)
        cs.update({"stage": "bucket", "dest": None, "custom_dir": None})
        self.prompt(chat_id, cs)

    def prompt_text(self, cs, prefix=""):
        item = cs["active"]
        bits = [describe_kind(item["kind"])]
        if item.get("orig_name"):
            bits.append("<code>%s</code>" % item["orig_name"])
        if item.get("size"):
            bits.append("%.0f KB" % (item["size"] / 1024.0))
        if item.get("caption"):
            bits.append("“%s”" % item["caption"][:60])
        head = "%s%s" % (prefix, " · ".join(bits))
        if cs["queue"]:
            head += "\n<i>%d more waiting</i>" % len(cs["queue"])
        return head + "\n\nWhere does this go?"

    def prompt(self, chat_id, cs, prefix=""):
        sent = self.tg.send(chat_id, self.prompt_text(cs, prefix), self.keyboard(cs))
        cs["prompt_id"] = sent.get("message_id")

    def refresh_prompt(self, chat_id, cs):
        """Keep the live prompt's waiting count honest as an album piles up."""
        if cs.get("stage") != "bucket" or not cs.get("prompt_id"):
            return
        self.tg.edit(chat_id, cs["prompt_id"], self.prompt_text(cs), self.keyboard(cs))

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
        prefix, _, value = (cq.get("data") or "").partition(":")

        # Undo runs after filing, when nothing is pending, so it comes first.
        if prefix == "z":
            return self.undo_last(chat_id, cs, via_edit=msg["message_id"])

        if not cs.get("active"):
            return self.tg.edit(chat_id, msg["message_id"],
                                "That item is no longer pending.")

        if value == "__cancel":
            discard(cs["active"])
            cs.update({"active": None, "stage": None})
            self.tg.edit(chat_id, msg["message_id"], "Discarded.")
            return self.advance(chat_id, cs)

        if value == "__back":
            cs.update({"stage": "bucket", "dest": None, "custom_dir": None,
                       "prompt_id": msg["message_id"]})
            return self.tg.edit(chat_id, msg["message_id"],
                                self.prompt_text(cs), self.keyboard(cs))

        if value == "__custom":
            if not self.cfg.allow_custom:
                return
            cs["stage"] = "custom"
            cs["prompt_id"] = msg["message_id"]
            example = self.cfg.destinations[0].path
            return self.tg.edit(
                chat_id, msg["message_id"],
                "Send a folder path relative to the root.\n"
                "<i>e.g.</i> <code>%s</code>" % example,
                self.back_keyboard())

        if value not in self.cfg.by_key:
            return
        cs["dest"] = value
        if prefix == "q":
            return self.finish(chat_id, cs, "-", via_edit=msg["message_id"])
        cs["stage"] = "name"
        cs["prompt_id"] = msg["message_id"]
        self.tg.edit(chat_id, msg["message_id"],
                     "Filing to <b>%s</b>.\n\nName it, or send <code>-</code> "
                     "for <code>%s</code>."
                     % (self.cfg.by_key[value].label,
                        suggested_name(cs["active"], self.cfg.naming)),
                     self.back_keyboard())

    def on_custom_path(self, chat_id, cs, text):
        try:
            target = safe_join(self.cfg.root, render_path(
                text, kind=cs["active"]["kind"], ext=cs["active"].get("ext", "")))
        except (ValueError, ConfigError) as exc:
            return self.tg.send(chat_id, "Bad path: %s. Try again." % exc)
        cs["custom_dir"] = str(target)
        cs["stage"] = "name"
        rel = target.relative_to(self.cfg.root) if target != self.cfg.root else "."
        sent = self.tg.send(chat_id, "Filing to <code>%s</code>.\n\nName it, or "
                                     "send <code>-</code> for <code>%s</code>."
                            % (rel, suggested_name(cs["active"], self.cfg.naming)),
                            self.back_keyboard())
        cs["prompt_id"] = sent.get("message_id")

    def finish(self, chat_id, cs, name_text, via_edit=None):
        item = cs.get("active")
        if not item:
            return
        name = (suggested_name(item, self.cfg.naming)
                if name_text.strip() in ("-", ".") else name_text)
        try:
            if cs.get("custom_dir"):
                directory = Path(cs["custom_dir"])
            else:
                directory = self.cfg.resolve_dir(
                    self.cfg.by_key[cs["dest"]], item["kind"], item.get("ext", ""))
            dest = file_item(item, directory, name, self.cfg)
        except Exception as exc:
            log("filing failed: %r" % (exc,))
            return self.tg.send(chat_id, "Could not save that: %s" % exc)

        try:
            rel = dest.relative_to(self.cfg.root)
        except ValueError:
            rel = dest
        log("filed -> %s" % rel)
        # Remember enough to put it back, and the size and mtime it had when we
        # wrote it, so undo never touches a file that has since been edited.
        cs["last_filed"] = None
        try:
            st = dest.stat()
            record = {k: item.get(k) for k in
                      ("id", "kind", "ext", "orig_name", "caption", "text",
                       "meta", "media_group_id", "size")}
            record.update({"path": str(dest), "written": st.st_size,
                           "mtime": int(st.st_mtime)})
            cs["last_filed"] = record
        except OSError:
            pass
        confirmation = "✅ <code>%s</code>" % rel
        keyboard = self.undo_keyboard() if cs.get("last_filed") else None
        if via_edit:
            self.tg.edit(chat_id, via_edit, confirmation, keyboard)
        else:
            self.tg.send(chat_id, confirmation, keyboard)

        if cs.get("dest"):
            cs["last_dest"] = cs["dest"]
        cs.update({"active": None, "stage": None, "dest": None,
                   "custom_dir": None, "prompt_id": None})
        self.advance(chat_id, cs)

    def undo_last(self, chat_id, cs, via_edit=None):
        """Put the last filed item back in the queue, if it is untouched."""
        def say(text):
            if via_edit:
                return self.tg.edit(chat_id, via_edit, text)
            return self.tg.send(chat_id, text)

        rec = cs.get("last_filed")
        if not rec:
            return say("Nothing to undo. Only the last filed item can come "
                       "back, and only until the next one.")
        path = Path(rec["path"])
        try:
            st = path.stat()
        except OSError:
            cs["last_filed"] = None
            return say("<code>%s</code> is not where I left it, so I have not "
                       "touched anything." % rec["path"])
        if st.st_size != rec.get("written") or int(st.st_mtime) != rec.get("mtime"):
            cs["last_filed"] = None
            return say("<code>%s</code> has changed since I filed it, so I have "
                       "left it alone." % rec["path"])

        item = {k: v for k, v in rec.items()
                if k not in ("path", "written", "mtime")}
        if item.get("kind") == "text":
            # A note was written from the text we still hold, so removing the
            # file loses nothing: the item goes back in the queue intact.
            try:
                path.unlink()
            except OSError as exc:
                return say("Could not take that back: %s" % exc)
        else:
            try:
                STAGING.mkdir(parents=True, exist_ok=True)
                staged = unique_path(STAGING, str(item.get("id") or "undo"),
                                     item.get("ext") or "")
                shutil.move(str(path), str(staged))
            except OSError as exc:
                return say("Could not take that back: %s" % exc)
            item["staged"] = str(staged)

        cs["last_filed"] = None
        cs["queue"].insert(0, item)
        try:
            rel = path.relative_to(self.cfg.root)
        except ValueError:
            rel = path
        log("undone <- %s" % rel)
        say("↩️ Took <code>%s</code> back." % rel)
        self.advance(chat_id, cs)


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
            "Chute is already running as pid %d.\n"
            "  Stop it first (chute stop), or delete %s if that pid is stale."
            % (existing, LOCK_PATH))
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


def _join_rel(here, name):
    return "%s/%s" % (here, name) if here else name


def browse_folder(root, start="", purpose="these files"):
    """Pick a folder under root. Returns a path relative to root.

    Navigation is numbers, every action is a letter, and every action that
    picks a folder ends the browse. Creating a folder selects it, because
    that is why you were creating it.
    """
    here = "" if is_template(start) else str(start or "").strip("/")
    if here and not (root / here).is_dir():
        here = ""

    while True:
        current = safe_join(root, here) if here else root
        label = here or "the root folder"
        subs = []
        if current.is_dir():
            subs = sorted(p.name for p in current.iterdir()
                          if p.is_dir() and not p.name.startswith("."))

        print("\n  Where should %s go?" % purpose)
        print("  Now in: %s" % label)
        print("  " + "-" * 46)
        for i, name in enumerate(subs, 1):
            print("    %2d. %s" % (i, name))
        if not subs:
            print("    (no folders in here yet)")
        print()
        if subs:
            print("    %-6s open that folder" % ("1-%d" % len(subs)))
        print("    %-6s use this one: %s" % ("s", label))
        print("    %-6s create a new folder in here" % "c")
        print("    %-6s type a path instead" % "t")
        if here:
            print("    %-6s go up" % "u")

        answer = input("\n  > ").strip()
        low = answer.lower()

        if low == "s":
            return here

        if low == "u":
            if not here:
                print("  Already at the top.")
                continue
            parent = str(Path(here).parent)
            here = "" if parent == "." else parent
            continue

        if low == "c":
            name = input("  Name for the new folder "
                         "(use / to nest, e.g. Receipts/2026): ").strip()
            if not name:
                print("  No name given, nothing created.")
                continue
            try:
                target = safe_join(root, _join_rel(here, name.strip("/")))
                target.mkdir(parents=True, exist_ok=True)
            except (ValueError, OSError) as exc:
                print("  Could not create it: %s" % exc)
                continue
            rel = str(target.relative_to(root))
            print("  Created %s, and that is where these will go." % rel)
            return rel

        if low == "t":
            typed = input("  Path under the root: ").strip().lstrip("/")
            if not typed:
                continue
            if is_template(typed):
                try:
                    safe_join(root, render_path(typed))
                except (ValueError, ConfigError) as exc:
                    print("  That path will not work: %s" % exc)
                    continue
                print("  Using %s, expanded when each file is written." % typed)
                return typed
            try:
                target = safe_join(root, typed)
            except ValueError as exc:
                print("  That path will not work: %s" % exc)
                continue
            if not target.exists():
                if not ask_bool("  %s does not exist yet. Create it?" % typed,
                                True):
                    continue
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    print("  Could not create it: %s" % exc)
                    continue
                print("  Created %s." % typed)
            elif not target.is_dir():
                print("  %s is a file, not a folder." % typed)
                continue
            return str(target.relative_to(root))

        if answer.isdigit():
            n = int(answer)
            if 1 <= n <= len(subs):
                here = _join_rel(here, subs[n - 1])
                continue
            print("  There is no %d in that list." % n)
            continue

        print("  Type a number, or s, c, t%s." % (", u" if here else ""))


def edit_destination(root, existing=None):
    """Build or change one button. Returns a destination dict, or None."""
    existing = existing or {}
    print("\n  " + ("Editing a folder button" if existing else "New folder button"))
    label = ask("  What should the button say", existing.get("label", ""))
    if not label:
        print("  No label, nothing added.")
        return None

    dest = {"label": label}
    dest["path"] = browse_folder(root, existing.get("path", ""),
                                 "images and documents")

    by_kind = dict(existing.get("by_kind") or {})
    print("\n  By default everything from this button goes to %s." % dest["path"])
    if ask_bool("  Send voice notes, audio and video somewhere else instead?",
                "media" in by_kind):
        by_kind["media"] = browse_folder(root, by_kind.get("media", ""),
                                         "audio and video")
    else:
        by_kind.pop("media", None)
    if ask_bool("  Send links and forwarded text somewhere else instead?",
                "text" in by_kind):
        by_kind["text"] = browse_folder(root, by_kind.get("text", ""),
                                        "links and notes")
    else:
        by_kind.pop("text", None)
    if by_kind:
        dest["by_kind"] = by_kind
    return dest


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


def edit_destinations(root, destinations):
    dests = [dict(d) for d in destinations]
    while True:
        show_destinations(dests, root)
        if len(dests) > 12:
            print("\n  Note: more than 12 buttons gets unwieldy on a phone.")
        print()
        choice = ask_menu([
            ("a", "add a button"),
            ("e", "edit one"),
            ("r", "remove one"),
            ("m", "move one up or down"),
            ("d", "done"),
        ], default="d" if dests else "a")

        if choice == "a":
            made = edit_destination(root)
            if made:
                dests.append(made)
        elif choice in ("e", "r", "m") and not dests:
            print("  Nothing to work with yet. Add one first.")
        elif choice == "e":
            n = ask("  Which number")
            if n.isdigit() and 1 <= int(n) <= len(dests):
                made = edit_destination(root, dests[int(n) - 1])
                if made:
                    dests[int(n) - 1] = made
        elif choice == "r":
            n = ask("  Which number")
            if n.isdigit() and 1 <= int(n) <= len(dests):
                gone = dests.pop(int(n) - 1)
                print("  Removed %s. The folder on disk is untouched."
                      % gone["label"])
        elif choice == "m":
            n = ask("  Which number")
            if n.isdigit() and 1 <= int(n) <= len(dests):
                i = int(n) - 1
                where = ask("  Move it to which position (1 is first)")
                if where.isdigit() and 1 <= int(where) <= len(dests):
                    dests.insert(int(where) - 1, dests.pop(i))
        elif choice == "d":
            if not dests:
                print("  You need at least one button.")
                continue
            return dests


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


def prompt_root():
    """Ask where files should go. Returns a usable folder, or None to abort."""
    cwd = Path.cwd()
    print("\nWhere should files go on this computer?\n")
    print("  1. %s" % cwd)
    print("     (the folder you are running setup from)")
    print("  2. Somewhere else, and I'll type the path")
    print("\nPick 1 or 2, or just paste a path. Type q to quit setup.")

    while True:
        answer = input("Choice [1]: ").strip() or "1"
        if answer.lower() in ("q", "quit"):
            print("  Nothing saved. Run setup again whenever you like.")
            return None
        if answer == "1":
            root = cwd
        else:
            raw = answer if answer != "2" else input("Path: ").strip()
            try:
                root = resolve_root(raw)
            except ValueError as exc:
                print("  %s. Try again." % exc)
                continue

        if root.exists() and not root.is_dir():
            print("  %s is a file, not a folder. Try again." % root)
            continue
        if not root.exists():
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

        root = root.resolve()
        # Offer to nest, so picking a home or project folder does not mean
        # filed items land loose among whatever is already there.
        if root.name != APP:
            print("\n  Create a '%s' folder inside it, to keep filed items "
                  "together?" % APP)
            print("    y -> %s" % (root / APP))
            print("    n -> %s" % root)
            if input("  [y/N]: ").strip().lower() in ("y", "yes"):
                nested = root / APP
                try:
                    nested.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    print("  could not create it: %s" % exc)
                    continue
                print("  using %s" % nested)
                root = nested
        return root


def setup_destinations(root):
    """First-run buttons: type a name, get a folder of that name. Nothing else.

    Deliberately simpler than edit_destinations. Setup is the wrong moment to
    browse a folder tree; anyone who wants sub-paths or per-kind routing can
    run chute config afterwards.

    Returns the list of destinations, or None if the user asked to go back and
    choose a different root folder.
    """
    print("\n  Type a button name and press Enter. Each one becomes a folder")
    print("  of the same name inside %s." % root)
    print("  A folder that is already there is reused, never overwritten.\n")
    print("    undo    take back the last button")
    print("    back    choose a different root folder")
    print("    done    finish, or just press Enter on an empty line\n")
    dests = []
    made = {}   # label -> folder this run created, so undo can remove it again
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
        if "/" in label or "\\" in label:
            print("  One name per button, and no slashes. Sub-folders and "
                  "per-kind routing come later with:  chute config")
            continue
        # clean_name falls back to a date stamp when nothing usable is left,
        # which would be a baffling folder to end up with. Catch that here.
        if not ILLEGAL.sub("", label).strip().strip(". "):
            print("  There is nothing in that name I can make a folder from.")
            continue
        folder = clean_name(label)
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
        naming = data.get("naming") or {}
        sec = data.get("security") or {}
        cap = data.get("text_capture") or {}
        print("\n" + "=" * 62)
        print("Chute configuration            %s" % path)
        print("=" * 62)
        print("  Root folder    %s" % root)
        print("  Buttons        %d" % len(data.get("destinations") or []))
        print("  Naming         %s%s%s"
              % (naming.get("style", "keep-spaces"),
                 ", lowercase" if naming.get("lowercase") else "",
                 ", date prefix" if naming.get("date_prefix") else ""))
        print("  Notes saved as %s%s"
              % (cap.get("format", "markdown"),
                 " with frontmatter" if cap.get("frontmatter", True) else ""))
        print("  Reply to strangers  %s"
              % ("yes" if sec.get("reply_to_strangers") else "no, stay silent"))
        print("  'Other folder' button  %s"
              % ("on" if sec.get("allow_custom_paths", True) else "off"))
        print("  Allowed Telegram ids   %s"
              % ", ".join(str(i) for i in data.get("allowed_user_ids") or []))
        print()
        choice = ask_menu([
            ("1", "folders and buttons"),
            ("2", "root folder"),
            ("3", "how files are named"),
            ("4", "how links and notes are saved"),
            ("5", "who may use the bot"),
            ("6", "privacy and safety"),
            ("s", "save and exit"),
            ("q", "quit without saving"),
        ], default="s")

        if choice == "1":
            data["destinations"] = edit_destinations(
                root, data.get("destinations") or [])
        elif choice == "2":
            new_root = prompt_root()
            if new_root:
                root = new_root
                data["root"] = str(root)
                print("  Buttons still point at the same relative paths. "
                      "Check them with option 1.")
        elif choice == "3":
            print("\n  Spaces in names are kept as-is by default.")
            style = ask_menu([("1", "keep spaces  (My Photo.jpg)"),
                              ("2", "hyphens      (My-Photo.jpg)"),
                              ("3", "underscores  (My_Photo.jpg)")],
                             default={"keep-spaces": "1", "kebab": "2",
                                      "snake": "3"}[naming.get("style",
                                                               "keep-spaces")])
            naming["style"] = {"1": "keep-spaces", "2": "kebab",
                               "3": "snake"}[style]
            naming["lowercase"] = ask_bool("  Force names to lowercase?",
                                           bool(naming.get("lowercase")))
            naming["date_prefix"] = ask_bool(
                "  Put the date in front of every filename?",
                bool(naming.get("date_prefix")))
            data["naming"] = naming
        elif choice == "4":
            fmt = ask_menu([("1", "Markdown .md, good for Obsidian and Logseq"),
                            ("2", "plain text .txt")],
                           default="2" if cap.get("format") == "txt" else "1")
            cap["format"] = "txt" if fmt == "2" else "markdown"
            if cap["format"] == "markdown":
                cap["frontmatter"] = ask_bool(
                    "  Add YAML frontmatter with the date and source?",
                    bool(cap.get("frontmatter", True)))
            data["text_capture"] = cap
        elif choice == "5":
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
        elif choice == "6":
            sec["reply_to_strangers"] = ask_bool(
                "  Reply 'Not authorised' to strangers? Silence is safer",
                bool(sec.get("reply_to_strangers")))
            sec["allow_custom_paths"] = ask_bool(
                "  Keep the 'Other folder' button, for filing anywhere "
                "under the root?", bool(sec.get("allow_custom_paths", True)))
            data["security"] = sec
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
            print("Apply it with:  chute restart")
            return 0


def cmd_setup(args):
    print("Chute setup\n")
    print("1. In Telegram open @BotFather, send /newbot, follow the prompts.")
    print("2. Paste the token it gives you here.\n")
    token = input("Bot token: ").strip()
    if not token:
        print("No token given, aborting.")
        return 1
    tg = Telegram(token)
    try:
        me = tg.call("getMe")
    except NetworkError as exc:
        print("\nCould not reach Telegram, so the token was never checked.\n  %s"
              % exc)
        return 1
    except ConfigError as exc:
        print("\n%s" % exc)
        return 1
    except Exception as exc:
        print("\nThat token did not work: %s" % exc)
        return 1
    print("\nConnected as @%s." % me.get("username"))

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

    print("\nNow open Telegram, find @%s, and send it any message." % me.get("username"))
    print("Waiting (Ctrl-C to stop)...")
    offset, user_id, deadline = 0, None, time.time() + 300
    while time.time() < deadline and user_id is None:
        try:
            updates = tg.call("getUpdates", offset=offset, timeout=30)
        except Exception as exc:
            print("  ...%s" % exc)
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            frm = (u.get("message") or {}).get("from") or {}
            if frm.get("id"):
                user_id = frm["id"]
                print("\nGot it. You are %s (id %s)."
                      % (frm.get("username") or frm.get("first_name"), user_id))
                break
    if user_id is None:
        print("Timed out without a message. Run setup again.")
        return 1

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
    print("\nWrote %s (readable only by you)." % target)
    print("\nStart it with:      chute install")
    print("Change anything with: chute config")
    return 0


def cmd_check(args):
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
        me = Telegram(cfg.token).call("getMe")
        print("Bot:    @%s  ok" % me.get("username"))
    except ConfigError as exc:
        print("Bot:    FAILED\n  %s" % exc)
        return 1
    except NetworkError as exc:
        print("Bot:    unreachable\n  %s" % exc)
        return 1
    except Exception as exc:
        print("Bot:    unreachable - %s" % exc)
    print("Users:  %s" % ", ".join(str(x) for x in sorted(cfg.allowed)))
    print("Limits: %.0f MB max, %d blocked extensions, custom paths %s"
          % (cfg.max_bytes / 1048576.0, len(cfg.blocked_ext),
             "on" if cfg.allow_custom else "off"))
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
    return 0


def cmd_run(args):
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
        if args.action == "config":
            return cmd_config(args)
        if args.action == "run":
            return cmd_run(args)
    except ConfigError as exc:
        print("Config error: %s" % exc, file=sys.stderr)
        return 2
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        release_lock()
        print("\nstopped")
