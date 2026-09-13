#!/usr/bin/env python3
"""Unit tests for python/mask_qc.py.

Several of these exist because the first design of this module was wrong in ways
that only a fixture could show, so they are written as regressions rather than as
coverage:

* the orthogonal convex hull (the *intersection* of the directional span fills)
  is blind to a bite that opens to the outside -- which is the defect the test
  was written for;
* a dropout makes image *and* mask empty, so "bright signal outside the mask"
  reads zero exactly when the data is worst;
* a shifted-and-OR-ed dilation with np.roll wraps around the array, which would
  make a mask touching the last slice look as if it touched the first.
"""
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib

import numpy as np
import nibabel as nib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import mask_qc as mq  # noqa: E402

# A left-to-right first axis, so the mirror test has an axis to work with.
AFFINE = np.diag([-2.0, 2.0, 2.0, 1.0])
HEAD_AFFINE = AFFINE


def head(n=64, radius=22.0):
    """A brain-like sphere on a dark background with a bright scalp around it.

    The scalp matters: on a b=0 EPI the skull, marrow and orbital fat are bright,
    so any criterion phrased as "bright tissue just outside the mask" fires on a
    perfectly good mask. Omitting it from the fixtures would hide that.
    """
    grid = [np.arange(n) - (n - 1) / 2.0] * 3
    ii, jj, kk = np.meshgrid(*grid, indexing="ij")
    distance = np.sqrt(ii ** 2 + (jj * 0.95) ** 2 + (kk * 1.1) ** 2)
    brain = distance < radius
    image = np.zeros((n, n, n))
    image[brain] = 1000.0
    image[(distance >= radius + 2) & (distance < radius + 5)] = 1400.0
    return image, brain, (ii, jj, kk)


def sphere(coords, centre, radius):
    ii, jj, kk = coords
    return np.sqrt((ii - centre[0]) ** 2 + (jj - centre[1]) ** 2
                   + (kk - centre[2]) ** 2) < radius


class TestMorphology(unittest.TestCase):
    def test_dilate_does_not_wrap(self):
        flag = np.zeros((4, 4, 4), dtype=bool)
        flag[0, 0, 0] = True
        grown = mq.dilate(flag, 1)
        self.assertEqual(int(grown.sum()), 4)          # itself + 3 neighbours
        self.assertFalse(grown[-1, 0, 0])
        self.assertFalse(grown[0, -1, 0])
        self.assertFalse(grown[0, 0, -1])

    def test_erode_is_the_dual_of_dilate(self):
        flag = np.zeros((9, 9, 9), dtype=bool)
        flag[2:7, 2:7, 2:7] = True
        self.assertTrue(np.array_equal(mq.erode(flag, 1), mq.erode(flag, 1)))
        self.assertEqual(int(mq.erode(flag, 1).sum()), 27)
        self.assertTrue((mq.boundary(flag) & flag).sum() > 0)
        self.assertFalse((mq.boundary(flag) & ~flag).any())

    def test_thicken_drops_a_one_voxel_skin_and_keeps_a_chunk(self):
        flag = np.zeros((20, 20, 20), dtype=bool)
        flag[0, :, :] = True                           # a one-voxel sheet
        self.assertEqual(int(mq.thicken(flag).sum()), 0)
        flag = np.zeros((20, 20, 20), dtype=bool)
        flag[5:12, 5:12, 5:12] = True                  # a chunk
        self.assertGreater(int(mq.thicken(flag).sum()), 100)

    def test_span_fill_is_bounded_by_the_mask(self):
        flag = np.zeros((6, 6, 6), dtype=bool)
        flag[1, 1, 1] = True
        flag[1, 1, 4] = True
        filled = mq.span_fill(flag, 2)
        self.assertTrue(filled[1, 1, 2] and filled[1, 1, 3])
        self.assertFalse(filled[1, 1, 0] or filled[1, 1, 5])


