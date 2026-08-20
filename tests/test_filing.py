#!/usr/bin/env python3
"""Unit tests: naming, path safety, config validation, writing to disk."""
import shutil
import sys
import tempfile
from pathlib import Path

from harness import check, raises, section, make_config, report, REPO  # noqa: F401
import chute

root = Path(tempfile.mkdtemp()).resolve() / "Root"
(root / "Work" / "Attachments").mkdir(parents=True)
(root / "Media" / "Work").mkdir(parents=True)

section("filenames")
check("spaces preserved", chute.clean_name("Q3 Pricing Table"),
      "Q3 Pricing Table")
check("illegal chars stripped", chute.clean_name('a/b:c*d?e"f<g>h|i'), "abcdefghi")
check("whitespace collapsed", chute.clean_name("  too   many   "), "too many")
check("leading dots stripped", chute.clean_name("...hidden..."), "hidden")
check("truncated to limit", len(chute.clean_name("x" * 300)), 120)
check("custom limit", len(chute.clean_name("x" * 300, {"max_length": 40})), 40)
check("empty falls back", chute.clean_name("///", fallback="FB"), "FB")
check("newlines flattened", chute.clean_name("line1\nline2"), "line1 line2")
check("hebrew preserved", chute.clean_name("דוד תמונה"), "דוד תמונה")
check("kebab style", chute.clean_name("Two Words", {"style": "kebab"}), "Two-Words")
check("snake style", chute.clean_name("Two Words", {"style": "snake"}), "Two_Words")
check("lowercase option", chute.clean_name("Two Words", {"lowercase": True}),
      "two words")
check("windows reserved stem", chute.clean_name("CON"), "CON_")

section("filename spoofing")
# U+202E flips rendering so "gpj.exe" can display as "exe.jpg" in a file browser.
check("bidi override stripped", chute.clean_name("photo‮gpj.exe"),
      "photogpj.exe")
check("bidi isolates stripped", chute.clean_name("a⁦b⁩c"), "abc")
check("rtl text itself survives", chute.clean_name("קובץ חדש"), "קובץ חדש")
check("del char stripped", chute.clean_name("a\x7fb"), "ab")

section("slugs for callback data")
check("emoji dropped", chute.slugify("📡 Work"), "work")
check("punctuation folded", chute.slugify("Work / Clients!"), "work-clients")
check("accents folded", chute.slugify("Café Notes"), "cafe-notes")
check("slug length capped", len(chute.slugify("x" * 99)), 32)
check("empty slug has fallback", chute.slugify("📡"), "dest")

section("path safety")
check("simple join", chute.safe_join(root, "Work"), (root / "Work"))
check("leading slash tolerated", chute.safe_join(root, "/Work"),
      (root / "Work"))
check("backslashes normalised", chute.safe_join(root, "Work\\Attachments"),
      (root / "Work" / "Attachments"))
check("empty means root", chute.safe_join(root, ""), root)
for bad, why in [("../../etc", "traversal"),
                 ("Work/../../../tmp", "nested traversal"),
                 (".git/hooks", "dot folder"),
                 ("Work/.ssh", "nested dot folder")]:
    raises("blocks %s" % why, lambda b=bad: chute.safe_join(root, b), ValueError)

section("path templates")
now = chute.datetime(2026, 8, 20, 14, 30)
check("year token", chute.render_path("Inbox/{year}", now=now), "Inbox/2026")
check("month/day tokens", chute.render_path("{year}/{month}/{day}", now=now),
      "2026/08/20")
check("date token", chute.render_path("Log/{date}", now=now), "Log/2026-08-20")
check("kind token", chute.render_path("By kind/{kind}", kind="media"),
      "By kind/media")
check("ext token", chute.render_path("By ext/{ext}", ext=".PDF"), "By ext/PDF")
check("plain path untouched", chute.render_path("Just/A/Path"), "Just/A/Path")
raises("unknown token errors", lambda: chute.render_path("{nope}"), chute.ConfigError)

section("config validation")
raises("empty token rejected",
       lambda: make_config(root, bot_token=""), chute.ConfigError)
raises("empty allowlist rejected",
       lambda: make_config(root, allowed_user_ids=[]), chute.ConfigError)
raises("no destinations rejected",
       lambda: make_config(root, destinations=[]), chute.ConfigError)
raises("destination without path rejected",
       lambda: make_config(root, destinations=[{"label": "X"}]), chute.ConfigError)
raises("destination without label rejected",
       lambda: make_config(root, destinations=[{"path": "X"}]), chute.ConfigError)
raises("unknown kind rejected",
       lambda: make_config(root, destinations=[
           {"label": "X", "path": "X", "by_kind": {"picture": "Y"}}]),
       chute.ConfigError)
raises("bad naming style rejected",
       lambda: make_config(root, naming={"style": "shouty"}), chute.ConfigError)
raises("escaping destination path rejected",
       lambda: make_config(root, destinations=[
           {"label": "Bad", "path": "../outside"}]).validate_paths(),
       chute.ConfigError)
raises("dot-folder destination rejected",
       lambda: make_config(root, destinations=[
           {"label": "Bad", "path": ".git/objects"}]).validate_paths(),
       chute.ConfigError)

cfg = make_config(root)
check("keys derived from labels", [d.key for d in cfg.destinations],
      ["work", "personal"])
dupes = make_config(root, destinations=[{"label": "📡 Work", "path": "A"},
                                        {"label": "🛠 Work", "path": "B"}])
check("duplicate labels get unique keys", [d.key for d in dupes.destinations],
      ["work", "work-2"])
