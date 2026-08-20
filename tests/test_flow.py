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


section("keyboard is built from config")
bot.handle(photo(1, caption="q3 pricing table"))
keys = [b["callback_data"] for row in sent[-1][1] for b in row]
labels = [b["text"] for row in sent[-1][1] for b in row]
check("one button per destination", keys[:2], ["b:work", "b:personal"])
check("labels come from config", labels[:2], ["📡 Work", "🏠 Personal"])
check("custom and discard appended", keys[-2:], ["b:__custom", "b:__cancel"])
check("no repeat button before a first file", "q:work" in keys, False)

section("image, button, name")
check("asked where it goes", "Where does this go?" in sent[-1][0], True)
bot.handle(tap(2, "b:work"))
check("asked for a name", "send <code>-</code>" in edits[-1], True)
check("caption offered as suggestion", "q3 pricing table" in edits[-1], True)
bot.handle(text(3, "-"))
check("filed under the caption",
      (root / "Work/Attachments/q3 pricing table.jpg").exists(), True)
check("confirmed with a relative path",
      "Work/Attachments/q3 pricing table.jpg" in sent[-1][0], True)
check("staging left clean", list(chute.STAGING.iterdir()), [])

section("typed name beats the caption")
bot.handle(photo(4, caption="ignore me"))
bot.handle(tap(5, "b:personal"))
bot.handle(text(6, "Kitchen tiles"))
check("used the typed name",
      (root / "Personal/Attachments/Kitchen tiles.jpg").exists(), True)
check("destination folder created on demand",
      (root / "Personal/Attachments").is_dir(), True)

section("album queues, repeat button files fast")
sent.clear(); edits.clear()
for n in (10, 11, 12):
    bot.handle(photo(n, group="G1"))
check("only one prompt for three photos",
      sum("Where does this go?" in s[0] for s in sent), 1)
check("waiting count updates to 1", "1 more waiting" in edits[0], True)
check("waiting count updates to 2", "2 more waiting" in edits[-1], True)
keys = [b["callback_data"] for row in sent[-1][1] for b in row]
check("repeat button offered", "q:personal" in keys, True)
for n in (13, 14, 15):
    bot.handle(tap(n, "q:work"))
check("all three filed",
      len(list((root / "Work/Attachments").glob("*.jpg"))), 4)
check("queue drained", bot.chat_state(CHAT)["queue"], [])
check("no name prompt on the fast path",
      any("Work/Attachments" in e for e in edits), True)

section("link capture becomes a note")
bot.handle(text(20, "https://example.com/blog/some-article"))
bot.handle(tap(21, "b:work"))
bot.handle(text(22, "-"))
notes = list((root / "Work/Inbox").glob("*.md"))
check("routed to the by_kind text folder", len(notes), 1)
check("link kept in the body",
      "example.com/blog/some-article" in notes[0].read_text(), True)

section("custom folder path")
bot.handle(photo(30))
bot.handle(tap(31, "b:__custom"))
check("asked for a path", "relative to the root" in edits[-1], True)
bot.handle(text(32, "../../../etc"))
check("traversal refused", "Bad path" in sent[-1][0], True)
bot.handle(text(33, ".git/hooks"))
check("dot folder refused", "Bad path" in sent[-1][0], True)
bot.handle(text(34, "Work/Attachments/Brand Kit"))
bot.handle(text(35, "Logo dark"))
check("filed into the custom folder",
      (root / "Work/Attachments/Brand Kit/Logo dark.jpg").exists(), True)

section("blocked extensions")
before = len(list(root.rglob("*")))
bot.handle(document(38, "installer.app"))
check("blocked file refused", "blocked by config" in sent[-1][0], True)
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
resumed.handle(tap(62, "b:personal"))
resumed.handle(text(63, "-"))
check("still files after restart",
      (root / "Personal/Attachments/half done.jpg").exists(), True)

section("status")
resumed.handle(text(70, "/status"))
check("reports idle", "Nothing in progress." in sent[-1][0], True)
check("reports the root", str(root) in sent[-1][0], True)

def keys_of(keyboard):
    return [b["callback_data"] for row in keyboard or [] for b in row]


section("backing out of a wrong folder")
# The restart section left this instance holding an item its twin already
# filed, so clear it before driving the flow again.
bot.handle(text(79, "/cancel"))
sent.clear(); edits.clear(); edit_kb.clear()
bot.handle(photo(80, caption="wrong tap"))
bot.handle(tap(81, "b:work"))
check("name prompt offers a way back", keys_of(edit_kb[-1]),
      ["b:__back", "b:__cancel"])
