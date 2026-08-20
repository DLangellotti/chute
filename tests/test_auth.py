#!/usr/bin/env python3
"""Adversarial: a stranger who has found the bot tries everything."""
import shutil, sys, tempfile
from pathlib import Path

from harness import check, section, make_config, report
import chute

root = Path(tempfile.mkdtemp()).resolve() / "Root"
(root / "Work" / "Attachments").mkdir(parents=True)
chute.STAGING = root.parent / "staging"; chute.STAGING.mkdir()
chute.STATE_PATH = root.parent / "state.json"
chute.LOG_PATH = root.parent / "log"

OWNER, ATTACKER = 555, 66666
sent, downloads = [], []

class FakeTelegram:
    def __init__(self, *a): pass
    def call(self, m, **p): return {"username": "bot"}
    def send(self, chat, text, keyboard=None):
        sent.append((chat, text)); return {"message_id": 1}
    def edit(self, *a, **k): return {}
    def ack(self, *a, **k): pass
    def download(self, file_id, dest, max_bytes):
        downloads.append(file_id)          # records ANY attempt to pull a file
        Path(dest).write_bytes(b"X"); return "photos/f.jpg"

chute.Telegram = FakeTelegram
bot = chute.Bot(make_config(root))

def msg(n, uid, **body):
    m = {"message_id": n, "chat": {"id": uid}, "from": {"id": uid}}
    m.update(body); return {"update_id": n, "message": m}

section("a stranger tries every entry point")
attacks = [
    ("plain text",   msg(1, ATTACKER, text="hello")),
    ("a photo",      msg(2, ATTACKER, photo=[{"file_id": "evil1", "file_size": 9}])),
    ("a document",   msg(3, ATTACKER, document={"file_id": "evil2", "file_name": "x.pdf",
                                                "mime_type": "application/pdf"})),
    ("a voice note", msg(4, ATTACKER, voice={"file_id": "evil3"})),
    ("/start",       msg(5, ATTACKER, text="/start")),
    ("/cancel",      msg(6, ATTACKER, text="/cancel")),
    ("/status",      msg(7, ATTACKER, text="/status")),
    ("a path escape", msg(8, ATTACKER, text="../../../../etc/cron.d")),
]
for label, update in attacks:
    bot.handle(update)
    check("%s -> ignored in silence" % label, sent, [])

check("bot never downloaded anything for them", downloads, [])
check("nothing written to the tree", [p.name for p in root.rglob("*") if p.is_file()], [])
check("nothing left staged", list(chute.STAGING.iterdir()), [])
check("no state created for their chat", str(ATTACKER) in bot.state["chats"], False)

section("stranger tries to hijack the owner's pending item")
bot.handle(msg(20, OWNER, photo=[{"file_id": "mine", "file_size": 9}]))
check("owner's item is pending", bot.chat_state(OWNER)["active"] is not None, True)
hijack = {"update_id": 21, "callback_query": {
    "id": "c", "from": {"id": ATTACKER}, "data": "b:work",
    "message": {"message_id": 1, "chat": {"id": OWNER}}}}
bot.handle(hijack)
check("stranger cannot press the owner's button",
      bot.chat_state(OWNER)["stage"], "bucket")
check("owner's item still pending, unfiled",
      bot.chat_state(OWNER)["active"] is not None, True)

section("owner still works")
bot.handle({"update_id": 22, "callback_query": {
    "id": "c2", "from": {"id": OWNER}, "data": "b:work",
    "message": {"message_id": 1, "chat": {"id": OWNER}}}})
bot.handle(msg(23, OWNER, text="my photo"))
check("owner's file lands", (root / "Work/Attachments/my photo.jpg").exists(), True)

section("replying to strangers is opt-in")
loud = chute.Bot(make_config(root, security={"reply_to_strangers": True}))
loud.handle(msg(30, ATTACKER, text="hello"))
check("opt in and they get told", sent[-1][1], "Not authorised.")
sent.clear()

section("config refuses to run wide open")
try:
    make_config(root, allowed_user_ids=[])
    check("empty allowlist rejected", False, True)
except chute.ConfigError as e:
    check("empty allowlist rejected", "anyone who finds the bot" in str(e), True)

shutil.rmtree(root.parent, ignore_errors=True)
sys.exit(report())
