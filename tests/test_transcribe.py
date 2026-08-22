#!/usr/bin/env python3
"""Transcription: link detection, caption cleaning, and the button flow."""
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from harness import check, raises, section, make_config, report  # noqa: F401
import chute

root = Path(tempfile.mkdtemp()).resolve() / "Root"
(root / "Inbox").mkdir(parents=True)
chute.STAGING = root.parent / "staging"
chute.STATE_PATH = root.parent / "state.json"
chute.LOG_PATH = root.parent / "chute.log"
OWNER = CHAT = 555


section("YouTube links are recognised wherever they come from")
WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
for label, link in [
        ("plain watch url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("no www", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
        ("short form", "https://youtu.be/dQw4w9WgXcQ?si=abc123"),
        ("shorts", "https://www.youtube.com/shorts/dQw4w9WgXcQ"),
        ("live", "https://www.youtube.com/live/dQw4w9WgXcQ"),
        ("mobile", "https://m.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("params before v", "https://www.youtube.com/watch?app=desktop&v=dQw4w9WgXcQ"),
        ("inside a sentence", "watch this https://youtu.be/dQw4w9WgXcQ tonight")]:
    check(label, chute.youtube_url(link), WATCH)
check("a plain link is not one", chute.youtube_url("https://magmadevs.com"), None)
check("nor is an id-shaped word",
      chute.youtube_url("youtube.com/watch?v=short"), None)
check("empty text is fine", chute.youtube_url(""), None)


section("helpers are found without a login shell's PATH")
# A service gets /usr/bin:/bin:/usr/sbin:/sbin and nothing else. Everything
# below has to keep working under that, or the button never appears when Chute
# runs the way it is installed to run.
fake_bin = Path(tempfile.mkdtemp())
(fake_bin / "whisper-cli").write_text("#!/bin/sh\n")
(fake_bin / "whisper-cli").chmod(0o755)
(fake_bin / "not-executable").write_text("#!/bin/sh\n")
real_dirs, real_path = chute.BIN_DIRS, os.environ.get("PATH", "")
chute.BIN_DIRS = [str(fake_bin)]
os.environ["PATH"] = "/usr/bin:/bin"
try:
    check("found where package managers put it",
          chute.which_or_path("whisper-cli"), fake_bin / "whisper-cli")
    check("a file that cannot be run is not it",
          chute.which_or_path("not-executable"), None)
    check("nor is one that is not there",
          chute.which_or_path("no-such-binary"), None)
    engine = chute.Transcriber({"ffmpeg_bin": str(fake_bin / "whisper-cli")})
    check("an explicit path is taken as given",
          engine.ffmpeg, fake_bin / "whisper-cli")
    check("and the folders found lead the child's PATH",
          engine.env()["PATH"].split(os.pathsep)[0], str(fake_bin))
    check("without losing the rest of it",
          engine.env()["PATH"].endswith("/usr/bin:/bin"), True)
finally:
    chute.BIN_DIRS, os.environ["PATH"] = real_dirs, real_path
check("a missing binary is simply not offered",
      chute.Transcriber({"whisper_bin": "/nowhere/whisper"}).audio_ready(), False)


section("a yt-dlp failure is explained, not printed at you")
for label, raw, want in [
        ("a blocked download blames the version",
         "ERROR: unable to download video data: HTTP Error 403: Forbidden",
         "brew upgrade yt-dlp"),
        ("a bot check says what it is",
         "ERROR: Sign in to confirm you're not a bot", "not a bot"),
        ("a dead video is about the video",
         "ERROR: [youtube] abc: Video unavailable", "not available"),
        ("so is a private one",
         "ERROR: Private video. Sign in if you've been granted access",
         "not public")]:
    check(label, want in (chute.explain_ytdlp_error(raw) or ""), True)
check("anything unrecognised is passed through untouched",
      chute.explain_ytdlp_error("ERROR: brand new failure mode"), None)
check("and an empty one does not crash",
      chute.explain_ytdlp_error(""), None)


section("captions are cleaned of markup and of their rolling repeats")
VTT = """WEBVTT
Kind: captions
Language: he

00:00:01.000 --> 00:00:03.000 align:start position:0%
<c>שלום</c> לכולם

00:00:03.000 --> 00:00:05.000
שלום לכולם
ברוכים הבאים

00:00:05.000 --> 00:00:07.000
&amp; now in English
"""
lines = chute.vtt_to_lines(VTT)
check("headers dropped", "WEBVTT" in " ".join(lines), False)
check("timings dropped", "-->" in " ".join(lines), False)
check("tags stripped", lines[0], "שלום לכולם")
check("the repeated line is dropped", lines[1], "ברוכים הבאים")
check("entities decoded", lines[2], "& now in English")
check("nothing else survives", len(lines), 3)
check("an empty file yields nothing", chute.vtt_to_lines(""), [])


section("speech is broken into paragraphs at sentence ends")
sentences = ["This is a sentence about routing. " * 12,
             "And a short one.", "Then more about the router. " * 12]
paras = chute.paragraphs(sentences, width=200)
check("more than one paragraph", len(paras) > 1, True)
check("every one ends a sentence",
      all(p.rstrip().endswith(".") for p in paras), True)
check("nothing is lost",
      sum(len(p.split()) for p in paras),
      sum(len(x.split()) for x in sentences))
check("one short line stays one paragraph",
      chute.paragraphs(["Just this."]), ["Just this."])


section("a local Bot API server hands over paths, not URLs")
cloud = chute.Telegram("t")
check("no local path from Telegram's own server",
      cloud.local_path("photos/file_1.jpg"), None)
mapped = chute.Telegram("t", "http://localhost:8081",
                        "/var/lib/telegram-bot-api", "/Users/x/.tba")
check("the container prefix is swapped for the host one",
      str(mapped.local_path("/var/lib/telegram-bot-api/123/videos/f.mp4")),
      "/Users/x/.tba/123/videos/f.mp4")
check("a relative path is still a URL to fetch",
      mapped.local_path("videos/f.mp4"), None)
check("a path outside the mapping is taken as it is",
      str(mapped.local_path("/somewhere/else/f.mp4")), "/somewhere/else/f.mp4")
unmapped = chute.Telegram("t", "http://localhost:8081")
check("with no mapping the server's own path is used",
      str(unmapped.local_path("/var/lib/telegram-bot-api/f.mp4")),
      "/var/lib/telegram-bot-api/f.mp4")
check("the api root loses a trailing slash",
      chute.Telegram("t", "http://localhost:8081/").api_root,
      "http://localhost:8081")


section("a file from a local server is moved, not fetched")
served = Path(tempfile.mkdtemp())
(served / "123" / "videos").mkdir(parents=True)
big = served / "123" / "videos" / "big.mp4"
big.write_bytes(b"a big video" * 100)


class ServedTelegram(chute.Telegram):
    def call(self, method, **params):
        return {"file_path": "/var/lib/telegram-bot-api/123/videos/big.mp4",
                "file_size": big.stat().st_size}


local = ServedTelegram("t", "http://localhost:8081",
                       "/var/lib/telegram-bot-api", str(served))
landed = Path(tempfile.mkdtemp()) / "out.mp4"
local.download("fid", landed, 2000 * 1024 * 1024)
check("the file arrived", landed.read_bytes(), b"a big video" * 100)
check("and the server's copy was not left behind", big.exists(), False)
big.write_bytes(b"a big video" * 100)
raises("one over the limit never gets read",
       lambda: local.download("fid", landed, 10), ValueError)
check("so it is still sitting on the server", big.is_file(), True)
raises("a file the server promised but did not write is refused",
       lambda: ServedTelegram("t", "http://localhost:8081",
                              "/var/lib/telegram-bot-api",
                              "/nowhere").download("fid", landed, 1 << 30),
       ValueError)


section("a renamed note does not keep a heading naming the old file")
check("the heading follows the file",
      chute.retitle("# 2026-08-22 2118 Note\n\nbody\n",
                    "2026-08-22 2118 Note", "Root of Trust"),
      "# Root of Trust\n\nbody\n")
check("a heading someone wrote themselves is left alone",
      chute.retitle("# My own title\n", "2026-08-22 2118 Note", "Root of Trust"),
      "# My own title\n")
check("a deeper heading is not a title",
      chute.retitle("## 2026-08-22 2118 Note\n", "2026-08-22 2118 Note", "X"),
      "## 2026-08-22 2118 Note\n")
check("nothing to rename to changes nothing",
      chute.retitle("# a\n", "a", None), "# a\n")


section("a transcript note says what it is of, and when it was made")
STAMP = chute.datetime(2026, 8, 23, 0, 12)
check("video title, the word, then the time",
      chute.transcript_stem("Root of Trust", now=STAMP),
      "Root of Trust transcript 2026-08-23 0012")
check("a recording uses its own name",
      chute.transcript_stem("2026-08-22 2045 Audio", now=STAMP),
      "2026-08-22 2045 Audio transcript 2026-08-23 0012")


section("the language is named, not just coded")
check("known code", chute.language_label("he"), "Hebrew (he)")
check("regional code", chute.language_label("en-US"), "English (en)")
check("unknown code passes through", chute.language_label("xx"), "xx")
check("auto is not a language", chute.language_label("auto"), "")
check("duration reads as a clock", chute.hhmmss(3725), "1:02:05")


section("the transcript is a block, meant to be appended to a note")
block = chute.transcript_section(
    [(0.0, "First line."), (4.0, "Second line.")],
    {"title": "Root of Trust talk", "language": "Hebrew (he)",
     "duration": "0:42:10", "transcribed-with": "whisper.cpp large-v3-turbo",
     "channel": "Web3 Devs", "published": "2026-08-01"})
check("it opens with a heading, not frontmatter",
      block.strip().startswith("## Transcript"), True)
check("source named", "- Source: Root of Trust talk" in block, True)
check("channel named", "- Channel: Web3 Devs" in block, True)
check("publish date named", "- Published: 2026-08-01" in block, True)
check("language named", "- Language: Hebrew (he)" in block, True)
check("length named", "- Length: 0:42:10" in block, True)
check("engine named",
      "- Transcribed with: whisper.cpp large-v3-turbo" in block, True)
check("the words are there", "First line. Second line." in block, True)
check("no timestamps by default", "[0:00:00]" in block, False)
check("a fact nobody supplied is left out", "Published: None" in block, False)

stamped = chute.transcript_section(
    [(0.0, "First line."), (65.0, "Second line.")],
    {"language": "English (en)"}, timestamps=True)
check("timestamps when asked for", "[0:00:00] First line." in stamped, True)
check("and they count up", "[0:01:05] Second line." in stamped, True)


section("frontmatter is added to, never rewritten")
NOTE = """---
created: 2026-08-22
source: telegram
tags:
  - inbox
---

# A note

Body text I typed myself.
"""
grown = chute.add_frontmatter(NOTE, [("transcript-language", "Hebrew (he)"),
                                     ("transcript-length", "0:03:12")],
                              tag="transcript")
check("the original keys survive", "created: 2026-08-22" in grown, True)
check("the body is untouched", "Body text I typed myself." in grown, True)
check("the new key is inside the frontmatter",
      grown.index("transcript-language") < grown.index("\n---\n\n# A note"),
      True)
check("the tag joined the list", "  - inbox\n  - transcript" in grown, True)
check("an empty value is skipped",
      "transcript-length" in chute.add_frontmatter(
          NOTE, [("transcript-length", "")]), False)
check("a note with no frontmatter is left exactly as it is",
      chute.add_frontmatter("just words\n", [("a", "b")]), "just words\n")
check("so is an unterminated one",
      chute.add_frontmatter("---\nbroken\n", [("a", "b")]), "---\nbroken\n")


section("caption tracks: what is written beats what is guessed")
engine = chute.Transcriber({"youtube_captions": "manual"})
manual = {"subtitles": {"live_chat": [{}], "he": [{}]},
          "automatic_captions": {"en": [{}], "fr": [{}]}, "language": "he"}
check("a manual track wins", engine.caption_track(manual), ("he", False))
check("live chat is not a subtitle track",
      engine.caption_track({"subtitles": {"live_chat": [{}]}}), (None, False))
auto_only = {"automatic_captions": {"en": [{}], "fr": [{}], "he-orig": [{}]},
             "language": "he"}
check("automatic ones are refused by default",
      engine.caption_track(auto_only), (None, False))
loose = chute.Transcriber({"youtube_captions": "any"})
check("unless asked for, and then the original language wins",
      loose.caption_track(auto_only), ("he-orig", True))
check("falling back to the video's own language",
      loose.caption_track({"automatic_captions": {"en": [{}], "de": [{}]},
                           "language": "de"}), ("de", True))
off = chute.Transcriber({"youtube_captions": "off"})
check("off means nothing is offered", off.captions, "off")


# ------------------------------------------------------------------ the flow

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
        return {"message_id": m}

    def ack(self, cid, text=None):
        pass

    def download(self, file_id, dest, max_bytes):
        Path(dest).write_bytes(b"FAKE")
        return "voice/f.ogg"


class FakeEngine:
    """Stands in for whisper and yt-dlp. Records what it was asked to do."""
    timestamps = False
    captions = "manual"
    calls = []
    fail = None

    def audio_ready(self):
        return True

    def youtube_ready(self):
        return True

    def engine_label(self):
        return "whisper.cpp test"

    def model_name(self):
        return "test"

    def transcribe_audio(self, source, workdir, progress=None):
        self.calls.append(("audio", str(source)))
        if self.fail:
            raise chute.TranscribeError(self.fail)
        progress(50)
        return [(0.0, "Words from the recording.")], "he", 90.0

    keep = "video"

    def transcribe_youtube(self, url, workdir, progress=None):
        self.calls.append(("youtube", url))
        if self.fail:
            raise chute.TranscribeError(self.fail)
        media = None
        if self.keep != "none":
            media = Path(workdir) / "yt.mp4"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"FAKE VIDEO")
        return ([(None, "Words from the video.")], "en", 600.0,
                {"title": "Root of Trust", "uploader": "Web3 Devs",
                 "upload_date": "20260801"}, "the video's own captions", media)


chute.Telegram = FakeTelegram
cfg = make_config(root)
cfg.transcribe = FakeEngine()
bot = chute.Bot(cfg)
INBOX = root / "Inbox"
# Media has a by_kind route of its own in the shared test config.
WORK = root / "Media/Work"


def voice(n):
    return {"update_id": n, "message": {
        "message_id": n, "chat": {"id": CHAT}, "from": {"id": OWNER},
        "voice": {"file_id": "v%d" % n, "file_size": 10}}}


def photo(n):
    return {"update_id": n, "message": {
        "message_id": n, "chat": {"id": CHAT}, "from": {"id": OWNER},
        "photo": [{"file_id": "p%d" % n, "file_size": 10}]}}


def text(n, body):
    return {"update_id": n, "message": {"message_id": n, "chat": {"id": CHAT},
                                        "from": {"id": OWNER}, "text": body}}


def tap(n, data, message_id):
    return {"update_id": n, "callback_query": {
        "id": "c%d" % n, "from": {"id": OWNER}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": CHAT}}}}


def settle():
    """Wait for the transcription thread, which is where the work happens."""
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.daemon:
            thread.join(20)


def filed_path(message_id):
    """Where the bot says it put the thing that message is about."""
    return Path(bot.chat_state(CHAT)["filed"][str(message_id)]["path"])


def keys_of(keyboard):
    return [b["callback_data"] for row in keyboard or [] for b in row]


section("the button is offered for recordings and links, and nothing else")
bot.handle(voice(1))
voice_msg = mid[0]
check("a voice note is offered a transcript",
      "b:__transcribe" in keys_of(sent[-1][1]), True)
check("and the message says so", "Transcribe it?" in sent[-1][0], True)
check("delete is still last", keys_of(sent[-1][1])[-1], "b:__delete")

bot.handle(photo(2))
check("a photo is not", "b:__transcribe" in keys_of(sent[-1][1]), False)

bot.handle(text(3, "notes to self, nothing to watch"))
check("plain text is not", "b:__transcribe" in keys_of(sent[-1][1]), False)

bot.handle(text(4, "worth a look https://youtu.be/dQw4w9WgXcQ"))
link_msg = mid[0]
check("a YouTube link is", "b:__transcribe" in keys_of(sent[-1][1]), True)

cfg.transcribe = chute.Transcriber({"enabled": False})
bot.handle(voice(5))
check("with transcription off, no button",
      "b:__transcribe" in keys_of(sent[-1][1]), False)
check("and no prompt either", "Transcribe it?" in sent[-1][0], False)
cfg.transcribe = FakeEngine()


section("a recording with no note gets one, and only one")
bot.handle(tap(6, "b:__transcribe", voice_msg))
check("the chat is told at once, before any work",
      "Transcribing" in edits[-1], True)
settle()
audio = filed_path(voice_msg)
made = list(INBOX.glob(audio.stem + " transcript *.md"))
check("a note appeared, named for the recording and the time", len(made), 1)
note_path = made[0]
check("exactly one markdown for that recording",
      len(list(INBOX.glob(audio.stem + "*.md"))), 1)
note = note_path.read_text()
check("the words are in it", "Words from the recording." in note, True)
check("the language it worked out is in the frontmatter",
      'transcript-language: "Hebrew (he)"' in note, True)
check("the length too", 'transcript-length: "0:01:30"' in note, True)
check("tagged for retrieval", "  - transcript" in note, True)
check("it links back to the recording", "(<%s>)" % audio.name in note, True)
check("under one Transcript heading", note.count("## Transcript"), 1)
check("the reply names the language", "Transcript in Hebrew (he)" in edits[-1],
      True)
check("and names the note", note_path.name in edits[-1], True)
check("the button is gone once it is done",
      "b:__transcribe" in keys_of(edit_kb[-1]), False)
check("the engine was handed the filed recording",
      FakeEngine.calls[-1][0], "audio")
check("nothing was left in staging", list(chute.STAGING.glob("transcript-*")),
      [])

section("the note travels with its recording")
bot.handle(tap(7, "b:work", voice_msg))
moved = filed_path(voice_msg)
check("the recording moved", (moved.parent, moved.parent.name), (WORK, "Work"))
check("its note came too",
      len(list(WORK.glob(moved.stem + " transcript *.md"))), 1)
check("still one markdown, not two",
      len(list(WORK.glob(moved.stem + "*.md"))), 1)
check("and none was left behind", note_path.exists(), False)
check("both are named on the message", edits[-1].count("Media/Work"), 2)

section("a second tap cannot run it twice")
before = len(FakeEngine.calls)
bot.handle(tap(8, "b:__transcribe", voice_msg))
settle()
check("the engine was not called again", len(FakeEngine.calls), before)
check("and no second markdown was written",
      len(list(WORK.glob(moved.stem + "*.md"))), 1)

section("a link is already a note, so the words go into that note")
receipt = filed_path(link_msg)
before_notes = set(INBOX.glob("*.md"))
bot.handle(tap(9, "b:__transcribe", link_msg))
settle()
check("the engine was given the canonical url",
      FakeEngine.calls[-1], ("youtube", WATCH))
check("the note it arrived as is gone, renamed not copied",
      receipt.exists(), False)
renamed = filed_path(link_msg)
check("the count of notes did not grow, it was the same file renamed",
      len(list(INBOX.glob("*.md"))), len(before_notes))
check("named for the video and the moment",
      bool(re.match(r"Root of Trust transcript \d{4}-\d{2}-\d{2} \d{4}$",
                    renamed.stem)), True)
check("the video was kept beside it",
      (INBOX / "Root of Trust.mp4").is_file(), True)
vbody = renamed.read_text()
check("the note points at the video kept next to it",
      'file: "Root of Trust.mp4"' in vbody, True)
check("and its heading names the video, not the old filename",
      "# Root of Trust" in vbody, True)
check("the link exactly as it arrived is still there",
      "https://youtu.be/dQw4w9WgXcQ" in vbody, True)
check("the words were appended", "Words from the video." in vbody, True)
check("the video title is named", "- Source: Root of Trust" in vbody, True)
check("the channel is named", "- Channel: Web3 Devs" in vbody, True)
check("the publish date is named", "- Published: 2026-08-01" in vbody, True)
check("captions were credited, not whisper",
      "the video's own captions" in vbody, True)
check("the language reached the frontmatter",
      'transcript-language: "English (en)"' in vbody, True)
bot.handle(tap(10, "b:personal", link_msg))
PERSONAL = root / "Personal/Attachments"
check("the note moved", len(list(PERSONAL.glob("Root of Trust transcript *.md"))), 1)
check("and the video came with it",
      (PERSONAL / "Root of Trust.mp4").is_file(), True)
check("leaving neither behind",
      sorted(p.name for p in INBOX.glob("Root of Trust*")), [])

section("a failure explains itself and offers the button again")
bot.handle(voice(20))
failing = mid[0]
FakeEngine.fail = "that video is 5:00:00 long, past the 240 minute limit"
bot.handle(tap(21, "b:__transcribe", failing))
settle()
check("the reason is shown", "past the 240 minute limit" in edits[-1], True)
check("the button comes back",
      "b:__transcribe" in keys_of(edit_kb[-1]), True)
FakeEngine.fail = None
bot.handle(tap(22, "b:__transcribe", failing))
settle()
retried = list(INBOX.glob(filed_path(failing).stem + " transcript *.md"))
check("and a retry works",
      len(retried) == 1 and "## Transcript" in retried[0].read_text(), True)

section("a recording deleted while it runs still yields its transcript")


class VanishingEngine(FakeEngine):
    def transcribe_audio(self, source, workdir, progress=None):
        bot.chat_state(CHAT)["filed"].pop(str(orphan), None)
        Path(source).unlink()
        return [(0.0, "Said before it went.")], "en", 10.0


bot.handle(voice(30))
orphan = mid[0]
cfg.transcribe = VanishingEngine()
before_notes = set(INBOX.glob("*.md"))
bot.handle(tap(31, "b:__transcribe", orphan))
settle()
new_notes = sorted(set(INBOX.glob("*.md")) - before_notes)
check("the transcript lands in the folder things arrive in",
      len(new_notes), 1)
check("with the words in it",
      "Said before it went." in new_notes[0].read_text(), True)
check("and a fresh message points at it",
      new_notes[0].name in sent[-1][0], True)
cfg.transcribe = FakeEngine()

shutil.rmtree(root.parent, ignore_errors=True)
sys.exit(report())