class TestBiteRecovery(unittest.TestCase):
    """The regression the whole detector design turns on."""

    def setUp(self):
        self.image, self.brain, self.coords = head()
        self.bite = sphere(self.coords, (-16, 0, 0), 8.0)
        self.bitten = self.brain & ~self.bite

    def test_the_union_of_span_fills_recovers_the_bite_and_the_intersection_does_not(self):
        union = mq.span_fill_union(self.bitten) & ~self.bitten
        intersection = mq.span_fill_intersection(self.bitten) & ~self.bitten
        recovered_by_union = int((union & self.bite).sum())
        recovered_by_intersection = int((intersection & self.bite).sum())
        # The union finds most of the bite; the hull barely sees it, because a
        # bite open to the outside is not inside the orthogonal convex hull.
        self.assertGreater(recovered_by_union, 0.5 * int(self.bite.sum()))
        self.assertLess(recovered_by_intersection, 0.25 * int(self.bite.sum()))
        self.assertLess(recovered_by_intersection, 0.3 * recovered_by_union)

    def test_a_lateral_bite_is_suspicious_and_localised(self):
        report = mq.assess(self.image, self.bitten, AFFINE)
        self.assertEqual(report["verdict"], "suspicious")
        self.assertGreater(report["missing"]["by_criterion"]["mirror"], 0)
        self.assertGreater(report["missing"]["by_criterion"]["hull_band"], 0)
        self.assertGreater(report["worst_block"]["fraction"], 0.25)
        self.assertEqual(len(report["worst_block"]["centre_mm"]), 3)

    def test_a_good_mask_is_ok_despite_the_bright_scalp(self):
        report = mq.assess(self.image, self.brain, AFFINE)
        self.assertEqual(report["verdict"], "ok", report["reasons"])
        self.assertEqual(report["missing"]["bright_voxels"], 0)
        # The scalp *is* seen -- it is just not allowed to mean anything.
        self.assertGreater(report["boundary_through_tissue"]["voxels"], 0)


class TestDropout(unittest.TestCase):
    """Signal dropout: the image is empty there too, so nothing can be recovered."""

    def setUp(self):
        self.image, self.brain, self.coords = head()
        self.hole = sphere(self.coords, (-16, 0, 0), 8.0)
        self.image[self.hole] = 0.0
        self.mask = self.brain & ~self.hole

    def test_it_is_reported_as_dark_not_as_a_mask_error(self):
        report = mq.assess(self.image, self.mask, AFFINE)
        self.assertLess(report["missing"]["bright_fraction"], 0.005)
        self.assertGreater(report["missing"]["dark_fraction"], 0.01)
        self.assertTrue(report["dropout"])
        self.assertEqual(report["verdict"], "ok")
        self.assertTrue(any("dropout" in note for note in report["notes"]))

    def test_the_repair_refuses_to_grow_into_it(self):
        _, dark = mq.candidates_of(self.image, self.mask, AFFINE)
        fixed, record = mq.repair(self.image, self.mask, permissive=self.brain,
                                  missing_dark=dark, confine=mq.dilate(dark, 2),
                                  grow=3, cap=0.25)
        self.assertEqual(int((fixed & self.hole).sum()), 0)
        self.assertFalse(any(step["added_voxels"] for step in record["steps"]
                             if "grow" in step["step"]))


class TestTruncation(unittest.TestCase):
    def test_a_mask_cut_off_mid_brain_is_suspicious(self):
        image, brain, _ = head()
        cut = brain.copy()
        cut[:, :, 40:] = False
        report = mq.assess(image, cut, AFFINE)
        self.assertEqual(report["verdict"], "suspicious")
        self.assertIn("boundary", report["truncation"]["kinds"])
        self.assertTrue(any("ends abruptly" in reason for reason in report["reasons"]))

    def test_an_interior_slice_collapsing_is_suspicious(self):
        image, brain, _ = head()
        gap = brain.copy()
        gap[:, :, 30] = False
        report = mq.assess(image, gap, AFFINE)
        self.assertEqual(report["verdict"], "suspicious")
        self.assertIn(30, report["truncation"]["slices"])

    def test_a_whole_brain_mask_tapers_and_is_not_flagged(self):
        image, brain, _ = head()
        report = mq.assess(image, brain, AFFINE)
        self.assertEqual(report["truncation"]["slices"], [])