check("explicit key honoured",
      make_config(root, destinations=[{"label": "X", "path": "X", "key": "kk"}]
                  ).destinations[0].key, "kk")
check("blocked extensions defaulted", ".app" in cfg.blocked_ext, True)
check("max size clamped to telegram ceiling",
      make_config(root, security={"max_file_mb": 999}).max_bytes,
      chute.TG_DOWNLOAD_CEILING)
warn = make_config(root, destinations=[{"label": "D%d" % i, "path": "D%d" % i}
                                       for i in range(14)]).validate_paths()
check("warns past 12 destinations", any("unwieldy" in w for w in warn), True)

section("routing")
work, personal = cfg.destinations
check("image uses default path", cfg.resolve_dir(work, "image", ".jpg"),
      root / "Work/Attachments")
check("document uses default path", cfg.resolve_dir(work, "document", ".pdf"),
      root / "Work/Attachments")
check("media overridden by by_kind", cfg.resolve_dir(work, "media", ".ogg"),
      root / "Media/Work")
check("text overridden by by_kind", cfg.resolve_dir(work, "text", ".md"),
      root / "Work/Inbox")
check("destination without by_kind falls through",
      cfg.resolve_dir(personal, "media", ".ogg"), root / "Personal/Attachments")
dated = make_config(root, destinations=[{"label": "Log", "path": "Log/{year}"}])
check("template resolved at file time",
      str(dated.resolve_dir(dated.destinations[0], "image", ".jpg")).endswith(
          "Log/%s" % chute.datetime.now().strftime("%Y")), True)

section("writing files")
def staged(name, data=b"X"):
    p = root.parent / name
    p.write_bytes(data)
    return str(p)

d1 = chute.file_item({"kind": "image", "staged": staged("a.png"), "ext": ".png"},
                     root / "Work/Attachments", "Router diagram", cfg)
check("written where told", d1, root / "Work/Attachments/Router diagram.png")
check("staged source consumed", Path(root.parent / "a.png").exists(), False)
d2 = chute.file_item({"kind": "image", "staged": staged("b.png", b"Y"), "ext": ".png"},
                     root / "Work/Attachments", "Router diagram", cfg)
check("collision suffixed", d2.name, "Router diagram 2.png")
check("original not overwritten", d1.read_bytes(), b"X")
d3 = chute.file_item({"kind": "document", "staged": staged("c.pdf"), "ext": ".pdf"},
                     root / "New Folder", "Contract.pdf", cfg)
check("no doubled extension", d3.name, "Contract.pdf")
check("missing folder created", d3.parent.is_dir(), True)

section("date prefix applies exactly once")
dcfg = make_config(root, naming={"date_prefix": True})
item = {"kind": "image", "caption": "Receipt", "ext": ".jpg"}
name = chute.suggested_name(item, dcfg.naming)
item["staged"] = staged("d.jpg")
out = chute.file_item(item, root / "Dated", name, dcfg)
today = chute.datetime.now().strftime("%Y-%m-%d")
check("prefixed once, not twice", out.name, "%s Receipt.jpg" % today)

section("text capture")
note = chute.file_item(
    {"kind": "text", "text": "read this https://a.com",
     "meta": {"from": "Amit", "forwarded": "2026-08-19"}},
    root / "Work/Inbox", "Saved article", cfg)
body = note.read_text()
check("markdown extension", note.suffix, ".md")
check("frontmatter written", body.startswith("---\ncreated:"), True)
check("forward source recorded", 'forwarded-from: "Amit"' in body, True)
check("body retained", "read this https://a.com" in body, True)
plain = chute.file_item({"kind": "text", "text": "just text"}, root / "Plain", "n",
                        make_config(root, text_capture={"format": "txt"}))
check("txt format honoured", (plain.suffix, plain.read_text()), (".txt", "just text\n"))
nofm = chute.file_item({"kind": "text", "text": "no fm"}, root / "Plain", "n2",
                       make_config(root, text_capture={"frontmatter": False}))
check("frontmatter can be off", nofm.read_text().startswith("---"), False)

section("suggested names")
check("caption wins", chute.suggested_name({"kind": "image", "caption": "A B"}), "A B")
check("filename stem used", chute.suggested_name(
    {"kind": "document", "orig_name": "Q3 deck.pdf"}), "Q3 deck")
check("url becomes host plus slug", chute.suggested_name(
    {"kind": "text", "text": "https://example.com/blog/some-article"}),
    "example.com some article")
check("text beats url", chute.suggested_name(
    {"kind": "text", "text": "Worth reading\nhttps://x.com/a"}), "Worth reading")

section("single instance lock")
import os
chute.LOCK_PATH = root.parent / "chute.lock"
chute.acquire_lock()
check("lock records our pid", chute.LOCK_PATH.read_text(), str(os.getpid()))
chute.acquire_lock()
check("re-locking our own pid is fine", chute.LOCK_PATH.exists(), True)

# pid 1 is always alive and, for a non-root user, os.kill raises PermissionError
# rather than succeeding. Treating that as "dead" would let a second copy start.
chute.LOCK_PATH.write_text("1")
raises("live foreign pid blocks start", chute.acquire_lock, SystemExit)
check("pid 1 seen as alive", chute.pid_alive(1), True)
check("absurd pid seen as dead", chute.pid_alive(999999), False)

chute.LOCK_PATH.write_text("999999")
chute.acquire_lock()
check("stale lock is reclaimed", chute.LOCK_PATH.read_text(), str(os.getpid()))
chute.release_lock()
check("release removes the lock", chute.LOCK_PATH.exists(), False)

shutil.rmtree(root.parent, ignore_errors=True)
sys.exit(report())
