#!/usr/bin/env python3
"""Conversation tests: drive the bot against a fake Telegram, no network."""
import shutil
import sys
import tempfile
from pathlib import Path

from harness import check, section, make_config, report  # noqa: F401
import chute

root = Path(tempfile.mkdtemp()).resolve() / "Root"
(root / "Work" / "Attachments").mkdir(parents=True)
chute.STAGING = root.parent / "staging"
chute.STATE_PATH = root.parent / "state.json"
chute.LOG_PATH = root.parent / "chute.log"
OWNER = CHAT = 555

sent, edits, edit_kb, mid = [], [], [], [100]


class FakeTelegram:
    def __init__(self, *a):
        pass

    def call(self, method, **p):
        return {"username": "testbot"}

    def send(self, chat, text, keyboard=None):
        mid[0] += 1
        sent.append((text, keyboard))
        return {"message_id": mid[0]}

    def edit(self, chat, m, text, keyboard=None):
        edits.append(text)
        edit_kb.append(keyboard)
        return {}

    def ack(self, cid, text=None):
        pass

    def download(self, file_id, dest, max_bytes):
        Path(dest).write_bytes(b"FAKE")
        return "photos/f.jpg"


chute.Telegram = FakeTelegram
cfg = make_config(root)
bot = chute.Bot(cfg)


def photo(n, caption=None, group=None):
    m = {"message_id": n, "chat": {"id": CHAT}, "from": {"id": OWNER},
         "photo": [{"file_id": "f%d" % n, "file_size": 10}]}
    if caption:
        m["caption"] = caption
    if group:
        m["media_group_id"] = group
    return {"update_id": n, "message": m}


def document(n, filename):
    return {"update_id": n, "message": {
        "message_id": n, "chat": {"id": CHAT}, "from": {"id": OWNER},
        "document": {"file_id": "d%d" % n, "file_name": filename,
                     "file_size": 10, "mime_type": "application/octet-stream"}}}


def text(n, body, uid=OWNER):
    return {"update_id": n, "message": {"message_id": n, "chat": {"id": CHAT},
                                        "from": {"id": uid}, "text": body}}


def tap(n, data):
    return {"update_id": n, "callback_query": {
        "id": "c%d" % n, "from": {"id": OWNER}, "data": data,
        "message": {"message_id": mid[0], "chat": {"id": CHAT}}}}


import re as _re

DATED = _re.compile(r"\d{4}-\d{2}-\d{2} \d{4} (Image|Document|Audio|Video|Note)( \d+)?\.\w+$")

def snap(folder):
    return set(folder.glob("*")) if folder.is_dir() else set()

def added(folder, before):
    return sorted(set(snap(folder)) - before)


section("keyboard is built from config")
bot.handle(photo(1, caption="q3 pricing table"))
keys = [b["callback_data"] for row in sent[-1][1] for b in row]
labels = [b["text"] for row in sent[-1][1] for b in row]
check("one button per destination", keys[:3],
      ["b:inbox", "b:work", "b:personal"])
check("labels come from config", labels[:3],
      ["📥 Inbox", "📡 Work", "🏠 Personal"])
check("only discard appended", keys[-1], "b:__cancel")
check("no custom-path button", "b:__custom" in keys, False)
check("no repeat button either", any(k.startswith("q:") for k in keys), False)

section("image, tap, filed at once")
check("asked where it goes", "Where does this go?" in sent[-1][0], True)
before = snap(root / "Work/Attachments")
bot.handle(tap(2, "b:work"))
got = added(root / "Work/Attachments", before)
check("one file appeared, no name prompt", len(got), 1)
check("the caption named the file", got[0].name, "q3 pricing table.jpg")
check("confirmed in place with the path", got[0].name in edits[-1], True)
check("nothing ever asked for a name",
      any("Name it" in e for e in edits), False)
check("staging left clean", list(chute.STAGING.iterdir()), [])

section("a captionless photo names the same way")
bot.handle(photo(4))
before = snap(root / "Personal/Attachments")
bot.handle(tap(5, "b:personal"))
got = added(root / "Personal/Attachments", before)
check("one file appeared", len(got), 1)
check("dated Image name", got[0].name.endswith(" Image.jpg") and
      bool(DATED.match(got[0].name)), True)

section("text after filing is a new item, not a name")
bot.handle(text(6, "Kitchen tiles"))
check("it queued as its own note", "Where does this go?" in sent[-1][0], True)
bot.handle(text(7, "/cancel"))

