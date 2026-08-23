#!/usr/bin/env python3
"""Summaries: what is sent, what comes back, and what happens when it does not.

Nothing here opens a socket. Summariser.post is the one method that makes the
HTTP call and does nothing else, so a canned answer replaces it - the same
trick as the shell scripts that stand in for whisper and the diarizer.
"""
import json
import os
import tempfile
import urllib.error
from pathlib import Path

from harness import check, raises, section, report  # noqa: F401
import chute


section("a key belongs in the environment, not in the config")
raises("a key written into config.json is refused",
       lambda: chute.Summariser({"api_key": "sk-ant-oops"}), chute.ConfigError)
raises("under either name",
       lambda: chute.Summariser({"key": "sk-ant-oops"}), chute.ConfigError)
raises("and transcripts are not sent in clear text",
       lambda: chute.Summariser({"base_url": "http://example.com"}),
       chute.ConfigError)
check("a proxy on this machine is allowed",
      chute.Summariser({"base_url": "http://127.0.0.1:8000"}).base_url,
      "http://127.0.0.1:8000")

envfile = Path(tempfile.mkdtemp()) / ".env"
envfile.write_text("# a comment\n\nCHUTE_ENV_ONE=first\n"
                   'CHUTE_ENV_TWO="quoted"\nnot a pair\n')
os.environ.pop("CHUTE_ENV_ONE", None)
os.environ["CHUTE_ENV_TWO"] = "already set"
chute.load_env_file(envfile)
check("a value is read out of service/.env",
      os.environ.get("CHUTE_ENV_ONE"), "first")
check("what is already in the environment wins",
      os.environ.get("CHUTE_ENV_TWO"), "already set")
check("and a file that is not there is not fatal",
      chute.load_env_file("/no/such/.env"), None)


section("the summary request is the one the API expects")
KEY = "sk-ant-test"
os.environ["ANTHROPIC_API_KEY"] = KEY
teller = chute.Summariser({"enabled": True})
body = teller.body("some words that were said", "Hebrew (he)")
check("the model is named", body["model"], "claude-opus-5")
check("the transcript is the message",
      body["messages"][0]["content"], "some words that were said")
check("a schema makes the answer parseable rather than scraped",
      body["output_config"]["format"]["schema"]["required"],
      ["headline", "bullets"])
check("cheap task, low effort", body["output_config"]["effort"], "low")
# Thinking is on by default on this model and max_tokens caps thinking and
# answer together, so a small number buys a truncated summary, not a short one.
check("there is headroom over the thinking", body["max_tokens"], 4096)
check("a refusal re-runs rather than coming back empty",
      body["fallbacks"], "default")
# All four are removed on this model and would come back a 400.
for gone in ("temperature", "top_p", "top_k", "thinking"):
    check("no %s is sent" % gone, gone in body, False)
check("the recording's language is asked for",
      "Hebrew (he)" in body["system"], True)
check("and so is the number of points", "4 bullets" in body["system"], True)
check("a runaway transcript is cut before it is sent",
      len(chute.Summariser({"enabled": True, "max_chars": 20})
          .body("z" * 5000, "")["messages"][0]["content"]), 20)
raises("an absurd number of bullets is refused",
       lambda: chute.Summariser({"bullets": 99}), chute.ConfigError)


section("a summary that does not come back costs the transcript nothing")


def answering(reply):
    """A summariser whose one HTTP call is replaced by a canned answer."""
    teller = chute.Summariser({"enabled": True})
    teller.post = reply if callable(reply) else (lambda body: reply)
    return teller


