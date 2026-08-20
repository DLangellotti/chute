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
with Script(["1", "n"]):
    check("option 1 gives the current directory", chute.prompt_root(),
          (tmp / "Existing").resolve())

with Script(["2", str(tmp / "Existing"), "n"]):
    check("option 2 takes a typed path", chute.prompt_root(),
          (tmp / "Existing").resolve())

with Script([str(tmp / "Existing"), "n"]):
    check("a pasted path skips the menu", chute.prompt_root(),
          (tmp / "Existing").resolve())

with Script(["", "n"]):
    check("blank defaults to option 1", chute.prompt_root(),
          (tmp / "Existing").resolve())

section("creating a folder that is not there yet")
fresh = tmp / "Fresh"
with Script([str(fresh), "y", "n"]):
    check("offers to create, then uses it", chute.prompt_root(), fresh.resolve())
check("it really exists now", fresh.is_dir(), True)

declined = tmp / "Declined"
with Script([str(declined), "n", str(tmp / "Existing"), "n"]):
    check("declining creation asks again", chute.prompt_root(),
          (tmp / "Existing").resolve())
check("declined folder not created", declined.exists(), False)

section("the chute subfolder offer")
with Script([str(tmp / "Existing"), "y"]):
    check("yes nests a chute folder", chute.prompt_root(),
          (tmp / "Existing" / "chute").resolve())
check("subfolder created", (tmp / "Existing" / "chute").is_dir(), True)

with Script([str(tmp / "Existing"), "n"]):
    check("no keeps the folder as chosen", chute.prompt_root(),
          (tmp / "Existing").resolve())

already = tmp / "chute"
already.mkdir()
with Script([str(already)]):
    check("no nesting offer when already named chute", chute.prompt_root(),
          already.resolve())
check("would not nest chute/chute", (already / "chute").exists(), False)

section("rejecting bad choices")
afile = tmp / "afile.txt"
afile.write_text("x")
with Script([str(afile), str(tmp / "Existing"), "n"]):
    check("a file is refused, asks again", chute.prompt_root(),
          (tmp / "Existing").resolve())

with Script(["", "n"]) as s:
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
with Script(["Receipts", "Work Photos", ""]):
    check("one folder per button",
          chute.setup_destinations(buttons),
          [{"label": "Receipts", "path": "Receipts"},
           {"label": "Work Photos", "path": "Work Photos"}])
check("first folder created", (buttons / "Receipts").is_dir(), True)
check("spaces kept in the folder name",
      (buttons / "Work Photos").is_dir(), True)

(buttons / "Already There").mkdir()
(buttons / "Already There" / "keep.txt").write_text("x")
with Script(["Already There", ""]):
    check("an existing folder is reused", chute.setup_destinations(buttons),
          [{"label": "Already There", "path": "Already There"}])
check("existing contents untouched",
      (buttons / "Already There" / "keep.txt").exists(), True)

with Script(["Notes", "notes", "", ""]):
    check("a duplicate label is refused", chute.setup_destinations(buttons),
          [{"label": "Notes", "path": "Notes"}])

with Script(["", "Inbox", ""]):
    check("blank first answer asks again", chute.setup_destinations(buttons),
          [{"label": "Inbox", "path": "Inbox"}])

with Script(["../escape", "Safe", ""]):
    check("traversal refused", chute.setup_destinations(buttons),
          [{"label": "Safe", "path": "Safe"}])
check("nothing created outside the root",
      (tmp / "escape").exists(), False)

with Script([".hidden", ""]):
    check("a leading dot is dropped, not made into a dot-folder",
          chute.setup_destinations(buttons),
          [{"label": ".hidden", "path": "hidden"}])
check("no dot-folder created", (buttons / ".hidden").exists(), False)
check("plain folder created instead", (buttons / "hidden").is_dir(), True)

with Script(["Talks/2026", "Talks", ""]):
    check("a slash is refused rather than flattened",
          chute.setup_destinations(buttons),
          [{"label": "Talks", "path": "Talks"}])
check("nothing named Talks2026", (buttons / "Talks2026").exists(), False)

with Script(["...", "Plain", ""]):
    check("a name with nothing usable in it is refused",
          chute.setup_destinations(buttons),
          [{"label": "Plain", "path": "Plain"}])

with Script(["Bills: 2026", ""]):
    check("an illegal character is dropped from the folder name",
          chute.setup_destinations(buttons),
          [{"label": "Bills: 2026", "path": "Bills 2026"}])
check("folder created under the cleaned name",
      (buttons / "Bills 2026").is_dir(), True)

os.chdir("/")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(report())
