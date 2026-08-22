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
      ["inbox", "work", "personal"])
dupes = make_config(root, destinations=[
    {"label": "📥 Inbox", "path": "Inbox", "catch_all": True},
    {"label": "📡 Work", "path": "A"},
    {"label": "🛠 Work", "path": "B"}])
check("duplicate labels get unique keys", [d.key for d in dupes.destinations],
      ["inbox", "work", "work-2"])
check("explicit key honoured",
      make_config(root, destinations=[
          {"label": "X", "path": "X", "key": "kk", "catch_all": True}]
                  ).destinations[0].key, "kk")
check("blocked extensions defaulted", ".app" in cfg.blocked_ext, True)
check("max size clamped to telegram's ceiling",
      make_config(root, security={"max_file_mb": 999}).max_bytes,
      chute.CLOUD_CEILING)
check("but not when the server is your own",
      make_config(root, security={"max_file_mb": 999},
                  api_root="http://localhost:8081").max_bytes,
      999 * 1024 * 1024)
check("and a smaller setting still wins either way",
      make_config(root, security={"max_file_mb": 5},
                  api_root="http://localhost:8081").max_bytes,
      5 * 1024 * 1024)
warn = make_config(root, destinations=[{"label": "D%d" % i, "path": "D%d" % i}
                                       for i in range(14)]).validate_paths()
check("warns past 12 destinations", any("unwieldy" in w for w in warn), True)

section("the landing folder")
flagged = make_config(root, destinations=[
    {"label": "A", "path": "A"},
    {"label": "B", "path": "B", "catch_all": True}])
check("an explicit flag wins", flagged.inbox.key, "b")
check("and is not treated as derived", flagged.inbox_derived, False)
check("nothing is prepended", len(flagged.destinations), 2)

raises("two landing folders are refused", lambda: make_config(root, destinations=[
    {"label": "A", "path": "A", "catch_all": True},
    {"label": "B", "path": "B", "catch_all": True}]), chute.ConfigError)

named = make_config(root, destinations=[{"label": "Inbox", "path": "In"},
                                        {"label": "Work", "path": "Work"}])
check("a button already called Inbox is adopted", named.inbox.key, "inbox")
check("adopting one counts as derived", named.inbox_derived, True)
check("an adopted button is not duplicated", len(named.destinations), 2)

bare = make_config(root, destinations=[{"label": "Work", "path": "Work"}])
check("with none, one is invented", bare.inbox.path, "Inbox")
check("invented one is flagged derived", bare.inbox_derived, True)
check("and gets a button, first in the row",
      [d.key for d in bare.destinations], ["inbox", "work"])
check("deriving warns so it is not silent",
      any("no landing folder is set" in w for w in bare.validate_paths()), True)

by_kind_inbox = make_config(root, destinations=[
    {"label": "In", "path": "In", "catch_all": True,
     "by_kind": {"text": "In/Notes"}}])
check("the landing folder routes by kind too",
      by_kind_inbox.resolve_dir(by_kind_inbox.inbox, "text", ".md"),
      root / "In/Notes")

raises("an escaping landing path is caught", lambda: make_config(
    root, destinations=[{"label": "Bad", "path": "../out", "catch_all": True}]
).validate_paths(), chute.ConfigError)