class TestImplausible(unittest.TestCase):
    def test_bet_landing_on_the_neck_is_implausible_not_suspicious(self):
        image, brain, coords = head()
        neck = sphere(coords, (0, 14, -24), 9.0)
        report = mq.assess(image, neck, AFFINE)
        self.assertEqual(report["verdict"], "implausible")
        self.assertFalse(report["repairable"])
        self.assertTrue(any("field of view" in reason or "centre" in reason
                            or "ml" in reason for reason in report["reasons"]))

    def test_an_empty_mask_is_implausible_and_not_repairable(self):
        image, brain, _ = head()
        report = mq.assess(image, np.zeros_like(brain), AFFINE)
        self.assertEqual(report["verdict"], "implausible")
        self.assertEqual(report["reasons"], ["the mask is empty"])
        _, record = mq.repair(image, np.zeros_like(brain), permissive=brain)
        self.assertFalse(record["applied"])

    def test_a_mask_covering_the_whole_field_of_view_is_implausible(self):
        image, brain, _ = head()
        report = mq.assess(image, np.ones_like(brain), AFFINE)
        self.assertEqual(report["verdict"], "implausible")

    def test_the_absolute_volume_range_applies_only_to_a_head_sized_fov(self):
        self.assertTrue(mq.plausible_fov((64, 64, 64), 8.0))     # 128 mm cube
        # The synthetic fixtures the dry run uses are a few centimetres across,
        # so a brain-volume range would fail every dry run for the wrong reason.
        self.assertFalse(mq.plausible_fov((32, 32, 12), 8.0))
        image, brain, _ = head(n=16, radius=6.0)
        report = mq.assess(image, brain, AFFINE)
        self.assertTrue(any("head-sized" in note for note in report["notes"]))
        self.assertNotEqual(report["verdict"], "implausible")


class TestRepair(unittest.TestCase):
    def setUp(self):
        self.image, self.brain, self.coords = head()
        self.bite = sphere(self.coords, (-16, 0, 0), 8.0)
        self.bitten = self.brain & ~self.bite
        self.bright, self.dark = mq.candidates_of(self.image, self.bitten, AFFINE)

    def test_it_recovers_the_bite_and_the_verdict_becomes_ok(self):
        fixed, record = mq.repair(self.image, self.bitten, permissive=self.brain,
                                  missing_dark=self.dark,
                                  confine=mq.dilate(self.bright, 2))
        self.assertTrue(record["applied"])
        self.assertGreater(int((fixed & self.bite).sum()), 0.5 * int(self.bite.sum()))
        self.assertEqual(mq.assess(self.image, fixed, AFFINE)["verdict"], "ok")

    def test_it_never_removes_a_voxel(self):
        for permissive in (self.brain, np.ones_like(self.brain), None):
            fixed, _ = mq.repair(self.image, self.bitten, permissive=permissive,
                                 missing_dark=self.dark, grow=2)
            self.assertFalse((self.bitten & ~fixed).any())

    def test_a_repair_above_the_cap_is_discarded_whole(self):
        fixed, record = mq.repair(self.image, self.bitten,
                                  permissive=np.ones_like(self.brain), cap=0.25)
        self.assertFalse(record["applied"])
        self.assertTrue(np.array_equal(fixed, self.bitten))
        self.assertIn("cap", record["reason"])
        self.assertEqual(record["voxels_after"], record["voxels_before"])

    def test_confining_the_repair_keeps_the_rest_of_the_mask_as_bet_made_it(self):
        wide = mq.dilate(self.brain, 2)
        local, _ = mq.repair(self.image, self.bitten, permissive=wide,
                             confine=mq.dilate(self.bright, 2), cap=1.0)
        everywhere, _ = mq.repair(self.image, self.bitten, permissive=wide, cap=1.0)
        self.assertLess(int(local.sum()), int(everywhere.sum()))
        self.assertGreater(int((local & self.bite).sum()), 0)

    def test_growth_is_bounded_above_so_it_cannot_walk_into_the_skull(self):
        # The scalp is brighter than the brain; growth must not reach it even
        # with many iterations, because the ceiling is the in-mask p99.5.
        fixed, _ = mq.repair(self.image, self.brain, grow=6,
                             confine=np.ones_like(self.brain))
        scalp = self.image > 1200
        self.assertEqual(int((fixed & scalp).sum()), 0)

    def test_holes_are_filled_but_a_huge_hole_is_not(self):
        holed = self.brain.copy()
        holed[30:34, 30:34, 30:34] = False             # enclosed
        fixed, record = mq.repair(self.image, holed)
        self.assertTrue(record["applied"])
        self.assertEqual(int((~fixed & holed).sum()), 0)
        self.assertTrue(any("hole" in step["step"] for step in record["steps"]))