section("album queues, repeat button files fast")
sent.clear(); edits.clear()
for n in (10, 11, 12):
    bot.handle(photo(n, group="G1"))
check("only one prompt for three photos",
      sum("Where does this go?" in s[0] for s in sent), 1)
check("waiting count updates to 1", "1 more waiting" in edits[0], True)
check("waiting count updates to 2", "2 more waiting" in edits[-1], True)
for n in (13, 14, 15):
    bot.handle(tap(n, "b:work"))
check("all three filed by three taps",
      len(list((root / "Work/Attachments").glob("*.jpg"))), 4)
check("queue drained", bot.chat_state(CHAT)["queue"], [])

section("link capture becomes a note")
bot.handle(text(20, "https://example.com/blog/some-article"))
bot.handle(tap(21, "b:work"))
notes = list((root / "Work/Inbox").glob("*.md"))
check("routed to the by_kind text folder", len(notes), 1)
check("link kept in the body",
      "example.com/blog/some-article" in notes[0].read_text(), True)

section("a stale custom-path tap does nothing")
bot.handle(photo(30, caption="still here"))
bot.handle(tap(31, "b:__custom"))
check("old keyboard button is ignored",
      bot.chat_state(CHAT)["active"] is not None, True)
before = snap(root / "Work/Attachments")
bot.handle(tap(32, "b:work"))
check("a real tap still files it",
      len(added(root / "Work/Attachments", before)), 1)

section("blocked extensions")
before = len(list(root.rglob("*")))
bot.handle(document(38, "installer.app"))
check("blocked file refused", "blocked for safety" in sent[-1][0], True)
check("nothing staged for it", list(chute.STAGING.iterdir()), [])
check("nothing written", len(list(root.rglob("*"))), before)

section("discard, cancel, authorisation")
bot.handle(photo(40))
check("staged while pending", len(list(chute.STAGING.iterdir())), 1)
bot.handle(tap(41, "b:__cancel"))
check("discard cleans staging", list(chute.STAGING.iterdir()), [])
for n in (42, 43, 44):
    bot.handle(photo(n))
bot.handle(text(45, "/cancel"))
check("cancel clears queue and staging",
      (bot.chat_state(CHAT)["queue"], list(chute.STAGING.iterdir())), ([], []))
check("cancel reports the count", "Cleared 3 item(s)" in sent[-1][0], True)
count_before = len(list(root.rglob("*.jpg")))
bot.handle(photo(50))
sent.clear()
bot.handle(text(51, "steal", uid=999))
check("stranger gets no reply at all", sent, [])
check("stranger wrote nothing", len(list(root.rglob("*.jpg"))), count_before)
bot.handle(text(52, "/cancel"))

section("help lists the configured folders")
bot.handle(text(55, "/help"))
check("help names a destination", "📡 Work" in sent[-1][0], True)
check("help shows its path", "Work/Attachments" in sent[-1][0], True)

section("restart mid-flow")
sent.clear()
bot.handle(photo(61, caption="half done"))
bot.persist()
resumed = chute.Bot(make_config(root))
sent.clear()
resumed.recover()
check("re-prompts after restart",
      "Picking up where we left off" in sent[-1][0], True)
before = snap(root / "Personal/Attachments")
resumed.handle(tap(62, "b:personal"))
check("still files after restart",
      len(added(root / "Personal/Attachments", before)), 1)

section("status")
resumed.handle(text(70, "/status"))
check("reports idle", "Nothing in progress." in sent[-1][0], True)
check("reports the root", str(root) in sent[-1][0], True)

def keys_of(keyboard):
    return [b["callback_data"] for row in keyboard or [] for b in row]


# The restart section left this instance holding an item its twin already
# filed, so clear it before driving the flow again.
bot.handle(text(79, "/cancel"))

section("undo a filing")
sent.clear(); edits.clear(); edit_kb.clear()
bot.handle(photo(91, caption="undo me"))
before = snap(root / "Work/Attachments")
bot.handle(tap(92, "b:work"))
new = added(root / "Work/Attachments", before)
filed = new[0] if new else root / "Work/Attachments/missing"
check("filed to begin with", filed.exists(), True)
check("confirmation offers undo", keys_of(edit_kb[-1]), ["z:last"])
bot.handle(tap(94, "z:last"))
check("the file is out of the tree again", filed.exists(), False)
check("and back in staging", len(list(chute.STAGING.iterdir())), 1)
check("re-prompted for a folder", "Where does this go?" in sent[-1][0], True)
before = snap(root / "Personal/Attachments")
bot.handle(tap(95, "b:personal"))
refiled_new = added(root / "Personal/Attachments", before)
check("refiled where the second answer said", len(refiled_new), 1)
check("staging clean again", list(chute.STAGING.iterdir()), [])

