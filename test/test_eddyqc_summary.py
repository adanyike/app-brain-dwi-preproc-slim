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
    "data_protocol": [[1500, 8], [3000, 8]],
    "data_unique_pes": [[0, 1, 0], [0, -1, 0]],
    "data_eddy_para": [[0, 1, 0, 0.0959], [0, -1, 0, 0.0959]],
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
        return es.signature_of(es.flags_of(qc), es.acquisition_of(qc))

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

    def test_differing_acquisition_parameters_split_the_cohort(self):
        # What a real study hit: identical eddy options, a different readout
        # time. Newer FSL compares the eddy input data and refuses the study
        # over exactly this.
        other = without(data_eddy_para=[[0, 1, 0, 0.1043], [0, -1, 0, 0.1043]])
        self.assertNotEqual(self.signature(FULL), self.signature(other))

    def test_the_fields_squad_tolerates_are_not_in_the_key_at_all(self):
        # squad_db.py compares these two with np.allclose, not !=, so they
        # cannot partition anything: a key built on them splits cohorts SQUAD
        # would have pooled. They are checked against a reference subject
        # instead -- see TestTolerance and squad_inputs.bucket.
        self.assertEqual(self.signature(FULL),
                         self.signature(without(data_unique_bvals=[1495, 3000])))
        self.assertEqual(self.signature(FULL),
                         self.signature(without(data_vox_size=[2.01, 2.0, 2.0])))

    def test_the_fields_squad_never_compares_are_not_in_the_key_either(self):
        # data_protocol and data_unique_pes are absent from squad_db.py's
        # comparison. Splitting on them refuses a study SQUAD accepts.
        self.assertEqual(self.signature(FULL),
                         self.signature(without(data_protocol=[[1500, 4], [3000, 12]])))
        self.assertEqual(self.signature(FULL),
                         self.signature(without(data_unique_pes=[[1, 0, 0], [-1, 0, 0]])))

    def test_a_dropped_volume_splits_the_cohort_too(self):
        self.assertNotEqual(self.signature(FULL),
                            self.signature(without(data_no_dw_vols=15)))

    def test_the_key_can_be_narrowed_when_a_difference_is_tolerated(self):
        narrow = ("data_no_shells", "data_no_PE_dirs")
        self.assertEqual(
            es.signature_of(es.flags_of(FULL), es.acquisition_of(FULL, narrow)),
            es.signature_of(es.flags_of(without(data_no_dw_vols=15)),
                            es.acquisition_of(without(data_no_dw_vols=15), narrow)))

    def test_the_enabled_features_are_readable_in_the_signature(self):
        signature = self.signature(FULL)
        self.assertIn("params", signature)
        self.assertIn("s2v_params", signature)
        self.assertIn("data_no_shells=2", signature)
        self.assertIn("data_no_dw_vols=16", signature)
        # ...and the fields SQUAD does not compare exactly are not in it.
        self.assertNotIn("data_unique_bvals", signature)
        self.assertNotIn("data_protocol", signature)

    def test_a_tolerant_field_can_be_forced_back_to_exact(self):
        # The escape hatch for an FSL whose comparison is stricter than the one
        # this app read: name the field and it partitions again.
        strict = es.EXACT_FIELDS + ("data_unique_bvals",)
        near = without(data_unique_bvals=[1495, 3000])
        self.assertNotEqual(
            es.signature_of(es.flags_of(FULL), es.acquisition_of(FULL), strict),
            es.signature_of(es.flags_of(near), es.acquisition_of(near), strict))

    def test_a_run_with_nothing_enabled_still_has_a_signature(self):
        bare = {name: False for name in es.SQUAD_FLAGS}
        self.assertIn("flags=none", es.signature_of(bare, es.acquisition_of({})))


class TestTolerance(unittest.TestCase):
    """The two fields squad_db.py compares with np.allclose rather than !=."""

    def gaps(self, **overrides):
        return es.tolerance_gaps(es.acquisition_of(without(**overrides)),
                                 es.acquisition_of(FULL))

    def test_a_b_value_five_apart_is_the_same_shell(self):
        # The difference that split a real two-site study in half: 5 against a
        # tolerance of 20. SQUAD would have pooled these subjects.
        self.assertEqual(self.gaps(data_unique_bvals=[1495, 3000]), [])

    def test_the_b_value_tolerance_is_twenty_and_it_is_inclusive(self):
        self.assertEqual(self.gaps(data_unique_bvals=[1480, 3000]), [])
        self.assertEqual(self.gaps(data_unique_bvals=[1530, 3000]),
                         ["data_unique_bvals"])

    def test_a_voxel_size_within_one_percent_is_the_same_resolution(self):
        self.assertEqual(self.gaps(data_vox_size=[2.01, 2.0, 2.0]), [])
        self.assertEqual(self.gaps(data_vox_size=[2.5, 2.0, 2.0]), ["data_vox_size"])

    def test_a_different_number_of_shells_is_not_a_tolerance_question(self):
        # np.allclose on arrays of different length broadcasts or raises; either
        # way it is not "close", and data_no_shells has already split them.
        self.assertEqual(self.gaps(data_unique_bvals=[1500]), ["data_unique_bvals"])

    def test_the_reference_subject_is_the_one_rtol_scales(self):
        # np.allclose(a, b) tests |a - b| <= atol + rtol * |b|, and SQUAD passes
        # the first subject in the list as b -- so the tolerance is a property of
        # the reference, not of the pair.
        self.assertTrue(es.close_enough([2.0], [200.0], atol=0.0, rtol=0.99))
        self.assertFalse(es.close_enough([200.0], [2.0], atol=0.0, rtol=0.99))

    def test_a_value_that_is_not_a_number_falls_back_to_exact_equality(self):
        self.assertTrue(es.close_enough("axial", "axial", atol=20.0, rtol=0.0))
        self.assertFalse(es.close_enough("axial", "sagittal", atol=20.0, rtol=0.0))
        self.assertIsNone(es.numbers_of([1500, "x"]))

    def test_only_the_named_fields_are_checked(self):
        far = without(data_unique_bvals=[1530, 3000], data_vox_size=[2.5, 2.0, 2.0])
        self.assertEqual(
            es.tolerance_gaps(es.acquisition_of(far), es.acquisition_of(FULL),
                              ["data_vox_size"]),
            ["data_vox_size"])


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

    def test_it_says_how_each_field_was_compared(self):
        compared = es.summarise(FULL, {"subject": "sub-01"})["compared"]
        self.assertEqual(compared["exact"], list(es.EXACT_FIELDS))
        self.assertEqual(sorted(compared["tolerant"]),
                         ["data_unique_bvals", "data_vox_size"])
        self.assertEqual(compared["tolerant"]["data_unique_bvals"]["atol"], 20.0)
        self.assertEqual(compared["disclosed"], list(es.DISCLOSED_FIELDS))

    def test_the_whole_acquisition_is_reported_not_only_the_compared_part(self):
        # The protocol SQUAD never checks is still worth publishing: the group
        # report is labelled from one subject, so a reader needs to see it.
        acquisition = es.summarise(FULL, {"subject": "sub-01"})["acquisition"]
        self.assertEqual(sorted(acquisition), sorted(es.REPORTED_FIELDS))
        self.assertEqual(acquisition["data_protocol"], [[1500, 8], [3000, 8]])

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
