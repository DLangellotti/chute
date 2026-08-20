#!/usr/bin/env python3
"""Tests for the setup path prompt: parsing, creating, and the nesting offer."""
import builtins
import os
import shutil
import sys
import tempfile
from pathlib import Path

from harness import check, raises, section, report  # noqa: F401
import chute

tmp = Path(tempfile.mkdtemp()).resolve()
(tmp / "Existing").mkdir()

section("path parsing")
check("absolute kept", chute.resolve_root("/var/tmp"), Path("/var/tmp"))
check("tilde expanded", chute.resolve_root("~/Notes"), Path.home() / "Notes")
check("relative resolved against cwd", chute.resolve_root("sub", cwd="/var/tmp"),
      Path("/var/tmp/sub"))
check("dot means cwd", chute.resolve_root(".", cwd="/var/tmp"), Path("/var/tmp"))
check("parent segment normalised", chute.resolve_root("a/../b", cwd="/var/tmp"),
      Path("/var/tmp/b"))
check("double quotes stripped", chute.resolve_root('"/var/tmp/My Folder"'),
      Path("/var/tmp/My Folder"))
check("single quotes stripped", chute.resolve_root("'/var/tmp/My Folder'"),
      Path("/var/tmp/My Folder"))
check("dragged path unescaped", chute.resolve_root("/var/tmp/My\\ Folder"),
      Path("/var/tmp/My Folder"))
check("surrounding space ignored", chute.resolve_root("  /var/tmp  "),
      Path("/var/tmp"))
raises("empty rejected", lambda: chute.resolve_root(""), ValueError)
raises("whitespace rejected", lambda: chute.resolve_root("   "), ValueError)
raises("empty quotes rejected", lambda: chute.resolve_root('""'), ValueError)