bot.handle(text(97, "/undo"))
check("the refiling can be undone as well", refiled_new[0].exists(), False)
bot.handle(text(98, "/undo"))
check("but the same filing cannot be undone twice",
      "Nothing to undo" in sent[-1][0], True)
bot.handle(text(99, "/cancel"))
check("cancel clears what undo put back",
      (bot.chat_state(CHAT)["queue"], list(chute.STAGING.iterdir())), ([], []))

section("undo a note")
bot.handle(text(100, "https://example.com/undo-note"))
before = snap(root / "Work/Inbox")
bot.handle(tap(101, "b:work"))
new_notes = added(root / "Work/Inbox", before)
check("one dated note written", len(new_notes) == 1 and
      bool(DATED.match(new_notes[0].name)), True)
note = new_notes[0]
bot.handle(text(103, "/undo"))
check("note removed again", note.exists(), False)
before = snap(root / "Work/Inbox")
bot.handle(tap(104, "b:work"))
renotes = added(root / "Work/Inbox", before)
check("the same note can be filed again", len(renotes), 1)
note = renotes[0]
check("the link survived the round trip",
      "example.com/undo-note" in note.read_text(), True)

section("undo keeps its hands off a changed file")
bot.handle(photo(110, caption="edited later"))
before = snap(root / "Work/Attachments")
bot.handle(tap(111, "b:work"))
edited = added(root / "Work/Attachments", before)[0]
edited.write_bytes(b"SOMEONE ELSE CHANGED THIS")
bot.handle(text(113, "/undo"))
check("an edited file is left where it is", edited.exists(), True)
check("and the reason is given", "changed since I filed it" in sent[-1][0], True)

bot.handle(photo(114, caption="moved later"))
before = snap(root / "Work/Attachments")
bot.handle(tap(115, "b:work"))
moved = added(root / "Work/Attachments", before)[0]
moved.rename(root / "Work/moved by hand.jpg")
bot.handle(text(117, "/undo"))
check("a file that walked off is not chased",
      (root / "Work/moved by hand.jpg").exists(), True)
check("and that is said plainly", "not where I left it" in sent[-1][0], True)

section("undo survives a restart")
bot.handle(photo(120, caption="restart undo"))
before = snap(root / "Work/Attachments")
bot.handle(tap(121, "b:work"))
last = added(root / "Work/Attachments", before)[0]
bot.persist()
after = chute.Bot(make_config(root))
after.handle(text(123, "/undo"))
check("the new process can still undo it", last.exists(), False)
after.handle(text(124, "/cancel"))

section("/history shows what went where")
after.handle(text(130, "/history"))
hist = sent[-1][0]
check("newest first header", hist.startswith("<b>Filed</b> (newest first)"),
      True)
check("shows filed paths", "Work/Attachments" in hist, True)
check("the undone filing is marked", "</s> undone" in hist, True)
check("timestamps rendered", bool(_re.search(r"\d{2} \w{3} \d{2}:\d{2}", hist)),
      True)
check("history survived the restart",
      len(after.chat_state(CHAT)["history"]) > 3, True)

fresh_chat = chute.Bot(make_config(root))
fresh_chat.state["chats"] = {}
sent.clear()
fresh_chat.handle(text(131, "/history"))
check("empty history says so", sent[-1][0], "Nothing filed yet.")

section("history is bounded")
cs = after.chat_state(CHAT)
cs["history"] = [{"at": 1755600000 + i, "path": "P/%d.jpg" % i,
                  "kind": "Image"} for i in range(300)]
before_photo = snap(root / "Work/Attachments")
after.handle(photo(140, caption="cap check"))
after.handle(tap(141, "b:work"))
check("trimmed to the keep limit", len(cs["history"]), chute.HISTORY_KEEP)
check("newest entry is the new filing",
      cs["history"][-1]["path"].endswith("cap check.jpg"), True)
after.handle(text(142, "/history"))
check("only a page is shown plus a count of older",
      "and %d older" % (chute.HISTORY_KEEP - chute.HISTORY_SHOW)
      in sent[-1][0], True)

shutil.rmtree(root.parent, ignore_errors=True)
sys.exit(report())
