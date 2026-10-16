#!/usr/bin/env python3
"""Crash triage for the nightly fuzz workflow (issues #1226, #1403).

  python3 tools/ci/fuzz_triage.py triage crash/log.txt   # prints sig= / severity=
  python3 tools/ci/fuzz_triage.py selftest

The signature is a hash of the first failing line with run-specific noise
removed (hex addresses, timestamps, durations, seed/case counters). The faulting
location (file:line) and message are kept, so the same crash on another night
maps to one tracking issue while a different fault gets its own.
"""

import argparse
import hashlib
import re
import sys

FAIL_LINE = re.compile(r"panicked|Error|error:|FAIL|mismatch")
HIGH_SEVERITY = re.compile(r"get_score|read_|query|view|oracle|invariant|security", re.I)
NOISE = [
    re.compile(r"0x[0-9a-fA-F]+"),
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"),
    re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"),
    re.compile(r"\b(?:seed|case|cases|iteration|pid|tid)[ =:]+\d+", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|us|µs|ns|s)\b"),
]


def failing_line(log: str) -> str:
    lines = [l for l in log.splitlines() if l.strip()]
    for l in lines:
        if FAIL_LINE.search(l):
            return l
    return lines[-1] if lines else ""


def signature(log: str) -> str:
    line = failing_line(log)
    for pat in NOISE:
        line = pat.sub("", line)
    line = re.sub(r"\s+", " ", line).strip()
    return hashlib.sha256(line.encode()).hexdigest()[:12]


def severity(log: str) -> str:
    return "high" if HIGH_SEVERITY.search(log) else "normal"


def tracking_action(sig: str, open_issue_titles: list[str]) -> str:
    """'update' when an open `fuzz crash [sig]` issue exists, else 'create'."""
    return "update" if f"fuzz crash [{sig}]" in open_issue_titles else "create"


def selftest() -> None:
    import unittest

    class T(unittest.TestCase):
        A = "thread 'main' panicked at src/score.rs:42:9:\nscore overflow 0x7ffd1234 at 2026-09-01T02:00:01Z seed 1234 (12ms)\n"
        A_LATER = "thread 'main' panicked at src/score.rs:42:9:\nscore overflow 0x7ffe9999 at 2026-09-02T02:00:07Z seed 9999 (31ms)\n"

        def test_same_crash_is_stable(self):
            a = "mismatch: score overflow 0x7ffd1234 at 2026-09-01T02:00:01Z seed 1234 (12ms)"
            b = "mismatch: score overflow 0x7ffe9999 at 2026-09-02T02:00:07Z seed 9999 (31ms)"
            self.assertEqual(signature(a), signature(b))
            self.assertEqual(signature(self.A), signature(self.A_LATER))

        def test_different_location_differs(self):
            self.assertNotEqual(
                signature("thread 'main' panicked at src/score.rs:42:9:"),
                signature("thread 'main' panicked at src/score.rs:77:9:"),
            )
            self.assertNotEqual(
                signature("thread 'main' panicked at src/a.rs:1:1:"),
                signature("thread 'main' panicked at src/b.rs:1:1:"),
            )

        def test_different_input_differs(self):
            self.assertNotEqual(signature("mismatch: score 41 != 42"), signature("mismatch: score 41 != 43"))

        def test_distinct_digits_not_merged(self):
            # Regression: stripping every digit merged unrelated crashes.
            self.assertNotEqual(signature("error: slot 1 corrupt"), signature("error: slot 2 corrupt"))

        def test_falls_back_to_last_line(self):
            self.assertEqual(signature("a\nb\nweird end"), signature("x\nweird end"))

        def test_severity(self):
            self.assertEqual(severity("mismatch in get_score"), "high")
            self.assertEqual(severity("mismatch in decoder"), "normal")

        def test_two_run_sequence(self):
            """Run 1: crash A. Run 2: crash A again plus new crash B."""
            issues: list[str] = []
            created, updated = [], []
            run1 = [signature("panicked at src/a.rs:1:1")]
            run2 = [signature("panicked at src/a.rs:1:1 seed 5"), signature("panicked at src/b.rs:9:9")]
            for crashes in (run1, run2):
                for sig in crashes:
                    if tracking_action(sig, issues) == "update":
                        updated.append(sig)
                    else:
                        created.append(sig)
                        issues.append(f"fuzz crash [{sig}]")
            self.assertEqual(len(created), 2)
            self.assertEqual(updated, [run1[0]])
            self.assertEqual(len(issues), 2)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("triage")
    t.add_argument("log")
    sub.add_parser("selftest")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        selftest()
    log = open(args.log, errors="replace").read()
    print(f"sig={signature(log)}")
    print(f"severity={severity(log)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