def said(payload):
    return {"stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(payload)}]}


check("a good answer is read",
      answering(said({"headline": "Two people on keys.",
                      "bullets": ["One.", "Two."]})).summarise("words", "en"),
      ("Two people on keys.", ["One.", "Two."]))
check("a blank bullet is dropped, not printed",
      answering(said({"headline": "H.", "bullets": ["a", "", "  "]}))
      .summarise("words")[1], ["a"])
for label, reply in [
        ("an answer cut off", {"stop_reason": "max_tokens",
                              "content": [{"type": "text", "text": "{\"head"}]}),
        ("a refusal", {"stop_reason": "refusal",
                       "stop_details": {"category": "cyber"}, "content": []}),
        ("an answer that is not JSON",
         {"content": [{"type": "text", "text": "I think it is about keys"}]}),
        ("JSON with no headline", said({"bullets": ["a"]})),
        ("bullets that are not a list", said({"headline": "H", "bullets": 3})),
        ("an empty headline", said({"headline": "   ", "bullets": []})),
        ("no content at all", {"content": []})]:
    check("%s yields nothing and does not raise" % label,
          answering(reply).summarise("words"), (None, []))


def raiser(exc):
    def post(body):
        raise exc
    return post


check("a network that is down yields nothing",
      answering(raiser(urllib.error.URLError("unreachable")))
      .summarise("words"), (None, []))
check("so does a socket that times out",
      answering(raiser(OSError("timed out"))).summarise("words"), (None, []))
check("so does an error from the API",
      answering(raiser(urllib.error.HTTPError(
          "u", 401, "Unauthorized", {}, None))).summarise("words"), (None, []))
check("nothing is sent when there is nothing to say",
      answering(said({"headline": "H", "bullets": []})).summarise("   "),
      (None, []))
del os.environ["ANTHROPIC_API_KEY"]
check("asked for without a key, it is not ready",
      chute.Summariser({"enabled": True}).ready(), False)
check("and it says what is missing",
      "console.anthropic.com" in chute.Summariser({"enabled": True})
      .missing()[0], True)
check("a key with nobody asking is still off",
      chute.Summariser({"api_key_file": "/dev/null"}).ready(), False)
keyfile = Path(tempfile.mkdtemp()) / "key"
keyfile.write_text("sk-ant-from-a-file\n")
check("a key file works where a service has no environment",
      chute.Summariser({"enabled": True,
                        "api_key_file": str(keyfile)}).key(),
      "sk-ant-from-a-file")
check("and a file that is not there is not fatal",
      chute.Summariser({"enabled": True,
                        "api_key_file": "/no/such/key"}).key(), "")


section("the summary sits above the words, and the headline in the frontmatter")
SUMMARY = ("Two engineers arguing about where a signing key should live.",
           ["Splitting it three ways is the only workable answer.",
            "One place becomes the whole attack surface."])
WORDS = [chute.Segment(0.0, 3.0, "First line."),
         chute.Segment(4.0, 7.0, "Second line.")]
noted = chute.transcript_section(WORDS, {"language": "English (en)"},
                                 summary=SUMMARY)
check("the summary is a heading of its own",
      chute.SUMMARY_HEADING in noted, True)
check("and it comes before the words",
      noted.index(chute.SUMMARY_HEADING)
      < noted.index(chute.TRANSCRIPT_HEADING), True)
check("the headline is there", SUMMARY[0] in noted, True)
check("the points are a list", "- %s" % SUMMARY[1][0] in noted, True)
check("a headline with no points still stands",
      chute.SUMMARY_HEADING in chute.transcript_section(
          WORDS, {}, summary=("Just a headline.", [])), True)
# The whole no-regression guarantee, in one line.
check("no summary reads exactly as before",
      chute.transcript_section(WORDS, {"language": "English (en)"}),
      chute.transcript_section(WORDS, {"language": "English (en)"},
                               summary=(None, [])))
check("the headline reaches the frontmatter as one line",
      'transcript-summary: "%s"' % SUMMARY[0]
      in chute.transcript_note("talk.m4a", {"headline": SUMMARY[0]}, ""), True)
check("and is left out when there is none",
      "transcript-summary" in chute.transcript_note("talk.m4a", {}, ""), False)

report()
