#!/usr/bin/env python3
"""Unit tests for python/labels.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import labels  # noqa: E402


class TestSplit(unittest.TestCase):
    def test_the_three_site_conventions(self):
        self.assertEqual(labels.split_subject_session("sub-FA023_ses-03"),
                         ("sub-FA023", "ses-03"))
        self.assertEqual(labels.split_subject_session("sub01-MR03"), ("sub01", "MR03"))
        self.assertEqual(labels.split_subject_session("sub01_MR03"), ("sub01", "MR03"))

    def test_a_subject_containing_a_delimiter_is_not_split(self):
        # Splitting on the last delimiter would give ("sub", "FA023").
        self.assertEqual(labels.split_subject_session("sub-FA023"), ("sub-FA023", ""))

    def test_a_bare_number_is_not_a_session(self):
        # ("sub-FA023_ses", "03") is the wrong answer for the first of these.
        self.assertEqual(labels.split_subject_session("sub-01"), ("sub-01", ""))
        self.assertEqual(labels.split_subject_session("sub01"), ("sub01", ""))

    def test_other_recognised_session_prefixes(self):
        self.assertEqual(labels.split_subject_session("sub-A_visit2"), ("sub-A", "visit2"))
        self.assertEqual(labels.split_subject_session("sub-A_tp1"), ("sub-A", "tp1"))
        self.assertEqual(labels.split_subject_session("sub-A_V01"), ("sub-A", "V01"))

    def test_session_prefix_variants(self):
        for label, expected in (("s_ses-03", "ses-03"), ("s_ses03", "ses03"),
                                ("s_ses_03", "ses_03"), ("s_mr03", "mr03")):
            self.assertEqual(labels.split_subject_session(label)[1], expected, label)

    def test_prefix_list_is_configurable(self):
        self.assertEqual(labels.split_subject_session("sub-A_wave2"), ("sub-A_wave2", ""))
        self.assertEqual(labels.split_subject_session("sub-A_wave2", ("wave",)),
                         ("sub-A", "wave2"))

    def test_never_produces_an_empty_subject(self):
        self.assertEqual(labels.split_subject_session("_ses-01"), ("_ses-01", ""))

    def test_rightmost_session_wins(self):
        self.assertEqual(labels.split_subject_session("sub-01_ses-01"), ("sub-01", "ses-01"))


class TestCli(unittest.TestCase):
    def _run(self, argv):
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            labels.main(argv)
        return out.getvalue().rstrip("\n").split("\t")

    def test_without_split_the_label_is_untouched(self):
        self.assertEqual(self._run(["--subject", "sub01-MR03"]), ["sub01-MR03", ""])

    def test_with_split(self):
        self.assertEqual(self._run(["--subject", "sub01-MR03", "--split"]),
                         ["sub01", "MR03"])

    def test_an_explicit_session_is_never_overwritten(self):
        self.assertEqual(
            self._run(["--subject", "sub01-MR03", "--session", "given", "--split"]),
            ["sub01-MR03", "given"])


if __name__ == "__main__":
    unittest.main()
