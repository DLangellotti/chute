#!/usr/bin/env python3
"""Conversation tests: drive the bot against a fake Telegram, no network."""
import re
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

sent, edits, edit_kb, edit_ids, mid = [], [], [], [], [100]
edit_fails = [False]          # simulate Telegram refusing to edit an old message

DATED = re.compile(
    r"\d{4}-\d{2}-\d{2} \d{4} (Image|Document|Audio|Video|Note)( \d+)?\.\w+$")


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
        if edit_fails[0]:
            return None       # too old for Telegram to edit
        edit_ids.append(m)
        edits.append(text)
        edit_kb.append(keyboard)
        return {"message_id": m}

    def ack(self, cid, text=None):
        pass

    def download(self, file_id, dest, max_bytes):
        Path(dest).write_bytes(b"FAKE")
        return "photos/f.jpg"


chute.Telegram = FakeTelegram
bot = chute.Bot(make_config(root))

INBOX = root / "Inbox"
WORK = root / "Work/Attachments"
PERSONAL = root / "Personal/Attachments"


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


def tap(n, data, message_id=None):
    """A button press. Defaults to the newest message, or name an older one."""
    return {"update_id": n, "callback_query": {
        "id": "c%d" % n, "from": {"id": OWNER}, "data": data,
        "message": {"message_id": message_id if message_id is not None
                    else mid[0], "chat": {"id": CHAT}}}}


def arrive(update, on=bot):
    """Deliver a message and return the id of the confirmation it produced."""
    on.handle(update)
    return mid[0]


def snap(folder):
    return set(folder.glob("*")) if folder.is_dir() else set()


def added(folder, before):
    return sorted(set(snap(folder)) - before)


def keys_of(keyboard):
    return [b["callback_data"] for row in keyboard or [] for b in row]


def labels_of(keyboard):
    return [b["text"] for row in keyboard or [] for b in row]


section("a message is made safe, and cut to what Telegram accepts")
check("an ampersand cannot break the parse",
      chute.tg_escape("Marks & Spencer"), "Marks &amp; Spencer")
check("nor can a tag someone spoke",
      chute.tg_escape("a <b> c"), "a &lt;b&gt; c")
check("ordinary text is left alone", chute.tg_escape("plain"), "plain")
check("a short message is untouched", chute.tg_fit("short"), "short")
check("nothing at all is fine", chute.tg_fit(None), "")
long_one = chute.tg_fit("x" * 9000)
check("a long one is cut to the limit", len(long_one), chute.TG_LIMIT)
check("and says that it was", long_one.endswith("…"), True)
# An entity cut in half fails to parse for a different reason than the one
# just fixed, so the cut backs up past it.
check("Telegram counts UTF-16, so an emoji is two", chute.tg_len("\U0001F600"), 2)
check("and a Hebrew letter is one", chute.tg_len("\u05e9"), 1)
for label, sample in [("emoji", "\U0001F600" * 3000),
                    ("Hebrew", "\u05e9\u05dc\u05d5\u05dd " * 2000),
                    ("both at once", "a\U0001F600" * 3000)]:
    check("a message of %s is cut by what Telegram counts" % label,
          chute.tg_len(chute.tg_fit(sample)) <= chute.TG_LIMIT, True)
check("and is not collapsed to nothing in the process",
      len(chute.tg_fit("\U0001F600" * 3000)) > 2000, True)
check("a cut never lands inside an entity",
      chute.tg_fit("y" * 4093 + "&amp;" + "z" * 50).rstrip("…").endswith("y"),
      True)
check("nor inside a tag",
      "<cod" in chute.tg_fit("y" * 4090 + "<code>xx</code>" + "z" * 50), False)


section("a filename with an & in it does not take the reply down")
# clean_name strips < > and " but has no reason to touch &, so a file called
# Q&A.mp3 made every reply about it a Telegram parse error.
check("the ampersand is escaped on the way out",
      "Q&amp;A" in chute.tg_escape("Inbox/Q&A.mp3"), True)
check("and the path is otherwise untouched",
      chute.tg_escape("Inbox/Q&A.mp3"), "Inbox/Q&amp;A.mp3")


section("the keyboard is every folder, plus delete")
first = arrive(photo(1, caption="q3 pricing table"))
keys = keys_of(sent[-1][1])
check("one button per folder", keys[:3], ["b:inbox", "b:work", "b:personal"])
check("delete is the last button", keys[-1], "b:__delete")
check("the folder it is in is marked",
      labels_of(sent[-1][1])[0], "• 📥 Inbox")
check("the others are not", labels_of(sent[-1][1])[1], "📡 Work")

