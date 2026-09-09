#!/usr/bin/env python3
"""Unit tests for python/roi_stats.py, on a synthetic atlas with known means."""
import csv
import json
import os
import sys
import tempfile
import unittest

import numpy as np
import nibabel as nib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import roi_stats as rs  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SHIPPED_LABELS = os.path.join(HERE, "..", "templates", "JHU-ICBM-labels.json")


def save(path, array, voxel_size=2.0):
    affine = np.diag([voxel_size, voxel_size, voxel_size, 1.0])
    nib.save(nib.Nifti1Image(array, affine), path)


class TestShippedLabelFile(unittest.TestCase):
    def test_has_48_unique_contiguous_labels(self):
        labels = rs.load_labels(SHIPPED_LABELS)
        self.assertEqual(len(labels), 48)
        self.assertEqual([l["index"] for l in labels], list(range(1, 49)))
        self.assertEqual(len({l["abbreviation"] for l in labels}), 48)
        self.assertEqual(len({l["name"] for l in labels}), 48)

    def test_hemispheres_are_paired(self):
        labels = rs.load_labels(SHIPPED_LABELS)
        left = sum(1 for l in labels if l["hemisphere"] == "left")
        right = sum(1 for l in labels if l["hemisphere"] == "right")
        self.assertEqual(left, right)
        self.assertEqual(left, 21)


class TestRoiStats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name

        # 4x4x4 volume: ROI 1 is the first z-plane, ROI 2 the second.
        atlas = np.zeros((4, 4, 4), dtype=np.int16)
        atlas[:, :, 0] = 1
        atlas[:, :, 1] = 2
        save(os.path.join(d, "atlas.nii.gz"), atlas)

        fa = np.zeros((4, 4, 4), dtype=np.float32)
        fa[:, :, 0] = 0.5          # ROI 1 -> mean 0.5
        fa[:, :, 1] = 0.2          # ROI 2 -> mean 0.2
        save(os.path.join(d, "fa.nii.gz"), fa)

        mask = np.ones((4, 4, 4), dtype=np.uint8)
        mask[0, :, :] = 0          # drops a quarter of every ROI
        save(os.path.join(d, "mask.nii.gz"), mask)

        labels = {"n_labels": 3, "labels": [
            {"index": 1, "name": "roi one",   "abbreviation": "R1", "hemisphere": "left"},
            {"index": 2, "name": "roi two",   "abbreviation": "R2", "hemisphere": "right"},
            {"index": 3, "name": "roi three", "abbreviation": "R3", "hemisphere": "left"},
        ]}
        with open(os.path.join(d, "labels.json"), "w") as fh:
            json.dump(labels, fh)
        self.d = d

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *extra):
        out = os.path.join(self.d, "out")
        rs.main(["--atlas", os.path.join(self.d, "atlas.nii.gz"),
                 "--labels", os.path.join(self.d, "labels.json"),
                 "--metric", "FA=" + os.path.join(self.d, "fa.nii.gz"),
                 "--subject", "sub-test", "--run-id", "task-123",
                 "--outdir", out, *extra])
        with open(os.path.join(out, "roi_stats.json")) as fh:
            return out, json.load(fh)

    def test_means_and_volumes(self):
        out, payload = self._run()
        by_roi = {r["roi_index"]: r for r in payload["stats"]}
        self.assertAlmostEqual(by_roi[1]["mean"], 0.5, places=6)
        self.assertAlmostEqual(by_roi[2]["mean"], 0.2, places=6)
        self.assertEqual(by_roi[1]["n_voxels"], 16)
        self.assertAlmostEqual(by_roi[1]["volume_mm3"], 16 * 8.0, places=3)
        self.assertEqual(by_roi[1]["std"], 0.0)
        self.assertTrue(os.path.exists(os.path.join(out, "FA_mean.csv")))

    def test_missing_roi_is_reported_not_fatal(self):
        _, payload = self._run()
        self.assertEqual(payload["empty_rois"], ["R3"])
        by_roi = {r["roi_index"]: r for r in payload["stats"]}
        self.assertEqual(by_roi[3]["n_voxels"], 0)
        self.assertNotEqual(by_roi[3]["mean"], by_roi[3]["mean"])   # NaN

    def test_brain_mask_restricts_each_roi(self):
        _, payload = self._run("--brain-mask", os.path.join(self.d, "mask.nii.gz"))
        by_roi = {r["roi_index"]: r for r in payload["stats"]}
        self.assertEqual(by_roi[1]["n_voxels"], 12)
        self.assertAlmostEqual(by_roi[1]["mean"], 0.5, places=6)

    def test_tidy_csv_round_trips(self):
        out, _ = self._run()
        with open(os.path.join(out, "roi_stats.csv")) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["subject"], "sub-test")
        self.assertEqual(rows[0]["roi_name"], "roi one")

    def test_wide_csv_has_one_column_per_roi(self):
        out, _ = self._run()
        with open(os.path.join(out, "FA_mean.csv")) as fh:
            header, values = list(csv.reader(fh))
        self.assertEqual(header[:3], ["subject", "session", "run_id"])
        self.assertEqual(header[3:], ["1_R1", "2_R2", "3_R3"])
        self.assertEqual(values[:3], ["sub-test", "", "task-123"])
        self.assertEqual(values[3], "0.500000")
        self.assertEqual(values[5], "")           # the empty ROI stays blank

    def test_every_row_carries_subject_and_run_id(self):
        """Concatenating tasks is only safe if each row identifies its run."""
        out, payload = self._run()
        with open(os.path.join(out, "roi_stats.csv")) as fh:
            rows = list(csv.DictReader(fh))
        self.assertTrue(all(r["subject"] == "sub-test" for r in rows))
        self.assertTrue(all(r["run_id"] == "task-123" for r in rows))
        self.assertEqual(payload["run_id"], "task-123")

    def test_shape_mismatch_is_fatal(self):
        odd = os.path.join(self.d, "odd.nii.gz")
        save(odd, np.zeros((2, 2, 2), dtype=np.float32))
        with self.assertRaises(rs.RoiStatsError):
            rs.main(["--atlas", os.path.join(self.d, "atlas.nii.gz"),
                     "--labels", os.path.join(self.d, "labels.json"),
                     "--metric", "FA=" + odd,
                     "--outdir", os.path.join(self.d, "out2")])


if __name__ == "__main__":
    unittest.main()
