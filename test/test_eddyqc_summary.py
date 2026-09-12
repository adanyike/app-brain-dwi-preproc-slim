#!/usr/bin/env python3
"""Unit tests for python/eddyqc_summary.py."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import eddyqc_summary as es  # noqa: E402

# A QUAD database from a GPU run with topup: every feature available.
FULL = {
    "data_no_dw_vols": 16, "data_no_b0_vols": 4, "data_no_PE_dirs": 2,
    "data_no_shells": 2, "data_unique_bvals": [1500, 3000],
    "data_vox_size": [2.0, 2.0, 2.0],
    "qc_mot_abs": 0.42, "qc_mot_rel": 0.19,
    "qc_params_flag": True, "qc_s2v_params_flag": True, "qc_field_flag": True,
    "qc_ol_flag": True, "qc_cnr_flag": True, "qc_rss_flag": False,
    "qc_outliers_tot": 1.25, "qc_vox_displ_std": 0.8,
    "qc_cnr_avg": [12.0, 2.5, 1.8], "qc_cnr_std": [1.0, 0.3, 0.2],
}


def without(**overrides):
    qc = dict(FULL)
    qc.update(overrides)
    return qc


class TestFlags(unittest.TestCase):
    def test_all_six_squad_flags_are_read(self):
        flags = es.flags_of(FULL)
        self.assertEqual(set(flags), set(es.SQUAD_FLAGS))
        self.assertTrue(flags["qc_s2v_params_flag"])
        self.assertFalse(flags["qc_rss_flag"])

    def test_a_missing_flag_is_false_not_an_error(self):
        self.assertFalse(es.flags_of({})["qc_cnr_flag"])

    def test_tolerates_the_other_spellings_of_true(self):
        for value in (1, "true", "True", "yes"):
            self.assertTrue(es.as_bool(value), value)
        for value in (0, "", "false", None, "no"):
            self.assertFalse(es.as_bool(value), value)


class TestSignature(unittest.TestCase):
    def signature(self, qc):
        return es.signature_of(es.flags_of(qc), es.protocol_of(qc))

    def test_identical_runs_share_a_signature(self):
        self.assertEqual(self.signature(FULL), self.signature(dict(FULL)))

    def test_the_cpu_fallback_splits_the_cohort(self):
        # The trap this whole mechanism exists for: the same subject processed
        # on a node with no GPU has no slice-to-volume metrics, and eddy_squad
        # refuses to pool the two.
        self.assertNotEqual(self.signature(FULL),
                            self.signature(without(qc_s2v_params_flag=False)))

    def test_a_subject_without_topup_splits_the_cohort(self):
        self.assertNotEqual(self.signature(FULL),
                            self.signature(without(qc_field_flag=False)))

    def test_a_different_shell_count_splits_the_cohort(self):
        self.assertNotEqual(self.signature(FULL),
                            self.signature(without(data_no_shells=1,
                                                   data_unique_bvals=[1500])))

    def test_a_dropped_volume_does_not_split_the_cohort(self):
        # SQUAD pools these correctly, so excluding the subject would throw
        # away data for nothing.
        self.assertEqual(self.signature(FULL),
                         self.signature(without(data_no_dw_vols=15)))

    def test_b_values_are_rounded_to_the_nearest_50(self):
        self.assertEqual(self.signature(FULL),
                         self.signature(without(data_unique_bvals=[1495, 3005])))

    def test_the_enabled_features_are_readable_in_the_signature(self):
        signature = self.signature(FULL)
        self.assertIn("params", signature)
        self.assertIn("s2v_params", signature)
        self.assertIn("shells=2", signature)
        self.assertIn("bvals=1500,3000", signature)

    def test_a_run_with_nothing_enabled_still_has_a_signature(self):
        bare = {name: False for name in es.SQUAD_FLAGS}
        self.assertIn("flags=none", es.signature_of(bare, es.protocol_of({})))


class TestSummary(unittest.TestCase):
    def test_carries_the_labels_and_the_headline_metrics(self):
        summary = es.summarise(FULL, {"subject": "sub-01", "session": "ses-02",
                                      "run_id": "task-9"})
        self.assertEqual(summary["subject"], "sub-01")
        self.assertEqual(summary["session"], "ses-02")
        self.assertTrue(summary["squad_ready"])
        self.assertAlmostEqual(summary["metrics"]["motion_abs_mm"], 0.42)
        self.assertAlmostEqual(summary["metrics"]["outliers_pct"], 1.25)
        self.assertEqual(len(summary["signature_hash"]), 8)

    def test_the_hash_follows_the_signature(self):
        a = es.summarise(FULL, {"subject": "a"})
        b = es.summarise(without(qc_cnr_flag=False), {"subject": "a"})
        self.assertNotEqual(a["signature_hash"], b["signature_hash"])

    def test_nan_metrics_are_dropped_rather_than_published(self):
        summary = es.summarise(without(qc_mot_abs=float("nan")), {"subject": "a"})
        self.assertIsNone(summary["metrics"]["motion_abs_mm"])

    def test_a_missing_database_is_reported_not_fatal(self):
        summary = es.summarise({}, {"subject": "sub-01"})
        self.assertFalse(summary["squad_ready"])
        self.assertIn("cannot take part", summary["note"])


class TestCommandLine(unittest.TestCase):
    def test_writes_a_summary_beside_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            qc_json = os.path.join(tmp, "qc.json")
            out = os.path.join(tmp, "squad_ready.json")
            with open(qc_json, "w") as fh:
                json.dump(FULL, fh)
            self.assertEqual(es.main(["--qc-json", qc_json, "--subject", "sub-07",
                                      "--run-id", "t1", "--out", out]), 0)
            with open(out) as fh:
                summary = json.load(fh)
            self.assertEqual(summary["subject"], "sub-07")
            self.assertEqual(summary["run_id"], "t1")
            self.assertIn("shells=2", summary["signature"])

    def test_a_missing_database_still_writes_a_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "squad_ready.json")
            self.assertEqual(es.main(["--qc-json", os.path.join(tmp, "absent.json"),
                                      "--out", out]), 0)
            with open(out) as fh:
                self.assertFalse(json.load(fh)["squad_ready"])


if __name__ == "__main__":
    unittest.main()