section("arrival files it with no tap at all")
check("it is already in the landing folder",
      (INBOX / "q3 pricing table.jpg").exists(), True)
check("the reply names where it went",
      "Inbox/q3 pricing table.jpg" in sent[-1][0], True)
check("and says it is filed", sent[-1][0].startswith("Filed"), True)
check("nothing was left staged", list(chute.STAGING.iterdir()), [])
check("nothing was ever asked", any("Where does this go" in s[0] for s in sent),
      False)

section("a captionless item names itself by date and type")
before = snap(INBOX)
arrive(photo(2))
got = added(INBOX, before)
check("one file appeared", len(got), 1)
check("named by date, time and type", bool(DATED.match(got[0].name)), True)

section("a tap moves it")
bot.handle(tap(3, "b:work", message_id=first))
check("gone from the landing folder",
      (INBOX / "q3 pricing table.jpg").exists(), False)
check("arrived in the tapped folder",
      (WORK / "q3 pricing table.jpg").exists(), True)
check("the message says it moved", edits[-1].startswith("Moved"), True)
check("and names the new path", "Work/Attachments/q3 pricing table.jpg"
      in edits[-1], True)
check("the buttons stay live", keys_of(edit_kb[-1])[:3],
      ["b:inbox", "b:work", "b:personal"])
check("now marking the folder it sits in", labels_of(edit_kb[-1])[1],
      "• 📡 Work")

section("a second tap moves it again")
bot.handle(tap(4, "b:personal", message_id=first))
check("moved on", (PERSONAL / "q3 pricing table.jpg").exists(), True)
check("and left the last folder", (WORK / "q3 pricing table.jpg").exists(),
      False)
bot.handle(tap(5, "b:inbox", message_id=first))
check("including back where it started",
      (INBOX / "q3 pricing table.jpg").exists(), True)

section("tapping the folder it is already in changes nothing")
count_before = len(list(INBOX.glob("q3 pricing table*.jpg")))
bot.handle(tap(6, "b:inbox", message_id=first))
check("no second copy appears",
      len(list(INBOX.glob("q3 pricing table*.jpg"))), count_before)
check("and it is still there", (INBOX / "q3 pricing table.jpg").exists(), True)

section("an album is one file and one message each")
before = snap(INBOX)
ids = [arrive(photo(n, group="G1")) for n in (10, 11, 12)]
check("three files landed with no taps", len(added(INBOX, before)), 3)
check("each got its own message", len(set(ids)), 3)
check("nothing mentions waiting",
      any("more waiting" in s[0] for s in sent), False)
for n, one in zip((13, 14, 15), ids):
    bot.handle(tap(n, "b:work", message_id=one))
check("each moves independently", len(added(INBOX, before)), 0)

section("a link becomes a note, and moves by its target's routing")
before = snap(INBOX)
note_id = arrive(text(20, "https://example.com/blog/some-article"))
notes = added(INBOX, before)
check("the note landed in the landing folder", len(notes), 1)
check("as markdown", notes[0].suffix, ".md")
check("with the link in it",
      "example.com/blog/some-article" in notes[0].read_text(), True)
bot.handle(tap(21, "b:work", message_id=note_id))
check("moving it follows the target's by_kind route",
      len(list((root / "Work/Inbox").glob("*.md"))), 1)

section("blocked file types never reach the disk")
before = snap(INBOX)
bot.handle(document(30, "installer.app"))
check("refused", "blocked for safety" in sent[-1][0], True)
check("nothing landed", added(INBOX, before), [])
check("nothing staged", list(chute.STAGING.iterdir()), [])

section("delete removes the file")
doomed = arrive(photo(40, caption="delete me"))
check("filed first", (INBOX / "delete me.jpg").exists(), True)
bot.handle(tap(41, "b:__delete", message_id=doomed))
check("the file is gone", (INBOX / "delete me.jpg").exists(), False)
check("the message says so", "Deleted" in edits[-1], True)
check("and loses its buttons", edit_kb[-1], None)
bot.handle(tap(42, "b:work", message_id=doomed))
check("a later tap has nothing to act on",
      "no longer have a record" in edits[-1], True)

section("delete keeps its hands off a changed file")
edited = arrive(photo(50, caption="edited later"))
(INBOX / "edited later.jpg").write_bytes(b"SOMEONE ELSE CHANGED THIS")
bot.handle(tap(51, "b:__delete", message_id=edited))
check("the file survives", (INBOX / "edited later.jpg").exists(), True)
check("and the reason is given", "changed since I filed it" in edits[-1], True)