bot.handle(tap(82, "b:__back"))
check("back returns to the folder question",
      "Where does this go?" in edits[-1], True)
check("folder buttons are there again", "b:personal" in keys_of(edit_kb[-1]),
      True)
bot.handle(tap(83, "b:personal"))
bot.handle(text(84, "Second thoughts"))
check("filed where the second tap said",
      (root / "Personal/Attachments/Second thoughts.jpg").exists(), True)
check("nothing left under the first folder",
      (root / "Work/Attachments/Second thoughts.jpg").exists(), False)

bot.handle(photo(85))
bot.handle(tap(86, "b:work"))
bot.handle(text(87, "/back"))
check("/back does the same", "Where does this go?" in sent[-1][0], True)
bot.handle(tap(88, "b:__custom"))
check("the custom path prompt has a way back too", keys_of(edit_kb[-1]),
      ["b:__back", "b:__cancel"])
bot.handle(tap(89, "b:__back"))
check("back out of the custom path prompt",
      "Where does this go?" in edits[-1], True)
bot.handle(text(90, "/cancel"))

section("undo a filing")
sent.clear(); edits.clear(); edit_kb.clear()
bot.handle(photo(91, caption="undo me"))
bot.handle(tap(92, "b:work"))
bot.handle(text(93, "-"))
filed = root / "Work/Attachments/undo me.jpg"
check("filed to begin with", filed.exists(), True)
check("confirmation offers undo", keys_of(sent[-1][1]), ["z:last"])
bot.handle(tap(94, "z:last"))
check("the file is out of the tree again", filed.exists(), False)
check("and back in staging", len(list(chute.STAGING.iterdir())), 1)
check("re-prompted for a folder", "Where does this go?" in sent[-1][0], True)
bot.handle(tap(95, "b:personal"))
bot.handle(text(96, "-"))
check("refiled where the second answer said",
      (root / "Personal/Attachments/undo me.jpg").exists(), True)
check("staging clean again", list(chute.STAGING.iterdir()), [])

bot.handle(text(97, "/undo"))
check("the refiling can be undone as well",
      (root / "Personal/Attachments/undo me.jpg").exists(), False)
bot.handle(text(98, "/undo"))
check("but the same filing cannot be undone twice",
      "Nothing to undo" in sent[-1][0], True)
bot.handle(text(99, "/cancel"))
check("cancel clears what undo put back",
      (bot.chat_state(CHAT)["queue"], list(chute.STAGING.iterdir())), ([], []))

section("undo a note")
bot.handle(text(100, "https://example.com/undo-note"))
bot.handle(tap(101, "b:work"))
bot.handle(text(102, "note undo"))
note = root / "Work/Inbox/note undo.md"
check("note written", note.exists(), True)
bot.handle(text(103, "/undo"))
check("note removed again", note.exists(), False)
bot.handle(tap(104, "b:work"))
bot.handle(text(105, "note undo"))
check("the same note can be filed again", note.exists(), True)
check("the link survived the round trip",
      "example.com/undo-note" in note.read_text(), True)

section("undo keeps its hands off a changed file")
bot.handle(photo(110, caption="edited later"))
bot.handle(tap(111, "b:work"))
bot.handle(text(112, "-"))
edited = root / "Work/Attachments/edited later.jpg"
edited.write_bytes(b"SOMEONE ELSE CHANGED THIS")
bot.handle(text(113, "/undo"))
check("an edited file is left where it is", edited.exists(), True)
check("and the reason is given", "changed since I filed it" in sent[-1][0], True)

bot.handle(photo(114, caption="moved later"))
bot.handle(tap(115, "b:work"))
bot.handle(text(116, "-"))
moved = root / "Work/Attachments/moved later.jpg"
moved.rename(root / "Work/moved by hand.jpg")
bot.handle(text(117, "/undo"))
check("a file that walked off is not chased",
      (root / "Work/moved by hand.jpg").exists(), True)
check("and that is said plainly", "not where I left it" in sent[-1][0], True)

section("undo survives a restart")
bot.handle(photo(120, caption="restart undo"))
bot.handle(tap(121, "b:work"))
bot.handle(text(122, "-"))
bot.persist()
after = chute.Bot(make_config(root))
after.handle(text(123, "/undo"))
check("the new process can still undo it",
      (root / "Work/Attachments/restart undo.jpg").exists(), False)
after.handle(text(124, "/cancel"))

shutil.rmtree(root.parent, ignore_errors=True)
sys.exit(report())
