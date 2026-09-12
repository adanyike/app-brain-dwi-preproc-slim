#!/usr/bin/env python3
"""Unit tests for python/rotated_bvecs.py."""
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import rotated_bvecs as rb  # noqa: E402

NAN = float("nan")


class TestSanitise(unittest.TestCase):
    def test_unweighted_nan_becomes_zero(self):
        """The case eddy actually produces: b0 written as 0 0 0, b-value 5."""
        bvecs = [[NAN, 1.0], [NAN, 0.0], [NAN, 0.0]]
        cleaned, repaired = rb.sanitise(bvecs, [5.0, 1500.0])
        self.assertEqual(repaired, [0])
        self.assertEqual([cleaned[a][0] for a in range(3)], [0.0, 0.0, 0.0])

    def test_weighted_volumes_are_untouched(self):
        bvecs = [[NAN, 1.0], [NAN, 0.0], [NAN, 0.0]]
        cleaned, _ = rb.sanitise(bvecs, [5.0, 1500.0])
        self.assertEqual([cleaned[a][1] for a in range(3)], [1.0, 0.0, 0.0])

    def test_nan_on_a_weighted_volume_is_fatal(self):
        """Repairing that one would mean inventing a gradient direction."""
        bvecs = [[NAN], [NAN], [NAN]]
        with self.assertRaises(rb.BvecError):
            rb.sanitise(bvecs, [1500.0])

    def test_infinite_direction_is_treated_like_nan(self):
        bvecs = [[float("inf")], [0.0], [0.0]]
        cleaned, repaired = rb.sanitise(bvecs, [0.0])
        self.assertEqual(repaired, [0])
        self.assertEqual([cleaned[a][0] for a in range(3)], [0.0, 0.0, 0.0])

    def test_clean_table_is_returned_unchanged(self):
        bvecs = [[1.0, 0.0], [0.0, 1.0], [0.0, 0.0]]
        cleaned, repaired = rb.sanitise(bvecs, [1500.0, 1500.0])
        self.assertEqual(repaired, [])
        self.assertEqual(cleaned, [[1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])

    def test_threshold_is_honoured(self):
        bvecs = [[NAN], [NAN], [NAN]]
        with self.assertRaises(rb.BvecError):
            rb.sanitise(bvecs, [60.0], b0_threshold=50.0)
        cleaned, repaired = rb.sanitise(bvecs, [60.0], b0_threshold=100.0)
        self.assertEqual(repaired, [0])

    def test_length_mismatch_is_fatal(self):
        with self.assertRaises(rb.BvecError):
            rb.sanitise([[1.0], [0.0], [0.0]], [1500.0, 1500.0])


class TestIo(unittest.TestCase):
    def test_round_trip_through_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            bvecs = os.path.join(tmp, "r.bvecs")
            bvals = os.path.join(tmp, "r.bvals")
            out = os.path.join(tmp, "clean.bvecs")
            with open(bvecs, "w") as fh:
                fh.write("nan 1.0\nnan 0.0\nnan 0.0\n")
            with open(bvals, "w") as fh:
                fh.write("5 1500\n")
            self.assertEqual(rb.main(["--bvecs", bvecs, "--bvals", bvals,
                                      "--out", out]), 0)
            rows = rb.read_bvecs(out)
            self.assertEqual([rows[a][0] for a in range(3)], [0.0, 0.0, 0.0])
            self.assertTrue(all(math.isfinite(v) for row in rows for v in row))

    def test_a_bvecs_file_must_have_three_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.bvecs")
            with open(path, "w") as fh:
                fh.write("1.0 0.0\n0.0 1.0\n")
            with self.assertRaises(rb.BvecError):
                rb.read_bvecs(path)


if __name__ == "__main__":
    unittest.main()