section("a move does not chase a file that walked off")
strayed = arrive(photo(60, caption="moved by hand"))
(INBOX / "moved by hand.jpg").rename(root / "elsewhere.jpg")
bot.handle(tap(61, "b:work", message_id=strayed))
check("it is left alone", (root / "elsewhere.jpg").exists(), True)
check("and that is said plainly", "not where I left it" in edits[-1], True)

section("an edited file can still be moved")
cropped = arrive(photo(65, caption="cropped in place"))
(INBOX / "cropped in place.jpg").write_bytes(b"EDITED BUT STILL MINE")
bot.handle(tap(66, "b:work", message_id=cropped))
check("the move goes ahead", (WORK / "cropped in place.jpg").exists(), True)
check("carrying the new contents",
      (WORK / "cropped in place.jpg").read_bytes(), b"EDITED BUT STILL MINE")

section("stale and unknown buttons")
live = arrive(photo(70, caption="still here"))
bot.handle(tap(71, "b:__custom", message_id=live))
check("an unknown button does nothing",
      (INBOX / "still here.jpg").exists(), True)
bot.handle(tap(72, "b:work", message_id=live))
check("a real button still works", (WORK / "still here.jpg").exists(), True)
bot.handle(tap(73, "b:work", message_id=999999))
check("a tap on a message we have forgotten says so",
      "no longer have a record" in edits[-1], True)

section("a message too old to edit gets a fresh one")
old = arrive(photo(80, caption="ancient"))
edit_fails[0] = True
sent.clear()
bot.handle(tap(81, "b:work", message_id=old))
check("the move still happened", (WORK / "ancient.jpg").exists(), True)
check("and a new message reports it", len(sent), 1)
check("naming the new path", "Work/Attachments/ancient.jpg" in sent[-1][0],
      True)
new_id = mid[0]
edit_fails[0] = False
bot.handle(tap(82, "b:personal", message_id=new_id))
check("the new message is the live handle now",
      (PERSONAL / "ancient.jpg").exists(), True)
bot.handle(tap(83, "b:inbox", message_id=old))
check("and the old one is dead", "no longer have a record" in edits[-1], True)

section("only the owner may move or delete")
mine = arrive(photo(90, caption="mine"))
sent.clear()
bot.handle(text(91, "steal", uid=999))
check("a stranger gets no reply", sent, [])
stranger_tap = {"update_id": 92, "callback_query": {
    "id": "c92", "from": {"id": 999}, "data": "b:work",
    "message": {"message_id": mine, "chat": {"id": CHAT}}}}
bot.handle(stranger_tap)
check("and cannot move the owner's file", (INBOX / "mine.jpg").exists(), True)

section("help names the folders and the landing one")
bot.handle(text(100, "/help"))
check("it lists a folder", "📡 Work" in sent[-1][0], True)
check("it marks where things land", "← lands here" in sent[-1][0], True)
check("it admits it sleeps", "while my computer is awake" in sent[-1][0], True)

section("retired commands point at the buttons")
for n, cmd in ((101, "/undo"), (102, "/cancel"), (103, "/status")):
    bot.handle(text(n, cmd))
    check("%s explains itself" % cmd,
          "filed as it arrives" in sent[-1][0], True)

section("a restart changes nothing")
resumed_id = arrive(photo(110, caption="restart me"))
bot.persist()
resumed = chute.Bot(make_config(root))
sent.clear()
check("the new process says nothing on startup", sent, [])
resumed.handle(tap(111, "b:work", message_id=resumed_id))
check("and the old message still moves its file",
      (WORK / "restart me.jpg").exists(), True)

section("history records filing, moving and deleting")
resumed.handle(text(120, "/history"))
hist = sent[-1][0]
check("newest first header", hist.startswith("<b>Filed</b> (newest first)"),
      True)
check("a move is marked with an arrow", "→ <code>Work/Attachments" in hist,
      True)
check("timestamps rendered", bool(re.search(r"\d{2} \w{3} \d{2}:\d{2}", hist)),
      True)
fresh = chute.Bot(make_config(root))
fresh.state["chats"] = {}
sent.clear()
fresh.handle(text(121, "/history"))
check("an empty history says so", sent[-1][0], "Nothing filed yet.")

section("the remembered messages are bounded")
cs = resumed.chat_state(CHAT)
cs["filed"] = {str(i): {"path": "p%d" % i, "at": 1} for i in range(5)}
now = int(chute.time.time())
for i in range(chute.FILED_KEEP + 5):
    chute.remember(cs["filed"], 10000 + i,
                   {"path": "q%d" % i, "at": now + i}, now=now)
check("capped at the keep limit", len(cs["filed"]), chute.FILED_KEEP)
check("the stale ones went first", "0" in cs["filed"], False)

shutil.rmtree(root.parent, ignore_errors=True)
sys.exit(report())