class Script:
    """Feed prompt_root a fixed list of answers instead of a keyboard."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def __enter__(self):
        self.real = builtins.input
        def fake(prompt=""):
            self.asked.append(prompt)
            if not self.answers:
                raise AssertionError("prompt_root asked more than expected: %r"
                                     % prompt)
            return self.answers.pop(0)
        builtins.input = fake
        return self

    def __exit__(self, *a):
        builtins.input = self.real


section("choosing the folder")
os.chdir(tmp / "Existing")
with Script(["1"]):
    check("option 1 gives the current directory", chute.prompt_root(),
          (tmp / "Existing").resolve())

with Script(["2", str(tmp / "Existing")]):
    check("option 2 takes a typed path", chute.prompt_root(),
          (tmp / "Existing").resolve())

with Script([str(tmp / "Existing")]):
    check("a pasted path skips the menu", chute.prompt_root(),
          (tmp / "Existing").resolve())

with Script([""]):
    check("blank defaults to option 1", chute.prompt_root(),
          (tmp / "Existing").resolve())

section("creating a folder that is not there yet")
fresh = tmp / "Fresh"
with Script([str(fresh), "y"]):
    check("offers to create, then uses it", chute.prompt_root(), fresh.resolve())
check("it really exists now", fresh.is_dir(), True)

declined = tmp / "Declined"
with Script([str(declined), "n", str(tmp / "Existing")]):
    check("declining creation asks again", chute.prompt_root(),
          (tmp / "Existing").resolve())
check("declined folder not created", declined.exists(), False)

section("no surprise subfolders")
with Script([str(tmp / "Existing")]):
    check("the chosen folder is used exactly as chosen", chute.prompt_root(),
          (tmp / "Existing").resolve())
check("nothing was created inside it",
      sorted(p.name for p in (tmp / "Existing").iterdir()), [])

section("rejecting bad choices")
afile = tmp / "afile.txt"
afile.write_text("x")
with Script([str(afile), str(tmp / "Existing")]):
    check("a file is refused, asks again", chute.prompt_root(),
          (tmp / "Existing").resolve())

with Script([""]) as s:
    chute.prompt_root()
    check("prompt mentions the choice", "Choice" in s.asked[0], True)

section("browsing for a folder")
br = tmp / "Tree"
(br / "Work" / "Attachments").mkdir(parents=True)
(br / "Personal").mkdir()

with Script(["s"]):
    check("s at the root selects the root", chute.browse_folder(br), "")
with Script(["2", "s"]):
    check("number opens, s selects it", chute.browse_folder(br), "Work")
with Script(["2", "1", "s"]):
    check("descends two levels", chute.browse_folder(br), "Work/Attachments")
with Script(["2", "1", "u", "s"]):
    check("u goes back up", chute.browse_folder(br), "Work")
with Script(["u", "s"]):
    check("u at the root is refused, not a crash", chute.browse_folder(br), "")
with Script(["9", "s"]):
    check("out of range number is refused", chute.browse_folder(br), "")

section("creating a folder selects it")
with Script(["c", "Receipts"]):
    check("c creates and selects in one step", chute.browse_folder(br), "Receipts")
check("it exists on disk", (br / "Receipts").is_dir(), True)
# Its own tree: earlier checks add folders to br and shift the numbering.
solo = tmp / "Solo"
(solo / "Work").mkdir(parents=True)
with Script(["1", "c", "Invoices"]):
    check("creates inside where you are", chute.browse_folder(solo),
          "Work/Invoices")
check("created in the right parent", (solo / "Work" / "Invoices").is_dir(), True)
with Script(["c", "A/B/C"]):
    check("slashes nest", chute.browse_folder(br), "A/B/C")
check("nested folders really made", (br / "A" / "B" / "C").is_dir(), True)
with Script(["c", "", "s"]):
    check("empty name creates nothing, back to menu", chute.browse_folder(br), "")

section("typing a path selects it, never wanders into it")
with Script(["t", "Work/Attachments"]):
    check("existing path selected outright",
          chute.browse_folder(br), "Work/Attachments")
with Script(["t", "Brand New", "y"]):
    check("missing path is created then selected",
          chute.browse_folder(br), "Brand New")
check("and it exists", (br / "Brand New").is_dir(), True)
with Script(["t", "Declined Path", "n", "s"]):
    check("declining leaves you at the menu", chute.browse_folder(br), "")
check("declined path not created", (br / "Declined Path").exists(), False)
with Script(["t", "../../etc", "s"]):
    check("traversal refused", chute.browse_folder(br), "")
with Script(["t", "Photos/{year}/{month}"]):
    check("template accepted as written",
          chute.browse_folder(br), "Photos/{year}/{month}")
check("template not created as a literal folder",
      (br / "Photos").exists(), False)
with Script(["t", "{nope}", "s"]):
    check("bad template token refused", chute.browse_folder(br), "")

section("starting point")
with Script(["s"]):
    check("opens where the destination already points",
          chute.browse_folder(br, start="Work"), "Work")
with Script(["s"]):
    check("a stale start falls back to the root",
          chute.browse_folder(br, start="Deleted/Folder"), "")
with Script(["s"]):
    check("a template start falls back to the root",
          chute.browse_folder(br, start="Photos/{year}"), "")

section("naming the buttons")
buttons = tmp / "Buttons"
buttons.mkdir()
with Script(["d", "Receipts", "Work Photos", ""]):
    check("one folder per button",
          chute.setup_destinations(buttons),
          [{"label": "Receipts", "path": "Receipts"},
           {"label": "Work Photos", "path": "Work Photos"}])
check("first folder created", (buttons / "Receipts").is_dir(), True)
check("spaces kept in the folder name",
      (buttons / "Work Photos").is_dir(), True)

(buttons / "Already There").mkdir()
(buttons / "Already There" / "keep.txt").write_text("x")
with Script(["d", "Already There", ""]):
    check("an existing folder is reused", chute.setup_destinations(buttons),
          [{"label": "Already There", "path": "Already There"}])
check("existing contents untouched",
      (buttons / "Already There" / "keep.txt").exists(), True)

with Script(["d", "Notes", "notes", "", ""]):
    check("a duplicate label is refused", chute.setup_destinations(buttons),
          [{"label": "Notes", "path": "Notes"}])

with Script(["d", "", "Inbox", ""]):
    check("blank first answer asks again", chute.setup_destinations(buttons),
          [{"label": "Inbox", "path": "Inbox"}])

with Script(["d", "../escape", "Safe", ""]):
    check("traversal refused", chute.setup_destinations(buttons),
          [{"label": "Safe", "path": "Safe"}])
check("nothing created outside the root",
      (tmp / "escape").exists(), False)

with Script(["d", ".hidden", ""]):
    check("a leading dot is dropped, not made into a dot-folder",
          chute.setup_destinations(buttons),
          [{"label": ".hidden", "path": "hidden"}])
check("no dot-folder created", (buttons / ".hidden").exists(), False)
check("plain folder created instead", (buttons / "hidden").is_dir(), True)

with Script(["d", "Talks/2026", "Talks", ""]):
    check("a slash is refused rather than flattened",
          chute.setup_destinations(buttons),
          [{"label": "Talks", "path": "Talks"}])
check("nothing named Talks2026", (buttons / "Talks2026").exists(), False)

with Script(["d", "...", "Plain", ""]):
    check("a name with nothing usable in it is refused",
          chute.setup_destinations(buttons),
          [{"label": "Plain", "path": "Plain"}])

with Script(["d", "Bills: 2026", ""]):
    check("an illegal character is dropped from the folder name",
          chute.setup_destinations(buttons),
          [{"label": "Bills: 2026", "path": "Bills 2026"}])
check("folder created under the cleaned name",
      (buttons / "Bills 2026").is_dir(), True)

section("undo and back")
undo_root = tmp / "Undo"
undo_root.mkdir()
with Script(["d", "Receipts", "undo", "Bills", ""]):
    check("undo drops the last button", chute.setup_destinations(undo_root),
          [{"label": "Bills", "path": "Bills"}])
check("undo removed the folder it had created",
      (undo_root / "Receipts").exists(), False)
check("the kept button still has its folder",
      (undo_root / "Bills").is_dir(), True)

with Script(["d", "u", "Only", ""]):
    check("undo with nothing to undo just asks again",
          chute.setup_destinations(undo_root),
          [{"label": "Only", "path": "Only"}])

with Script(["d", "Bills", "undo", "Kept", ""]):
    check("undo leaves a folder that was already there",
          chute.setup_destinations(undo_root),
          [{"label": "Kept", "path": "Kept"}])
check("pre-existing folder survives undo", (undo_root / "Bills").is_dir(), True)

full = undo_root / "Full"
full.mkdir()
(full / "file.txt").write_text("x")
with Script(["d", "Full", "undo", "Empty Enough", ""]):
    chute.setup_destinations(undo_root)
check("undo never deletes a folder with something in it",
      (full / "file.txt").exists(), True)

with Script(["d", "Half Done", "back"]):
    check("back returns None so setup can re-ask for the root",
          chute.setup_destinations(undo_root), None)
check("folders made before going back stay on disk",
      (undo_root / "Half Done").is_dir(), True)

with Script(["d", "done", "One", "done"]):
    check("done is refused while empty, then finishes once there is a button",
          chute.setup_destinations(undo_root),
          [{"label": "One", "path": "One"}])

section("picking from folders already there")
pick = tmp / "Pick"
for name in ("Alpha", "Beta", "Gamma"):
    (pick / name).mkdir(parents=True)

with Script(["d"]):
    check("nothing selected by default",
          chute.pick_existing_folders(pick, ["Alpha", "Beta", "Gamma"]), [])
with Script(["2", "d"]):
    check("one turned on", chute.pick_existing_folders(
        pick, ["Alpha", "Beta", "Gamma"]), [{"label": "Beta", "path": "Beta"}])
with Script(["1 3", "d"]):
    check("several at once", chute.pick_existing_folders(
        pick, ["Alpha", "Beta", "Gamma"]),
        [{"label": "Alpha", "path": "Alpha"},
         {"label": "Gamma", "path": "Gamma"}])
with Script(["1,3", "d"]):
    check("commas work too", [d["label"] for d in chute.pick_existing_folders(
        pick, ["Alpha", "Beta", "Gamma"])], ["Alpha", "Gamma"])
with Script(["2", "2", "d"]):
    check("picking twice turns it back off", chute.pick_existing_folders(
        pick, ["Alpha", "Beta", "Gamma"]), [])
with Script(["a", "d"]):
    check("a selects every one", [d["label"] for d in chute.pick_existing_folders(
        pick, ["Alpha", "Beta", "Gamma"])], ["Alpha", "Beta", "Gamma"])
with Script(["a", "n", "d"]):
    check("n clears them again", chute.pick_existing_folders(
        pick, ["Alpha", "Beta", "Gamma"]), [])
with Script(["9", "zz", "1", "d"]):
    check("junk is refused, valid picks still land",
          [d["label"] for d in chute.pick_existing_folders(
              pick, ["Alpha", "Beta", "Gamma"])], ["Alpha"])
with Script(["3 1", "d"]):
    check("result follows folder order, not typing order",
          [d["label"] for d in chute.pick_existing_folders(
              pick, ["Alpha", "Beta", "Gamma"])], ["Alpha", "Gamma"])
check("nothing was created or renamed",
      sorted(p.name for p in pick.iterdir()), ["Alpha", "Beta", "Gamma"])

section("an empty root offers starter buttons")
fresh = tmp / "FreshRoot"
fresh.mkdir()
with Script(["1", "d", ""]):
    check("picking Inbox creates it and makes the button",
          chute.setup_destinations(fresh),
          [{"label": "Inbox", "path": "Inbox"}])
check("Inbox folder exists now", (fresh / "Inbox").is_dir(), True)

fresh2 = tmp / "FreshRoot2"
fresh2.mkdir()
with Script(["1 2 5", "d", ""]):
    check("several starters at once",
          [d["label"] for d in chute.setup_destinations(fresh2)],
          ["Inbox", "Photos", "Notes"])
check("their folders exist", sorted(p.name for p in fresh2.iterdir()),
      ["Inbox", "Notes", "Photos"])

fresh3 = tmp / "FreshRoot3"
fresh3.mkdir()
with Script(["d", "My Stuff", ""]):
    check("declining starters falls back to typing names",
          chute.setup_destinations(fresh3),
          [{"label": "My Stuff", "path": "My Stuff"}])
check("no starter folders were created",
      sorted(p.name for p in fresh3.iterdir()), ["My Stuff"])

fresh4 = tmp / "FreshRoot4"
fresh4.mkdir()
with Script(["1", "d", "undo", "Kept", ""]):
    check("undo takes back a starter button",
          chute.setup_destinations(fresh4),
          [{"label": "Kept", "path": "Kept"}])
check("and removes the folder it created", (fresh4 / "Inbox").exists(), False)

fresh5 = tmp / "FreshRoot5"
fresh5.mkdir()
with Script(["a", "d", ""]):
    check("a turns on every starter",
          len(chute.setup_destinations(fresh5)), len(chute.STARTER_BUTTONS))

# A root with folders in it must NOT see the starter list.
with Script(["1", "d", ""]):
    got = chute.setup_destinations(fresh5)
    check("second run sees the real folders, not starters",
          got[0]["label"] in [n for n, _ in chute.STARTER_BUTTONS], True)

section("custom names typed into the starter checklist")
cust = tmp / "Custom1"
cust.mkdir()
with Script(["Tax 2026", "d", ""]):
    check("a typed name becomes a button",
          chute.setup_destinations(cust),
          [{"label": "Tax 2026", "path": "Tax 2026"}])
check("and its folder exists", (cust / "Tax 2026").is_dir(), True)

cust2 = tmp / "Custom2"
cust2.mkdir()
with Script(["1", "Tax 2026", "d", ""]):
    check("mixes with the starters, starters first",
          [d["label"] for d in chute.setup_destinations(cust2)],
          ["Inbox", "Tax 2026"])

cust3 = tmp / "Custom3"
cust3.mkdir()
with Script(["inbox", "d", ""]):
    check("typing a starter's name turns it on, no duplicate",
          chute.setup_destinations(cust3),
          [{"label": "Inbox", "path": "Inbox"}])

cust4 = tmp / "Custom4"
cust4.mkdir()
with Script(["Tax", "6", "d", "Other", ""]):
    check("an added name can be toggled off by its number",
          chute.setup_destinations(cust4),
          [{"label": "Other", "path": "Other"}])
check("no folder for the toggled-off name", (cust4 / "Tax").exists(), False)

cust5 = tmp / "Custom5"
cust5.mkdir()
with Script(["a/b", "d", "Real", ""]):
    check("a slash in a typed name is refused",
          chute.setup_destinations(cust5),
          [{"label": "Real", "path": "Real"}])

cust6 = tmp / "Custom6"
cust6.mkdir()
with Script(["Bills: 2026", "d", ""]):
    check("label keeps its punctuation, folder name is cleaned",
          chute.setup_destinations(cust6),
          [{"label": "Bills: 2026", "path": "Bills 2026"}])
check("cleaned folder exists", (cust6 / "Bills 2026").is_dir(), True)

section("command-letter confusion is caught")
g1 = tmp / "Guard1"
g1.mkdir()
with Script(["a personal", "n", "personal", "d", ""]):
    check("'a personal' asks, no on default, retype works",
          chute.setup_destinations(g1),
          [{"label": "personal", "path": "personal"}])
check("no 'a personal' folder", (g1 / "a personal").exists(), False)

g2 = tmp / "Guard2"
g2.mkdir()
with Script(["a team", "y", "d", ""]):
    check("a genuine 'a ...' name survives with a yes",
          chute.setup_destinations(g2),
          [{"label": "a team", "path": "a team"}])

section("removing a typed mistake")
r1 = tmp / "Remove1"
r1.mkdir()
with Script(["Whoops", "r 6", "d", "Real", ""]):
    check("r N removes a typed name", chute.setup_destinations(r1),
          [{"label": "Real", "path": "Real"}])
check("no Whoops folder was made", (r1 / "Whoops").exists(), False)

r2 = tmp / "Remove2"
r2.mkdir()
with Script(["Whoops", "r", "6", "d", "Real", ""]):
    check("bare r asks which number", chute.setup_destinations(r2),
          [{"label": "Real", "path": "Real"}])

r3 = tmp / "Remove3"
r3.mkdir()
with Script(["Typo", "r 1", "1", "r 9", "d", ""]):
    got = chute.setup_destinations(r3)
    check("r on a built-in explains, out-of-range is refused, Inbox stays",
          [d["label"] for d in got], ["Inbox", "Typo"])

section("typed words in the existing-folders checklist stay out")
words = tmp / "Words"
(words / "Docs").mkdir(parents=True)
with Script(["Brand New", "1", "d", "Brand New", ""]):
    got = chute.setup_destinations(words)
    check("the word never becomes a checklist button, typing later does",
          [d["label"] for d in got], ["Docs", "Brand New"])
check("its folder was created by the typing step",
      (words / "Brand New").is_dir(), True)

section("checklist feeds straight into the typing loop")
mixed = tmp / "Mixed"
(mixed / "Existing One").mkdir(parents=True)
with Script(["1", "d", "Typed One", ""]):
    check("keeps the picked folder and the typed one",
          chute.setup_destinations(mixed),
          [{"label": "Existing One", "path": "Existing One"},
           {"label": "Typed One", "path": "Typed One"}])
check("typed folder created", (mixed / "Typed One").is_dir(), True)

with Script(["1", "d", "Existing One", ""]):
    check("a picked folder cannot be typed again as a duplicate",
          chute.setup_destinations(mixed),
          [{"label": "Existing One", "path": "Existing One"}])

with Script(["1", "d", "undo", "Other", ""]):
    check("undo can take back a picked folder",
          chute.setup_destinations(mixed),
          [{"label": "Other", "path": "Other"}])
check("undo left the pre-existing folder alone",
      (mixed / "Existing One").is_dir(), True)

with Script(["1", "d", ""]):
    check("picking alone is enough to finish",
          chute.setup_destinations(mixed),
          [{"label": "Existing One", "path": "Existing One"}])

section("token handling")
check("quotes stripped", chute.clean_token('  "123:abc"  '), "123:abc")
check("plain token untouched",
      chute.clean_token("1234567890:AAF6yzXqML2wp3aBcDeFgHiJkLmNoP"),
      "1234567890:AAF6yzXqML2wp3aBcDeFgHiJkLmNoP")
good = "1234567890:AAF6yzXqML2wp3aBcDeFgHiJkLmNoP"
check("real-shaped token accepted", bool(chute.TOKEN_RE.match(good)), True)
for bad, why in [("botfather", "a word"), ("1234567890", "digits only"),
                 ("12:short", "code too short"),
                 ("t.me/mybot", "a link"), ("", "empty")]:
    check("rejects %s" % why, bool(chute.TOKEN_RE.match(bad)), False)

class TokenTG:
    """getMe succeeds only for the one good token."""
    def __init__(self, token):
        self.token = token
    def call(self, method, **p):
        if self.token == good:
            return {"username": "testbot"}
        raise chute.ConfigError("Telegram rejected the bot token.")

orig_tg = chute.Telegram
chute.Telegram = TokenTG
with Script(["q"]):
    check("q quits the token prompt", chute.ask_token(), None)
with Script(["", "botfather", good]):
    got = chute.ask_token()
    check("bad pastes re-ask until a token works",
          (got[0], got[2]["username"]), (good, "testbot"))
with Script(["1111111111:" + "x" * 30, good]):
    got = chute.ask_token()
    check("rejected token re-asks instead of aborting", got[0], good)
chute.Telegram = orig_tg

section("setup from the program folder")
fake_repo = tmp / "Repo"
fake_repo.mkdir()
fake_home = tmp / "FakeHome"
fake_home.mkdir()
orig_here, orig_home = chute.HERE, chute.Path.home
chute.HERE = fake_repo
chute.Path.home = staticmethod(lambda: fake_home)
os.chdir(fake_repo)
with Script(["1", "y"]):
    check("suggests a home folder, not the program folder",
          chute.prompt_root(), (fake_home / "Chute").resolve())
check("and creates it", (fake_home / "Chute").is_dir(), True)
check("no chute-in-Chute nesting", (fake_home / "Chute" / "chute").exists(),
      False)
chute.HERE, chute.Path.home = orig_here, orig_home
os.chdir(tmp)

section("stray words at the root prompt")
check("bare word is not a path", chute.looks_like_path("back"), False)
check("absolute path is", chute.looks_like_path("/Users/x"), True)
check("tilde path is", chute.looks_like_path("~/Notes"), True)
check("relative with slash is", chute.looks_like_path("Notes/Sub"), True)
check("dragged quoted path is", chute.looks_like_path('"/My Folder"'), True)
check("dot-relative is", chute.looks_like_path("./here"), True)
with Script(["back"]):
    check("back leaves the prompt instead of becoming a folder",
          chute.prompt_root(), None)
with Script(["help", "oops", "q"]):
    check("stray words are rejected until a real answer",
          chute.prompt_root(), None)
stray = tmp / "Stray"
stray.mkdir()
os.chdir(stray)
with Script(["hello", "1"]):
    check("after a rejected word, 1 still works",
          chute.prompt_root(), stray.resolve())
check("no folder called hello appeared", (stray / "hello").exists(), False)
check("no folder appeared next to it either",
      (stray.parent / "hello").exists(), False)

section("a bot held by another computer is explained, not retried")
class ConflictTG:
    def call(self, method, **p):
        raise chute.TelegramConflict("terminated by other getUpdates request")
def wait_conflict():
    chute.wait_for_first_message(ConflictTG(), 0, seconds=5)
raises("conflict is re-raised to the caller", wait_conflict,
       chute.TelegramConflict)

class QuietTG:
    def call(self, method, **p):
        return []
check("a quiet wait times out cleanly",
      chute.wait_for_first_message(QuietTG(), 0, seconds=0), (None, None, 0))

section("quitting the root prompt")
with Script(["q"]):
    check("q leaves setup", chute.prompt_root(), None)
with Script(["quit"]):
    check("quit leaves setup", chute.prompt_root(), None)

os.chdir("/")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(report())
