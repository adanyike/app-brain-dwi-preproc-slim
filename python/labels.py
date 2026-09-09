#!/usr/bin/env python3
"""Resolve the subject and session labels attached to a run's results.

Sites encode the session in the subject label in different ways --
``sub-FA023_ses-03``, ``sub01-MR03``, ``sub01_MR03`` -- and grouping results by
timepoint needs them apart. Splitting on the last delimiter does not work:
``sub-FA023_ses-03`` would become ``sub-FA023_ses`` and ``03``, and
``sub-FA023`` with no session at all would become ``sub`` and ``FA023``.

So a split is only made when the trailing part *looks like a session*: a
recognised session prefix followed by a number. Delimiters are tried from the
right, and if nothing matches the label is left exactly as it was. The whole
behaviour is opt-in (``split_subject_session`` in config.json) because silently
relabelling somebody's data is worse than leaving it joined.

On brainlife this is normally unnecessary: subject and session arrive separately
in the input metadata.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Sequence

# Prefixes that mark a session/timepoint token. "sub", "FA" and other subject
# fragments deliberately absent -- that is what stops sub-FA023 being split.
DEFAULT_SESSION_PREFIXES = ("ses", "MR", "visit", "tp", "V")

DELIMITERS = "-_"


def looks_like_session(token: str, prefixes: Sequence[str]) -> bool:
    """True for ses-03, ses03, MR03, visit2 ... and false for 03 or FA023."""
    for prefix in prefixes:
        if re.fullmatch(re.escape(prefix) + r"[-_]?\d+", token, flags=re.IGNORECASE):
            return True
    return False


def split_subject_session(label: str, prefixes: Sequence[str] = DEFAULT_SESSION_PREFIXES
                          ) -> tuple[str, str]:
    """Return (subject, session); session is empty when nothing splits off."""
    for index in range(len(label) - 1, 0, -1):
        if label[index] not in DELIMITERS:
            continue
        head, tail = label[:index], label[index + 1:]
        if head and looks_like_session(tail, prefixes):
            return head, tail
    return label, ""


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--session", default="")
    ap.add_argument("--split", action="store_true",
                    help="split a session off the subject label when session is empty")
    ap.add_argument("--session-prefixes", default=",".join(DEFAULT_SESSION_PREFIXES))
    args = ap.parse_args(argv)

    subject, session = args.subject, args.session

    # An explicit session always wins; nothing is ever overwritten.
    if args.split and not session:
        prefixes = [p for p in args.session_prefixes.split(",") if p]
        subject, session = split_subject_session(subject, prefixes)
        if session:
            print("labels: split %r into subject %r, session %r"
                  % (args.subject, subject, session), file=sys.stderr)
        else:
            print("labels: %r has no recognisable session suffix; left as is"
                  % args.subject, file=sys.stderr)

    print("%s\t%s" % (subject, session))
    return 0


if __name__ == "__main__":
    sys.exit(main())
