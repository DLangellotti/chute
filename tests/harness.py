"""Shared test helpers. No framework: these run on a bare python3."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import chute  # noqa: E402

FAILURES = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILURES.append("%s: got %r, want %r" % (label, got, want))
    print("  %-46s %s" % (label, "ok" if ok else "FAIL got=%r" % (got,)))
    return ok


def raises(label, fn, exc=Exception):
    try:
        fn()
    except exc:
        print("  %-46s ok" % label)
        return True
    except Exception as other:
        FAILURES.append("%s: raised %r, wanted %s" % (label, other, exc.__name__))
        print("  %-46s FAIL wrong error %r" % (label, other))
        return False
    FAILURES.append("%s: did not raise" % label)
    print("  %-46s FAIL no error" % label)
    return False


def section(title):
    print("\n%s" % title)


def make_config(root, **overrides):
    data = {
        "bot_token": "test-token",
        "allowed_user_ids": [555],
        "root": str(root),
        "destinations": [
            {"label": "📡 Work", "path": "Work/Attachments",
             "by_kind": {"media": "Media/Work", "text": "Work/Inbox"}},
            {"label": "🏠 Personal", "path": "Personal/Attachments"},
        ],
    }
    data.update(overrides)
    return chute.Config(data)


def report():
    if FAILURES:
        print("\nFAILURES:\n  " + "\n  ".join(FAILURES))
        return 1
    print("\nALL PASS")
    return 0
