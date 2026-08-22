#!/usr/bin/env python3
"""Transcription: link detection, caption cleaning, and the button flow."""
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from harness import check, section, make_config, report  # noqa: F401
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


section("the language is named, not just coded")
check("known code", chute.language_label("he"), "Hebrew (he)")
check("regional code", chute.language_label("en-US"), "English (en)")
check("unknown code passes through", chute.language_label("xx"), "xx")
check("auto is not a language", chute.language_label("auto"), "")
check("duration reads as a clock", chute.hhmmss(3725), "1:02:05")


section("the transcript note carries what it came from")
body = chute.transcript_body(
    [(0.0, "First line."), (4.0, "Second line.")],
    {"title": "Root of Trust talk", "language": "Hebrew (he)",
     "duration": "0:42:10", "transcribed-with": "whisper.cpp large-v3-turbo",
     "url": WATCH, "channel": "Web3 Devs"})
check("frontmatter opens the file", body.startswith("---\n"), True)
check("type recorded", "type: transcript" in body, True)
check("language recorded", 'language: "Hebrew (he)"' in body, True)
check("duration recorded", 'duration: "0:42:10"' in body, True)
check("engine recorded", "whisper.cpp large-v3-turbo" in body, True)
check("source linked", "Source: %s" % WATCH in body, True)
check("tagged for retrieval", "  - transcript" in body, True)
check("titled", "# Root of Trust talk" in body, True)
check("the words are there", "First line. Second line." in body, True)
check("no timestamps by default", "[0:00:00]" in body, False)

stamped = chute.transcript_body(
    [(0.0, "First line."), (65.0, "Second line.")],
    {"title": "t", "language": "English (en)"}, timestamps=True)
check("timestamps when asked for", "[0:00:00] First line." in stamped, True)
check("and they count up", "[0:01:05] Second line." in stamped, True)


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

    def transcribe_youtube(self, url, workdir, progress=None):
        self.calls.append(("youtube", url))
        if self.fail:
            raise chute.TranscribeError(self.fail)
        return ([(None, "Words from the video.")], "en", 600.0,
                {"title": "Root of Trust", "uploader": "Web3 Devs",
                 "upload_date": "20260801"}, "the video's own captions")


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


section("tapping it writes the words into the folder the file is in")
bot.handle(tap(6, "b:__transcribe", voice_msg))
check("the chat is told at once, before any work",
      "Transcribing" in edits[-1], True)
settle()
written = sorted(INBOX.glob("*transcript*.md"))
check("one transcript appeared", len(written), 1)
note = written[0].read_text()
check("named after the recording",
      written[0].name.endswith(" transcript.md"), True)
check("the words are in it", "Words from the recording." in note, True)
check("the language it worked out is recorded",
      'language: "Hebrew (he)"' in note, True)
check("it links back to the recording", "Audio.ogg" in note, True)
check("the reply names the language", "Transcript in Hebrew (he)" in edits[-1],
      True)
check("and names the file", written[0].name in edits[-1], True)
check("the button is gone once it is done",
      "b:__transcribe" in keys_of(edit_kb[-1]), False)
check("the engine was handed the filed recording",
      FakeEngine.calls[-1][0], "audio")
check("nothing was left in staging", list(chute.STAGING.glob("transcript-*")),
      [])

section("the transcript travels with its recording")
bot.handle(tap(7, "b:work", voice_msg))
check("the recording moved", len(list(WORK.glob("*.ogg"))), 1)
check("and so did the transcript", len(list(WORK.glob("*transcript*.md"))), 1)
check("leaving none behind", len(list(INBOX.glob("*transcript*.md"))), 0)
check("both are named on the message", edits[-1].count("Media/Work"), 2)

section("a second tap cannot run it twice")
before = len(FakeEngine.calls)
bot.handle(tap(8, "b:__transcribe", voice_msg))
settle()
check("the engine was not called again", len(FakeEngine.calls), before)
check("and no second transcript was written",
      len(list(WORK.glob("*transcript*.md"))), 1)

section("a YouTube transcript is named after the video")
bot.handle(tap(9, "b:__transcribe", link_msg))
settle()
check("the engine was given the canonical url",
      FakeEngine.calls[-1], ("youtube", WATCH))
video = INBOX / "Root of Trust.md"
check("named after the video, not the note", video.is_file(), True)
vbody = video.read_text()
check("the words are in it", "Words from the video." in vbody, True)
check("the channel is recorded", 'channel: "Web3 Devs"' in vbody, True)
check("the publish date is recorded", 'published: "2026-08-01"' in vbody, True)
check("the source url is recorded", WATCH in vbody, True)
check("captions were credited, not whisper",
      "the video's own captions" in vbody, True)
bot.handle(tap(10, "b:personal", link_msg))
moved_to = sorted(root.rglob("Root of Trust.md"))
check("moving the note brings the transcript, under its own name",
      [str(x.parent.relative_to(root)) for x in moved_to],
      ["Personal/Attachments"])
check("and none is left behind", video.exists(), False)

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
check("and a retry works", len(list(INBOX.glob("*transcript*.md"))), 1)

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