section("routing")
inbox, work, personal = cfg.destinations
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
      str(dated.resolve_dir(dated.by_key["log"], "image", ".jpg")).endswith(
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

section("auto names: date, time, type word")
stamp = chute.datetime(2026, 8, 20, 18, 48)
check("image", chute.auto_name({"kind": "image"}, now=stamp),
      "2026-08-20 1848 Image")
check("document", chute.auto_name({"kind": "document", "ext": ".pdf"},
                                  now=stamp), "2026-08-20 1848 Document")
check("voice note is audio", chute.auto_name(
    {"kind": "media", "ext": ".ogg"}, now=stamp), "2026-08-20 1848 Audio")
check("mp3 is audio", chute.kind_word({"kind": "media", "ext": ".mp3"}),
      "Audio")
check("mp4 is video", chute.kind_word({"kind": "media", "ext": ".mp4"}),
      "Video")
check("text is a note", chute.kind_word({"kind": "text"}), "Note")
check("caption changes nothing", chute.auto_name(
    {"kind": "image", "caption": "ignored"}, now=stamp),
    "2026-08-20 1848 Image")
check("original filename changes nothing", chute.auto_name(
    {"kind": "document", "orig_name": "Q3 deck.pdf"}, now=stamp),
    "2026-08-20 1848 Document")

section("a caption overrides the auto name")
check("caption wins", chute.name_for({"kind": "image", "caption": "Router "
      "diagram"}), "Router diagram")
check("caption is cleaned", chute.name_for(
    {"kind": "image", "caption": "a/b: c"}), "ab c")
check("first line only", chute.name_for(
    {"kind": "image", "caption": "Title\nsecond line"}), "Title")
check("no caption means auto name", chute.name_for(
    {"kind": "image"}, ).endswith(" Image"), True)
check("junk caption falls back to auto, type word kept", chute.name_for(
    {"kind": "media", "ext": ".ogg", "caption": "///..."}).endswith(" Audio"),
    True)
check("whitespace caption falls back too", chute.name_for(
    {"kind": "document", "caption": "   "}).endswith(" Document"), True)

section("same-minute files get suffixes, not overwrites")
cfg2 = make_config(root)
first = chute.file_item({"kind": "image", "staged": staged("m1.jpg"),
                         "ext": ".jpg"}, root / "Minute",
                        chute.auto_name({"kind": "image"}, now=stamp), cfg2)
second = chute.file_item({"kind": "image", "staged": staged("m2.jpg"),
                          "ext": ".jpg"}, root / "Minute",
                         chute.auto_name({"kind": "image"}, now=stamp), cfg2)
check("first keeps the plain name", first.name, "2026-08-20 1848 Image.jpg")
check("second gets a suffix", second.name, "2026-08-20 1848 Image 2.jpg")

section("moving a filed file")
def filed(name, folder="Inbox", data=b"HELLO"):
    """Write a file where Chute would have, and build its record."""
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(data)
    st = f.stat()
    return {"path": str(f), "stem": f.stem, "ext": f.suffix, "kind": "image",
            "dest": "inbox", "size": st.st_size, "mtime": int(st.st_mtime),
            "at": int(chute.time.time())}

rec = filed("a.jpg")
moved = chute.move_filed(rec, root / "Work", root)
check("lands in the new folder", moved, root / "Work/a.jpg")
check("and is gone from the old one", (root / "Inbox/a.jpg").exists(), False)
check("contents survive", moved.read_bytes(), b"HELLO")

rec2 = filed("a.jpg", data=b"SECOND")
moved2 = chute.move_filed(rec2, root / "Work", root)
check("a name already taken is suffixed", moved2.name, "a 2.jpg")
check("the first file is untouched", moved.read_bytes(), b"HELLO")

same = filed("b.jpg")
before_path = Path(same["path"])
again = chute.move_filed(same, root / "Inbox", root)
check("moving into its own folder does nothing", again, before_path)
check("and does not make a second copy",
      len(list((root / "Inbox").glob("b*.jpg"))), 1)

edited = filed("c.jpg")
Path(edited["path"]).write_bytes(b"CHANGED BY HAND")
onward = chute.move_filed(edited, root / "Work", root)
check("an edited file still moves", onward.exists(), True)
check("carrying its new contents", onward.read_bytes(), b"CHANGED BY HAND")

gone = filed("d.jpg")
Path(gone["path"]).unlink()
raises("a file that walked off is not chased",
       lambda: chute.move_filed(gone, root / "Work", root), chute.NotAsFiled)

restated = filed("e.jpg")
after = chute.move_filed(restated, root / "Work", root)
chute.restat(restated, after, "work")
check("the record follows the file", restated["path"], str(after))
check("and remembers which button it sits under", restated["dest"], "work")

section("deleting a filed file")
doomed = filed("x.jpg")
chute.delete_filed(doomed, root)
check("the file is gone", Path(doomed["path"]).exists(), False)

touched = filed("y.jpg")
Path(touched["path"]).write_bytes(b"EDITED SINCE")
raises("an edited file is not deleted",
       lambda: chute.delete_filed(touched, root), chute.NotAsFiled)
check("it survives", Path(touched["path"]).exists(), True)

strayed = filed("z.jpg")
Path(strayed["path"]).rename(root / "Inbox/renamed by hand.jpg")
raises("a renamed file is not deleted",
       lambda: chute.delete_filed(strayed, root), chute.NotAsFiled)
check("the renamed copy survives",
      (root / "Inbox/renamed by hand.jpg").exists(), True)

section("remembering which message owns which file")
filed_map = {}
now = 1755600000
for i in range(5):
    chute.remember(filed_map, 100 + i, {"path": "p%d" % i, "at": now}, now=now)
check("each message keyed separately", sorted(filed_map), 
      ["100", "101", "102", "103", "104"])

stale = {"old": {"path": "old", "at": now - chute.FILED_TTL - 1}}
chute.remember(stale, 200, {"path": "new", "at": now}, now=now)
check("anything older than a week is forgotten", sorted(stale), ["200"])

many = {}
for i in range(chute.FILED_KEEP + 25):
    chute.remember(many, i, {"path": "p%d" % i, "at": now + i}, now=now)
check("and the count is capped", len(many), chute.FILED_KEEP)
check("keeping the newest", str(chute.FILED_KEEP + 24) in many, True)
check("dropping the oldest", "0" in many, False)

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

section("a note's heading matches the name it actually got")
dupdir = root / "Dups"
first = chute.file_item({"kind": "text", "text": "one"}, dupdir, "Same name", cfg)
second = chute.file_item({"kind": "text", "text": "two"}, dupdir, "Same name", cfg)
check("the second file is suffixed", second.name, "Same name 2.md")
check("and its heading says so", "# Same name 2" in second.read_text(), True)
check("the first is unchanged", "# Same name\n" in first.read_text(), True)


section("forwarded media keep their message in a companion note")
check("a plain photo earns no note",
      chute.sidecar_worthy({"kind": "image", "caption": "receipt"}), False)
check("a forward origin earns one",
      chute.sidecar_worthy({"kind": "image", "meta": {"from": "Amit"}}), True)
check("a second caption line earns one",
      chute.sidecar_worthy({"kind": "image", "caption": "title\ndetail"}), True)
check("a link in the caption earns one",
      chute.sidecar_worthy({"kind": "image",
                            "meta": {"links": ["https://a.com"]}}), True)

fwd = {"kind": "image", "staged": staged("f.jpg"), "ext": ".jpg",
       "caption": "Router teardown\nworth reading in full https://a.com/post",
       "meta": {"from": "Some Channel", "forwarded": "2026-08-19",
                "url": "https://t.me/somechan/55",
                "links": ["https://a.com/post"]}}
fpath = chute.file_item(fwd, root / "Forwards", chute.name_for(fwd), cfg)
fnote = Path(fwd["sidecar"])
fbody = fnote.read_text()
check("file named by the caption's first line", fpath.name,
      "Router teardown.jpg")
check("note sits next to the file, same stem",
      (fnote.parent, fnote.name), (fpath.parent, "Router teardown.md"))
check("origin recorded", 'forwarded-from: "Some Channel"' in fbody, True)
check("forward date recorded", "forwarded-date: 2026-08-19" in fbody, True)
check("t.me source linked", "Source: https://t.me/somechan/55" in fbody, True)
check("full caption kept, second line included",
      "worth reading in full https://a.com/post" in fbody, True)
check("note names its file", 'file: "Router teardown.jpg"' in fbody, True)
check("image embedded", "![](<Router teardown.jpg>)" in fbody, True)

frec = {"path": str(fpath), "stem": fpath.stem, "ext": ".jpg",
        "kind": "image", "dest": "inbox", "size": fpath.stat().st_size,
        "mtime": int(fpath.stat().st_mtime),
        "sidecars": [chute.sidecar_stat(fnote, "")]}
fmoved = chute.move_filed(frec, root / "Work", root)
check("moving the file brings the note",
      (root / "Work/Router teardown.md").is_file(), True)
check("and the old note is gone", fnote.exists(), False)
check("record follows the note",
      chute.sidecars_of(frec)[0]["path"],
      str(root / "Work/Router teardown.md"))

chute.restat(frec, fmoved, "work")
chute.delete_filed(frec, root)
kept = chute.delete_sidecar(frec)
check("deleting the file deletes an untouched note",
      (kept, (root / "Work/Router teardown.md").exists()), ([], False))

edited = {"kind": "document", "staged": staged("g.pdf"), "ext": ".pdf",
          "meta": {"from": "Amit"}, "caption": ""}
gpath = chute.file_item(edited, root / "Forwards", "briefing", cfg)
gnote = Path(edited["sidecar"])
check("non-image gets a link, not an embed",
      "[briefing.pdf](<briefing.pdf>)" in gnote.read_text(), True)
grec = {"sidecars": [chute.sidecar_stat(gnote, "")]}
gnote.write_text(gnote.read_text() + "\nmy own thoughts\n")
check("an edited note survives the delete",
      (chute.delete_sidecar(grec), gnote.exists()), ([gnote], True))

lost = {"sidecars": [{"path": str(root / "Forwards/never was.md"),
                      "size": 1, "mtime": 1}]}
check("a note removed by hand is let go quietly",
      chute.delete_sidecar(lost), [])

section("a record written before 0.4 still carries its note")
legacy_note = root / "Forwards/legacy.md"
legacy_note.write_text("old\n")
legacy = {"sidecar": chute.sidecar_stat(legacy_note)}
check("the single sidecar is read as a list",
      [x["path"] for x in chute.sidecars_of(legacy)], [str(legacy_note)])
check("and deleting still removes it",
      (chute.delete_sidecar(legacy), legacy_note.exists()), ([], False))



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
