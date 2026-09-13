#!/usr/bin/env python3
"""Unit tests for python/make_product.py."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import make_product as mp  # noqa: E402

PREP = {
    "n_volumes": 117, "n_volumes_forward": 99, "n_volumes_reverse": 18,
    "n_b0_total": 9, "n_b0_for_topup": 4, "approx_shells": [0, 1500],
    "phase_encoding": [{"series": 0, "vector": [0, -1, 0], "total_readout_time": 0.03}],
}
ROI = {
    "n_rois": 2, "metrics": ["FA", "MD"], "empty_rois": [],
    "stats": [
        {"metric": "FA", "roi_index": 1, "roi_abbreviation": "MCP",
         "roi_name": "Middle cerebellar peduncle", "mean": 0.51, "n_voxels": 900},
        {"metric": "FA", "roi_index": 2, "roi_abbreviation": "PCT",
         "roi_name": "Pontine crossing tract", "mean": 0.44, "n_voxels": 120},
        {"metric": "MD", "roi_index": 1, "roi_abbreviation": "MCP",
         "roi_name": "Middle cerebellar peduncle", "mean": 0.0007, "n_voxels": 900},
    ],
}


MASK_OK = {
    "label": "eddy", "verdict": "ok", "volume_ml": 1402.0, "n_voxels": 175250,
    "missing": {"bright_fraction": 0.001, "bright_voxels": 175},
    "reasons": [], "notes": [], "dropout": False,
    "slice_profile": {"axis": 2, "mask_area": [0, 10, 40, 38, 8],
                      "missing_area": [0, 0, 0, 0, 0],
                      "tissue_area": [2, 14, 44, 42, 10]},
}
MASK_REPAIRED = {
    "label": "eddy", "verdict": "suspicious", "volume_ml": 1302.0, "n_voxels": 162750,
    "missing": {"bright_fraction": 0.032, "bright_voxels": 5208},
    "reasons": ["3.2% of the mask volume again is brain-bright signal just outside it"],
    "worst_block": {"fraction": 0.64, "centre_mm": [-44.0, -18.0, 12.0]},
    "notes": [], "dropout": False,
    "repair": {"applied": True, "added_voxels": 4100, "added_fraction": 0.025,
               "steps": [{"step": "union with a permissive bet, where there is signal",
                          "added_voxels": 4100}]},
    "slice_profile": {"axis": 2, "mask_area": [0, 10, 30, 38, 8],
                      "missing_area": [0, 0, 12, 0, 0],
                      "tissue_area": [2, 14, 44, 42, 10]},
}


class TestMakeProduct(unittest.TestCase):
    def _build(self, prep=None, roi=None, rms=None, **flags):
        tmp = tempfile.mkdtemp()
        prep_path = os.path.join(tmp, "prep.json")
        with open(prep_path, "w") as fh:
            json.dump(prep if prep is not None else PREP, fh)
        argv = ["--prep", prep_path, "--out", os.path.join(tmp, "product.json")]
        if roi is not None:
            roi_path = os.path.join(tmp, "roi.json")
            with open(roi_path, "w") as fh:
                json.dump(roi, fh)
            argv += ["--roi-stats", roi_path]
        if rms is not None:
            rms_path = os.path.join(tmp, "rms")
            with open(rms_path, "w") as fh:
                fh.write("\n".join("%f %f" % (v, v / 2) for v in rms))
            argv += ["--eddy-movement-rms", rms_path]
        for label, report in (flags.pop("mask_qc", {}) or {}).items():
            path = os.path.join(tmp, "mask_qc_%s.json" % label)
            with open(path, "w") as fh:
                json.dump(report, fh)
            argv += ["--mask-qc", "%s=%s" % (label, path)]
        for key, value in flags.items():
            argv += ["--" + key.replace("_", "-"), str(value)]
        mp.main(argv)
        with open(os.path.join(tmp, "product.json")) as fh:
            return json.load(fh)

    def test_happy_path_has_no_warnings(self):
        product = self._build(roi=ROI, rms=[0.2, 0.3, 0.25],
                              topup_applied="true", slice_to_volume="true",
                              eddy_binary="eddy_cuda10.2", shell="1500")
        entries = product["brainlife"]
        self.assertEqual([e for e in entries if e.get("type") == "warning"], [])
        self.assertTrue(product["provenance"]["topup_applied"])
        self.assertTrue(product["provenance"]["slice_to_volume_correction"])

    def test_missing_reverse_pe_warns(self):
        product = self._build(roi=ROI, topup_applied="false", slice_to_volume="true")
        warnings = [e["msg"] for e in product["brainlife"] if e.get("type") == "warning"]
        self.assertTrue(any("topup was skipped" in m for m in warnings))

    def test_no_slice_to_volume_warns(self):
        product = self._build(roi=ROI, topup_applied="true", slice_to_volume="false",
                              eddy_binary="eddy_openmp")
        warnings = [e["msg"] for e in product["brainlife"] if e.get("type") == "warning"]
        self.assertTrue(any("Slice-to-volume correction was NOT applied" in m
                            for m in warnings))

    def test_high_motion_warns(self):
        product = self._build(roi=ROI, rms=[0.2, 5.5], topup_applied="true",
                              slice_to_volume="true", motion_warn_mm=2.0)
        warnings = [e["msg"] for e in product["brainlife"] if e.get("type") == "warning"]
        self.assertTrue(any("peak 5.50 mm" in m for m in warnings))

    def test_empty_rois_warn(self):
        roi = dict(ROI, empty_rois=["TAP_R", "TAP_L"])
        product = self._build(roi=roi, topup_applied="true", slice_to_volume="true")
        warnings = [e["msg"] for e in product["brainlife"] if e.get("type") == "warning"]
        self.assertTrue(any("2 atlas ROI(s)" in m for m in warnings))

    def test_fa_figure_is_sorted_and_labelled(self):
        product = self._build(roi=ROI, topup_applied="true", slice_to_volume="true")
        figures = [e for e in product["brainlife"] if e.get("type") == "plotly"]
        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0]["data"][0]["x"], ["1 MCP", "2 PCT"])
        self.assertEqual(figures[0]["data"][0]["y"], [0.51, 0.44])

    def test_motion_figure_appears_only_with_rms(self):
        without = self._build(roi=ROI, topup_applied="true", slice_to_volume="true")
        with_rms = self._build(roi=ROI, rms=[0.1, 0.2], topup_applied="true",
                               slice_to_volume="true")
        self.assertEqual(
            len([e for e in without["brainlife"] if e.get("type") == "plotly"]), 1)
        self.assertEqual(
            len([e for e in with_rms["brainlife"] if e.get("type") == "plotly"]), 2)

    def test_a_good_mask_is_reported_without_a_warning(self):
        product = self._build(roi=ROI, topup_applied="true", slice_to_volume="true",
                              mask_qc={"eddy": MASK_OK})
        messages = [e["msg"] for e in product["brainlife"] if "msg" in e]
        warnings = [e["msg"] for e in product["brainlife"] if e.get("type") == "warning"]
        self.assertTrue(any("Brain mask (eddy) covers the brain" in m for m in messages))
        self.assertEqual(warnings, [])
        self.assertEqual(product["provenance"]["brain_mask"]["eddy"]["verdict"], "ok")

    def test_a_repaired_mask_is_reported_as_a_change_in_results(self):
        product = self._build(roi=ROI, topup_applied="true", slice_to_volume="true",
                              mask_qc={"eddy": MASK_REPAIRED})
        warnings = [e["msg"] for e in product["brainlife"] if e.get("type") == "warning"]
        self.assertTrue(any("missing brain" in m for m in warnings))
        self.assertTrue(any("was repaired" in m and "eddy was given" in m
                            for m in warnings))
        self.assertTrue(
            product["provenance"]["brain_mask"]["eddy"]["repair"]["applied"])

    def test_a_mask_left_alone_at_the_cap_says_why(self):
        report = dict(MASK_REPAIRED,
                      repair={"applied": False, "reason": "above the 10% cap",
                              "steps": []})
        product = self._build(roi=ROI, topup_applied="true", slice_to_volume="true",
                              mask_qc={"final": report})
        warnings = [e["msg"] for e in product["brainlife"] if e.get("type") == "warning"]
        self.assertTrue(any("left as bet made it" in m and "cap" in m
                            for m in warnings))

    def test_an_implausible_mask_says_it_was_not_repaired(self):
        report = dict(MASK_OK, verdict="implausible",
                      reasons=["the mask is 90 ml, below the 250 ml a brain can be"])
        product = self._build(roi=ROI, topup_applied="true", slice_to_volume="true",
                              mask_qc={"eddy": report})
        warnings = [e["msg"] for e in product["brainlife"] if e.get("type") == "warning"]
        self.assertTrue(any("is not a brain" in m and "NOT repaired" in m
                            for m in warnings))

    def test_a_dropout_is_reported_as_the_data_not_the_mask(self):
        report = dict(MASK_OK, dropout=True,
                      notes=["3.0% of the mask volume again is *dark* just outside it"])
        product = self._build(roi=ROI, topup_applied="true", slice_to_volume="true",
                              mask_qc={"eddy": report})
        warnings = [e["msg"] for e in product["brainlife"] if e.get("type") == "warning"]
        self.assertTrue(any("dark just outside it" in m.replace("*", "")
                            for m in warnings))

    def test_the_coverage_figure_has_a_trace_per_mask(self):
        product = self._build(roi=ROI, topup_applied="true", slice_to_volume="true",
                              mask_qc={"eddy": MASK_REPAIRED, "final": MASK_OK})
        figures = [e for e in product["brainlife"] if e.get("type") == "plotly"]
        coverage = [f for f in figures if f["name"] == "Brain mask coverage"]
        self.assertEqual(len(coverage), 1)
        names = [trace["name"] for trace in coverage[0]["data"]]
        self.assertIn("eddy mask", names)
        self.assertIn("eddy missing", names)          # it has a missing profile
        self.assertIn("final mask", names)
        self.assertNotIn("final missing", names)      # and it does not

    def test_a_malformed_mask_qc_argument_is_ignored_rather_than_fatal(self):
        tmp = tempfile.mkdtemp()
        prep_path = os.path.join(tmp, "prep.json")
        with open(prep_path, "w") as fh:
            json.dump(PREP, fh)
        out = os.path.join(tmp, "product.json")
        self.assertEqual(mp.main(["--prep", prep_path, "--out", out,
                                  "--mask-qc", "no-equals-sign"]), 0)
        with open(out) as fh:
            product = json.load(fh)
        self.assertTrue(product["brainlife"])
        self.assertEqual(product["provenance"]["brain_mask"], {})

    def test_survives_without_roi_stats(self):
        product = self._build(topup_applied="true", slice_to_volume="true")
        self.assertTrue(product["brainlife"])
        self.assertEqual(
            [e for e in product["brainlife"] if e.get("type") == "plotly"], [])


if __name__ == "__main__":
    unittest.main()
