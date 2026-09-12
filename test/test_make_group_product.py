#!/usr/bin/env python3
"""Unit tests for python/make_group_product.py."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import make_group_product as mgp  # noqa: E402

COHORTS = {
    "n_inputs": 4,
    "chosen": {
        "signature": "flags=params,s2v_params,field,ol,cnr|shells=2|bvals=1500,3000|pedirs=2",
        "signature_hash": "abc12345",
        "n_subjects": 3,
        "subjects": ["sub-01", "sub-02", "sub-03"],
        "protocol": {"n_shells": 2, "unique_bvals": [1500, 3000], "n_pe_directions": 2},
        "eddy_flags": {"qc_params_flag": True, "qc_s2v_params_flag": True,
                       "qc_field_flag": True, "qc_ol_flag": True,
                       "qc_cnr_flag": True, "qc_rss_flag": False},
    },
    "cohorts": [{"signature_hash": "abc12345", "n_subjects": 3, "subjects": []},
                {"signature_hash": "def67890", "n_subjects": 1, "subjects": ["sub-04"]}],
    "excluded": [{"signature_hash": "def67890", "subjects": ["sub-04"],
                  "reason": "slice-to-volume (within-volume) motion correction missing here"}],
    "rejected": [],
    "flag_meanings": {"qc_rss_flag": "eddy residuals (--residuals)"},
    "variable": {},
}

# The group_db schema eddy_squad actually writes: one row per subject, in the
# order of the folder list, with the quantities packed along the row.
GROUP_DB = {
    "data_no_subjects": 3,
    "qc_motion": [[0.4, 0.2], [3.1, 0.9], [0.5, 0.3]],
    "qc_outliers": [[1.0, 0.9, 1.1, 1.0, 1.0],
                    [8.2, 8.0, 8.4, 8.1, 8.3],
                    [1.5, 1.4, 1.6, 1.5, 1.5]],
    "qc_cnr": [[12.0, 2.5, 1.8], [9.0, 1.9, 1.2], [13.0, 2.7, 2.0]],
}


def build(cohorts=None, db=None, **kwargs):
    """Run the product builder over temporary inputs and return what it wrote."""
    with tempfile.TemporaryDirectory() as tmp:
        cohorts_path = os.path.join(tmp, "cohorts.json")
        with open(cohorts_path, "w") as fh:
            json.dump(cohorts if cohorts is not None else COHORTS, fh)
        argv = ["--cohorts", cohorts_path]
        if db is not None:
            db_path = os.path.join(tmp, "group_db.json")
            with open(db_path, "w") as fh:
                json.dump(db, fh)
            argv += ["--group-db", db_path]
        for key, value in kwargs.items():
            argv += ["--" + key.replace("_", "-"), str(value)]
        out = os.path.join(tmp, "product.json")
        assert mgp.main(argv + ["--out", out]) == 0
        with open(out) as fh:
            return json.load(fh)


def messages(product, kind=None):
    return [entry["msg"] for entry in product["brainlife"]
            if "msg" in entry and (kind is None or entry.get("type") == kind)]


def figures(product):
    return [entry for entry in product["brainlife"] if entry.get("type") == "plotly"]


class TestMessages(unittest.TestCase):
    def test_says_how_many_pooled_and_on_what_protocol(self):
        text = " ".join(messages(build(db=GROUP_DB)))
        self.assertIn("Pooled 3 subject(s) in cohort abc12345", text)
        self.assertIn("b=1500/3000", text)

    def test_names_the_excluded_subjects_and_why(self):
        warnings = " ".join(messages(build(db=GROUP_DB), "warning"))
        self.assertIn("Excluded 1 subject(s) (sub-04)", warnings)
        self.assertIn("slice-to-volume", warnings)

    def test_tells_the_user_how_to_get_the_excluded_cohort_reported(self):
        warnings = " ".join(messages(build(db=GROUP_DB), "warning"))
        self.assertIn("re-run this App on that cohort separately", warnings)

    def test_warns_about_metrics_eddy_never_produced(self):
        warnings = " ".join(messages(build(db=GROUP_DB), "warning"))
        self.assertIn("eddy residuals (--residuals)", warnings)

    def test_reports_an_unusable_input(self):
        cohorts = dict(COHORTS, rejected=[{"input": "/data/x", "reason": "no qc.json found"}])
        warnings = " ".join(messages(build(cohorts=cohorts, db=GROUP_DB), "warning"))
        self.assertIn("/data/x", warnings)
        self.assertIn("no qc.json found", warnings)

    def test_a_staging_failure_is_rendered_as_an_error(self):
        product = build(cohorts={"error": "the inputs split into 2 incompatible cohorts"})
        self.assertIn("incompatible cohorts", " ".join(messages(product, "error")))
        self.assertEqual(figures(product), [])

    def test_reports_the_grouping_variable(self):
        cohorts = dict(COHORTS, variable={"name": "age", "continuous": True,
                                          "source": "table 'participants.tsv'"})
        text = " ".join(messages(build(cohorts=cohorts, db=GROUP_DB)))
        self.assertIn("Grouped by 'age' (continuous)", text)
        self.assertIn("Scatter plots were added", text)

    def test_reports_updated_single_subject_reports(self):
        text = " ".join(messages(build(db=GROUP_DB, updated_reports=3)))
        self.assertIn("Updated 3 single-subject report(s)", text)

    def test_high_motion_is_a_warning_with_the_numbers(self):
        warnings = " ".join(messages(build(db=GROUP_DB), "warning"))
        self.assertIn("worst 3.10 mm", warnings)

    def test_a_quiet_group_is_only_informational(self):
        db = dict(GROUP_DB, qc_motion=[[0.4, 0.2], [0.5, 0.3], [0.6, 0.3]],
                  qc_outliers=[[1.0], [1.2], [1.1]])
        product = build(db=db)
        self.assertIn("worst 0.60 mm", " ".join(messages(product, "info")))
        self.assertEqual([m for m in messages(product, "warning")
                          if m.startswith("Absolute motion across the group")], [])

    def test_subjects_without_a_report_are_named(self):
        cohorts = dict(COHORTS, missing_reports=["sub-02"])
        warnings = " ".join(messages(build(cohorts=cohorts, db=GROUP_DB), "warning"))
        self.assertIn("1 pooled subject(s) published no single-subject report "
                      "(sub-02)", warnings)
        self.assertIn("still in the study-wise report", warnings)

    def test_an_arbitrary_cohort_choice_is_declared(self):
        cohorts = dict(COHORTS)
        cohorts["chosen"] = dict(COHORTS["chosen"], tied_with=["def67890"])
        warnings = " ".join(messages(build(cohorts=cohorts, db=GROUP_DB), "warning"))
        self.assertIn("no larger than 1 other cohort", warnings)
        self.assertIn("decided arbitrarily", warnings)

    def test_a_majority_cohort_is_not_called_arbitrary(self):
        warnings = " ".join(messages(build(db=GROUP_DB), "warning"))
        self.assertNotIn("decided arbitrarily", warnings)

    def test_a_subject_count_mismatch_is_flagged(self):
        warnings = " ".join(messages(build(db=dict(GROUP_DB, data_no_subjects=2)),
                                     "warning"))
        self.assertIn("may not line up", warnings)


class TestFigures(unittest.TestCase):
    def test_one_figure_per_qc_index(self):
        self.assertEqual([f["name"] for f in figures(build(db=GROUP_DB))],
                         ["Absolute motion", "Relative motion", "Outlier slices",
                          "SNR / CNR"])

    def test_the_right_element_is_taken_from_each_row(self):
        motion = figures(build(db=GROUP_DB))[0]
        # qc_motion rows are [absolute, relative]; the first figure is absolute.
        self.assertEqual(sorted(motion["data"][0]["y"]), [0.4, 0.5, 3.1])
        outliers = figures(build(db=GROUP_DB))[2]
        # qc_outliers rows start with the total, then per-shell and per-PE.
        self.assertEqual(sorted(outliers["data"][0]["y"]), [1.0, 1.5, 8.2])
        cnr = figures(build(db=GROUP_DB))[3]
        # qc_cnr rows start with the b=0 SNR.
        self.assertEqual(sorted(cnr["data"][0]["y"]), [9.0, 12.0, 13.0])

    def test_subjects_are_named_worst_first(self):
        motion = figures(build(db=GROUP_DB))[0]
        self.assertEqual(motion["data"][0]["x"], ["sub-02", "sub-03", "sub-01"])
        self.assertEqual(motion["data"][0]["y"], [3.1, 0.5, 0.4])

    def test_the_tail_is_coloured_and_the_rest_is_not(self):
        motion = figures(build(db=GROUP_DB))[0]
        self.assertEqual(motion["data"][0]["marker"]["color"],
                         [mgp.RED, mgp.BLUE, mgp.BLUE])

    def test_no_database_means_no_figures(self):
        self.assertEqual(figures(build()), [])

    def test_a_metric_eddy_never_produced_is_skipped(self):
        db = dict(GROUP_DB)
        db["qc_cnr"] = []
        self.assertNotIn("SNR / CNR", [f["name"] for f in figures(build(db=db))])

    def test_nan_values_do_not_become_bars(self):
        db = dict(GROUP_DB, qc_motion=[[0.4, 0.2], ["NaN", 0.9], [0.5, 0.3]])
        self.assertEqual(len(figures(build(db=db))[0]["data"][0]["y"]), 2)


class TestProvenance(unittest.TestCase):
    def test_records_every_cohort_so_the_rest_can_be_run(self):
        provenance = build(db=GROUP_DB)["provenance"]
        self.assertEqual(provenance["n_inputs"], 4)
        self.assertEqual(provenance["n_pooled"], 3)
        self.assertEqual(provenance["cohort_signature_hash"], "abc12345")
        self.assertEqual(len(provenance["cohorts"]), 2)
        self.assertEqual(provenance["excluded"][0]["subjects"], ["sub-04"])


if __name__ == "__main__":
    unittest.main()
