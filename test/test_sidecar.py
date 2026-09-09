#!/usr/bin/env python3
"""Unit tests for python/sidecar.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import sidecar  # noqa: E402


class FindListTests(unittest.TestCase):
    def test_flat_bids_sidecar_is_unchanged(self):
        value, path = sidecar.find_list({"SliceTiming": [0.0, 0.5]}, "SliceTiming")
        self.assertEqual(value, [0.0, 0.5])
        self.assertEqual(path, "SliceTiming")

    def test_missing_field_is_not_an_error(self):
        self.assertEqual(sidecar.find_list({"a": 1}, "SliceTiming"), (None, ""))

    def test_nested_field_is_found_and_its_path_reported(self):
        meta = {"time": {"samples": {"CsaImage.MosaicRefAcqTimes": [0.0, 140.0]}}}
        value, path = sidecar.find_list(meta, "CsaImage.MosaicRefAcqTimes")
        self.assertEqual(value, [0.0, 140.0])
        self.assertEqual(path, "time.samples.CsaImage.MosaicRefAcqTimes")

    def test_a_top_level_key_beats_a_nested_one(self):
        meta = {"SliceTiming": [0.0, 0.5], "nested": {"SliceTiming": [9.0, 9.5]}}
        self.assertEqual(sidecar.find_list(meta, "SliceTiming")[0], [0.0, 0.5])

    def test_per_volume_rows_collapse_to_one(self):
        meta = {"CsaImage.MosaicRefAcqTimes": [[0.0, 140.0]] * 99}
        self.assertEqual(
            sidecar.find_list(meta, "CsaImage.MosaicRefAcqTimes")[0], [0.0, 140.0])

    def test_decimal_round_trip_jitter_is_not_a_difference(self):
        """A DICOM dump writes the same time as ...49999999 and ...50000001."""
        meta = {"CsaImage.MosaicRefAcqTimes": [[697.49999999, 1257.49999999],
                                               [697.50000001, 1257.50000001]]}
        value, _ = sidecar.find_list(meta, "CsaImage.MosaicRefAcqTimes")
        self.assertEqual(value, [697.49999999, 1257.49999999])

    def test_a_near_zero_difference_is_zero(self):
        """The first excitation is 0.0; a dump may write 1e-8 for it instead."""
        meta = {"CsaImage.MosaicRefAcqTimes": [[0.0, 140.0], [1e-8, 140.0]]}
        value, _ = sidecar.find_list(meta, "CsaImage.MosaicRefAcqTimes")
        self.assertEqual(value, [0.0, 140.0])

    def test_a_real_difference_near_zero_is_still_caught(self):
        meta = {"CsaImage.MosaicRefAcqTimes": [[0.0, 140.0], [70.0, 140.0]]}
        with self.assertRaises(sidecar.SidecarError):
            sidecar.find_list(meta, "CsaImage.MosaicRefAcqTimes")

    def test_genuinely_differing_rows_are_refused(self):
        meta = {"CsaImage.MosaicRefAcqTimes": [[0.0, 140.0], [0.0, 280.0]]}
        with self.assertRaises(sidecar.SidecarError) as ctx:
            sidecar.find_list(meta, "CsaImage.MosaicRefAcqTimes")
        self.assertIn("volume 1", str(ctx.exception))

    def test_rows_of_differing_length_are_refused(self):
        meta = {"CsaImage.MosaicRefAcqTimes": [[0.0, 140.0], [0.0]]}
        with self.assertRaises(sidecar.SidecarError):
            sidecar.find_list(meta, "CsaImage.MosaicRefAcqTimes")

    def test_a_scalar_where_a_list_belongs_is_refused(self):
        with self.assertRaises(sidecar.SidecarError):
            sidecar.find_list({"SliceTiming": 0.5}, "SliceTiming")


class FindScalarTests(unittest.TestCase):
    def test_flat_bids_sidecar_is_unchanged(self):
        value, path = sidecar.find_scalar({"PhaseEncodingDirection": "j-"},
                                          "PhaseEncodingDirection")
        self.assertEqual(value, "j-")
        self.assertEqual(path, "PhaseEncodingDirection")

    def test_names_are_tried_in_order(self):
        meta = {"PhaseEncodingAxis": "j"}
        self.assertEqual(
            sidecar.find_scalar(meta, "PhaseEncodingDirection", "PhaseEncodingAxis")[0],
            "j")

    def test_nested_field_is_found(self):
        meta = {"global": {"const": {"TotalReadoutTime": 0.0342}}}
        value, path = sidecar.find_scalar(meta, "TotalReadoutTime")
        self.assertEqual(value, 0.0342)
        self.assertEqual(path, "global.const.TotalReadoutTime")

    def test_a_value_repeated_per_volume_collapses(self):
        meta = {"time": {"samples": {"TotalReadoutTime": [0.0342] * 99}}}
        self.assertEqual(sidecar.find_scalar(meta, "TotalReadoutTime")[0], 0.0342)

    def test_a_value_that_varies_per_volume_is_refused(self):
        meta = {"TotalReadoutTime": [0.0342, 0.0400]}
        with self.assertRaises(sidecar.SidecarError):
            sidecar.find_scalar(meta, "TotalReadoutTime")

    def test_missing_field_is_not_an_error(self):
        self.assertEqual(sidecar.find_scalar({"a": 1}, "TotalReadoutTime"), (None, ""))


if __name__ == "__main__":
    unittest.main()