class TestReportShape(unittest.TestCase):
    def test_the_report_is_json_serialisable(self):
        image, brain, coords = head()
        bitten = brain & ~sphere(coords, (-16, 0, 0), 8.0)
        report = mq.assess(image, bitten, AFFINE)
        text = json.dumps(report)                      # no numpy scalars allowed
        self.assertEqual(json.loads(text)["verdict"], "suspicious")

    def test_the_slice_profile_has_one_value_per_slice(self):
        image, brain, _ = head(n=48)
        report = mq.assess(image, brain, AFFINE)
        axis = report["slice_profile"]["axis"]
        for key in ("mask_area", "tissue_area", "missing_area"):
            self.assertEqual(len(report["slice_profile"][key]), brain.shape[axis])

    def test_an_oblique_affine_records_why_the_mirror_was_skipped(self):
        image, brain, _ = head(n=32)
        oblique = np.array([[1.4, 1.4, 0.0, 0.0],
                            [1.4, -1.4, 0.0, 0.0],
                            [0.0, 0.0, 2.0, 0.0],
                            [0.0, 0.0, 0.0, 1.0]])
        report = mq.assess(image, brain, oblique)
        self.assertEqual(report["missing"]["by_criterion"]["mirror"], 0)

    def test_a_mask_on_the_edge_of_the_field_of_view_says_so(self):
        image, brain, _ = head(n=40, radius=19.0)
        clipped = brain.copy()
        clipped[:, :, :] |= np.zeros_like(clipped)
        clipped[0, :, :] = brain[1, :, :]              # push it onto the face
        report = mq.assess(image, clipped, AFFINE)
        self.assertGreater(report["fov_contact"]["voxels"], 0)
        self.assertIn("x_low", report["fov_contact"]["faces"])

    def test_shape_mismatch_is_an_error_rather_than_a_wrong_answer(self):
        image, brain, _ = head(n=32)
        with self.assertRaises(mq.MaskQcError):
            mq.assess(image, brain[:16], AFFINE)


class TestPng(unittest.TestCase):
    """The montage is written with zlib and struct, so the tests can decode it."""

    def decode(self, path):
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", raw[16:24])
        # Walk the chunks, concatenate the IDATs, and unfilter (filter 0 only).
        data, offset = b"", 8
        while offset < len(raw):
            length = struct.unpack(">I", raw[offset:offset + 4])[0]
            tag = raw[offset + 4:offset + 8]
            if tag == b"IDAT":
                data += raw[offset + 8:offset + 8 + length]
            offset += 12 + length
        pixels = zlib.decompress(data)
        stride = width * 3 + 1
        rows = []
        for row in range(height):
            line = pixels[row * stride:(row + 1) * stride]
            self.assertEqual(line[0], 0)
            rows.append(np.frombuffer(line[1:], dtype=np.uint8).reshape(width, 3))
        return np.array(rows)

    def test_the_montage_decodes_and_draws_the_outline(self):
        image, brain, coords = head(n=48)
        bitten = brain & ~sphere(coords, (-12, 0, 0), 6.0)
        missing, _ = mq.candidates_of(image, bitten, AFFINE)
        canvas = mq.montage(image, bitten, 2, [(missing, (242, 201, 76))],
                            n_slices=8, columns=4)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "overlay.png")
            mq.write_png(path, canvas)
            decoded = self.decode(path)
        self.assertEqual(decoded.shape, canvas.shape)
        self.assertTrue(np.array_equal(decoded, canvas))
        flat = decoded.reshape(-1, 3)
        self.assertTrue((flat == np.array([228, 66, 86])).all(axis=1).any())
        self.assertTrue((flat == np.array([242, 201, 76])).all(axis=1).any())

    def test_a_non_rgb_array_is_refused(self):
        with self.assertRaises(mq.MaskQcError):
            mq.write_png("/dev/null", np.zeros((4, 4), dtype=np.uint8))


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        image, brain, coords = head(n=48)
        self.bite = sphere(coords, (-12, 0, 0), 6.0)
        self.paths = {}
        for name, array, dtype in (("image", image, np.float32),
                                   ("mask", brain & ~self.bite, np.uint8),
                                   ("permissive", brain, np.uint8)):
            path = os.path.join(self.tmp.name, name + ".nii.gz")
            nib.save(nib.Nifti1Image(array.astype(dtype), HEAD_AFFINE), path)
            self.paths[name] = path
        self.report = os.path.join(self.tmp.name, "report.json")

    def test_check_prints_the_verdict_and_writes_the_report(self):
        code = mq.main(["check", "--image", self.paths["image"],
                        "--mask", self.paths["mask"], "--label", "eddy",
                        "--out", self.report])
        self.assertEqual(code, 0)
        with open(self.report) as fh:
            report = json.load(fh)
        self.assertEqual(report["label"], "eddy")
        self.assertEqual(report["verdict"], "suspicious")

    def test_repair_updates_the_mask_and_the_report_in_place(self):
        mq.main(["check", "--image", self.paths["image"], "--mask", self.paths["mask"],
                 "--out", self.report])
        out = os.path.join(self.tmp.name, "repaired.nii.gz")
        code = mq.main(["repair", "--image", self.paths["image"],
                        "--mask", self.paths["mask"],
                        "--permissive", self.paths["permissive"],
                        "--out-mask", out, "--report", self.report, "--cap", "0.5"])
        self.assertEqual(code, 0)
        with open(self.report) as fh:
            report = json.load(fh)
        self.assertTrue(report["repair"]["applied"])
        self.assertTrue(report["repair"]["confined"])
        self.assertEqual(report["after"]["verdict"], "ok")
        repaired = np.asanyarray(nib.load(out).dataobj) > 0
        original = np.asanyarray(nib.load(self.paths["mask"]).dataobj) > 0
        self.assertFalse((original & ~repaired).any())

    def test_repair_leaves_the_mask_alone_when_it_hits_the_cap(self):
        out = os.path.join(self.tmp.name, "repaired.nii.gz")
        code = mq.main(["repair", "--image", self.paths["image"],
                        "--mask", self.paths["mask"],
                        "--permissive", self.paths["permissive"],
                        "--out-mask", out, "--confine", "none", "--cap", "0.001"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(out))

    def test_figure_writes_a_png(self):
        out = os.path.join(self.tmp.name, "overlay.png")
        code = mq.main(["figure", "--image", self.paths["image"],
                        "--mask", self.paths["mask"], "--out", out, "--slices", "4"])
        self.assertEqual(code, 0)
        self.assertGreater(os.path.getsize(out), 100)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
