#!/usr/bin/env python3
"""Unit tests for python/squad_inputs.py -- the group staging logic."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import squad_inputs as si  # noqa: E402

BASE = {
    "data_no_dw_vols": 16, "data_no_b0_vols": 4, "data_no_PE_dirs": 2,
    "data_no_shells": 2, "data_unique_bvals": [1500, 3000],
    "data_vox_size": [2.0, 2.0, 2.0],
    "data_protocol": [[1500, 8], [3000, 8]],
    "data_unique_pes": [[0, 1, 0], [0, -1, 0]],
    # Flat, as eddy_quad writes it: one [x, y, z, readout] row after another.
    "data_eddy_para": [0.0, 1.0, 0.0, 0.0959, 0.0, -1.0, 0.0, 0.0959],
    "qc_mot_abs": 0.4, "qc_mot_rel": 0.2, "qc_outliers_tot": 1.0,
    "qc_params_flag": True, "qc_s2v_params_flag": True, "qc_field_flag": True,
    "qc_ol_flag": True, "qc_cnr_flag": True, "qc_rss_flag": False,
    "qc_cnr_avg": [12.0, 2.5, 1.8],
}


def qc(**overrides):
    database = dict(BASE)
    database.update(overrides)
    return database


class StagingCase(unittest.TestCase):
    """Builds a directory of fake eddy QC datasets, the way brainlife stages them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.work = os.path.join(self.root, "work")

    def dataset(self, name, database, summary=None, pdf=True):
        """One staged input dataset: a folder holding qc.json (and friends)."""
        folder = os.path.join(self.root, "inputs", name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "qc.json"), "w") as fh:
            json.dump(database, fh)
        if summary is not None:
            with open(os.path.join(folder, "squad_ready.json"), "w") as fh:
                json.dump(summary, fh)
        if pdf:
            with open(os.path.join(folder, "qc.pdf"), "w") as fh:
                fh.write("%PDF-1.4\n")
        return folder

    def config(self, folders, **extra):
        config = {"eddyqc": folders}
        config.update(extra)
        path = os.path.join(self.root, "config.json")
        with open(path, "w") as fh:
            json.dump(config, fh)
        return path

    def run_staging(self, config_path, expect=0, min_subjects=2):
        out = os.path.join(self.root, "cohorts.json")
        status = si.main(["--config", config_path, "--work-dir", self.work,
                          "--out", out, "--min-subjects", str(min_subjects)])
        self.assertEqual(status, expect)
        with open(out) as fh:
            return json.load(fh)


class TestInputResolution(StagingCase):
    def test_a_folder_holding_qc_json(self):
        folders = [self.dataset("a", qc()), self.dataset("b", qc())]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["n_subjects"], 2)

    def test_a_dataset_that_mapped_qc_json_itself(self):
        paths = [os.path.join(self.dataset("a", qc()), "qc.json"),
                 os.path.join(self.dataset("b", qc()), "qc.json")]
        report = self.run_staging(self.config(paths))
        self.assertEqual(report["chosen"]["n_subjects"], 2)

    def test_the_app_s_own_output_directory_layout(self):
        # What this pipeline publishes: <dataset>/eddyqc/qc.json.
        folders = []
        for name in ("a", "b"):
            inner = self.dataset(os.path.join(name, "eddyqc"), qc())
            folders.append(os.path.dirname(inner))
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["n_subjects"], 2)

    def test_the_older_qc_bundle_layout_is_accepted(self):
        # Datasets produced before the lean eddyqc output existed carry the QUAD
        # folder inside the qc/ bundle; those studies can still be pooled.
        folders = []
        for name in ("a", "b"):
            inner = self.dataset(os.path.join(name, "qc", "eddy_quad"), qc())
            folders.append(os.path.dirname(os.path.dirname(inner)))
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["n_subjects"], 2)

    def test_an_archived_qc_dataset_is_accepted(self):
        # brainlife stages a dataset at what was *inside* output/qc/, so an
        # archived qc dataset from an older run presents as <dataset>/eddy_quad.
        folders = []
        for name in ("a", "b"):
            inner = self.dataset(os.path.join(name, "eddy_quad"), qc())
            folders.append(os.path.dirname(inner))
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["n_subjects"], 2)

    def test_a_label_is_found_past_the_generic_directories(self):
        # An older task's database sits at <task>/output/qc/eddy_quad/qc.json,
        # and none of those three directories names the subject.
        folders = []
        for name in ("sub-legacy-1", "sub-legacy-2"):
            inner = self.dataset(os.path.join(name, "output", "qc", "eddy_quad"), qc())
            folders.append(os.path.dirname(os.path.dirname(os.path.dirname(inner))))
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["subjects"], ["sub-legacy-1", "sub-legacy-2"])

    def test_a_single_input_is_still_an_array_on_brainlife(self):
        # brainlife puts even a single selected dataset in an array, and a
        # one-subject group report is refused as meaningless.
        report = self.run_staging(self.config([self.dataset("a", qc())]), expect=1)
        self.assertIn("at least 2", report["error"])

    def test_a_plain_text_list_of_folders_is_accepted(self):
        folders = [self.dataset("a", qc()), self.dataset("b", qc())]
        listing = os.path.join(self.root, "list.txt")
        with open(listing, "w") as fh:
            fh.write("# folders\n" + "\n".join(folders) + "\n")
        report = self.run_staging(self.config(listing))
        self.assertEqual(report["chosen"]["n_subjects"], 2)

    def test_an_input_without_a_qc_json_is_reported_not_fatal(self):
        empty = os.path.join(self.root, "inputs", "empty")
        os.makedirs(empty)
        folders = [self.dataset("a", qc()), self.dataset("b", qc()), empty]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["n_subjects"], 2)
        self.assertEqual(len(report["rejected"]), 1)
        self.assertIn("no qc.json", report["rejected"][0]["reason"])

    def test_unreadable_json_is_reported(self):
        folder = os.path.join(self.root, "inputs", "broken")
        os.makedirs(folder)
        with open(os.path.join(folder, "qc.json"), "w") as fh:
            fh.write("{not json")
        report = self.run_staging(
            self.config([self.dataset("a", qc()), self.dataset("b", qc()), folder]))
        self.assertIn("not readable JSON", report["rejected"][0]["reason"])

    def test_no_inputs_at_all_explains_what_to_map(self):
        report = self.run_staging(self.config([]), expect=1)
        self.assertIn("eddyqc", report["error"])


class TestLabels(StagingCase):
    def test_the_subject_comes_from_the_app_s_own_summary(self):
        folders = [self.dataset("a", qc(), summary={"subject": "sub-CC01", "session": ""}),
                   self.dataset("b", qc(), summary={"subject": "sub-CC02", "session": ""})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["subjects"], ["sub-CC01", "sub-CC02"])

    def test_brainlife_input_metadata_is_used_positionally(self):
        folders = [self.dataset("a", qc()), self.dataset("b", qc())]
        config = self.config(folders, _inputs=[{"meta": {"subject": "S1"}},
                                               {"meta": {"subject": "S2",
                                                         "session": "ses-2"}}])
        report = self.run_staging(config)
        self.assertEqual(report["chosen"]["subjects"], ["S1", "S2_ses-2"])

    def test_metadata_is_matched_by_task_id_not_only_position(self):
        # brainlife stages each dataset under the id of the task that produced
        # it. Matching that beats trusting the array order, which is what goes
        # wrong when a task is restaged or an input is dropped.
        first = self.dataset(os.path.join("5f0e1111111111111111aaaa", "eddyqc"), qc())
        second = self.dataset(os.path.join("5f0e2222222222222222bbbb", "eddyqc"), qc())
        folders = [os.path.dirname(first), os.path.dirname(second)]
        config = self.config(folders, _inputs=[
            {"task_id": "5f0e2222222222222222bbbb", "meta": {"subject": "second"}},
            {"task_id": "5f0e1111111111111111aaaa", "meta": {"subject": "first"}}])
        report = self.run_staging(config)
        self.assertEqual(report["chosen"]["subjects"], ["first", "second"])

    def test_the_app_s_own_label_wins_over_input_metadata(self):
        folders = [self.dataset("a", qc(), summary={"subject": "sub-recorded"}),
                   self.dataset("b", qc(), summary={"subject": "sub-other"})]
        config = self.config(folders, _inputs=[{"meta": {"subject": "stale-1"}},
                                               {"meta": {"subject": "stale-2"}}])
        report = self.run_staging(config)
        self.assertEqual(report["chosen"]["subjects"], ["sub-recorded", "sub-other"])

    def test_the_folder_name_is_the_last_resort(self):
        report = self.run_staging(
            self.config([self.dataset("sub-zz", qc()), self.dataset("sub-yy", qc())]))
        self.assertEqual(sorted(report["chosen"]["subjects"]), ["sub-yy", "sub-zz"])

    def test_repeated_labels_are_disambiguated(self):
        # Two sessions of one subject, with no session recorded: without this
        # they would stage into the same folder and one would overwrite the
        # other, silently halving the study.
        folders = [self.dataset("a", qc(), summary={"subject": "sub-01"}),
                   self.dataset("b", qc(), summary={"subject": "sub-01"})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["subjects"], ["sub-01", "sub-01-2"])

    def test_labels_are_safe_as_directory_names(self):
        folders = [self.dataset("a", qc(), summary={"subject": "sub 01/x"}),
                   self.dataset("b", qc(), summary={"subject": "sub-02"})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["subjects"][0], "sub-01-x")

    def test_explicit_labels_win(self):
        folders = [self.dataset("a", qc(), summary={"subject": "sub-01"}),
                   self.dataset("b", qc(), summary={"subject": "sub-02"})]
        report = self.run_staging(self.config(folders, subject_labels="one,two"))
        self.assertEqual(report["chosen"]["subjects"], ["one", "two"])


class TestCohorts(StagingCase):
    def heterogeneous(self):
        """Three GPU subjects and two that fell back to CPU eddy."""
        folders = []
        for index in range(3):
            folders.append(self.dataset("gpu%d" % index, qc(),
                                        summary={"subject": "gpu%d" % index}))
        for index in range(2):
            folders.append(self.dataset("cpu%d" % index,
                                        qc(qc_s2v_params_flag=False),
                                        summary={"subject": "cpu%d" % index}))
        return folders

    def test_the_largest_cohort_is_pooled_and_the_rest_reported(self):
        report = self.run_staging(self.config(self.heterogeneous()))
        self.assertEqual(report["chosen"]["n_subjects"], 3)
        self.assertEqual(report["chosen"]["subjects"], ["gpu0", "gpu1", "gpu2"])
        self.assertEqual(len(report["excluded"]), 1)
        self.assertEqual(report["excluded"][0]["subjects"], ["cpu0", "cpu1"])

    def test_the_exclusion_says_why_in_plain_terms(self):
        report = self.run_staging(self.config(self.heterogeneous()))
        self.assertIn("slice-to-volume", report["excluded"][0]["reason"])

    def test_differing_acquisition_parameters_split_the_cohort(self):
        # The real failure: same eddy options, different readout time. SQUAD
        # compares the eddy input data and refuses the study over it.
        other = [0.0, 1.0, 0.0, 0.1043, 0.0, -1.0, 0.0, 0.1043]
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(), summary={"subject": "b"}),
                   self.dataset("c", qc(data_eddy_para=other),
                                summary={"subject": "c"})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["subjects"], ["a", "b"])
        self.assertIn("topup acquisition parameters", report["excluded"][0]["reason"])
        self.assertIn("0.1043", report["excluded"][0]["reason"])

    def test_a_b_value_five_apart_pools_because_squad_pools_it(self):
        # squad_db.py compares the b-values with np.allclose(atol=20), so 1495
        # and 1500 are one cohort to it. Splitting them here would refuse a
        # study SQUAD accepts -- which is how a two-site study became two group
        # reports of two subjects each.
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(), summary={"subject": "b"}),
                   self.dataset("c", qc(data_unique_bvals=[1495, 3000]),
                                summary={"subject": "c"})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["n_subjects"], 3)
        self.assertEqual(report["excluded"], [])

    def test_a_b_value_beyond_the_tolerance_still_splits(self):
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(), summary={"subject": "b"}),
                   self.dataset("c", qc(data_unique_bvals=[1530, 3000]),
                                summary={"subject": "c"})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["excluded"][0]["subjects"], ["c"])
        self.assertIn("shell b-values", report["excluded"][0]["reason"])
        self.assertIn("more than SQUAD tolerates", report["excluded"][0]["reason"])

    def test_a_voxel_size_within_one_percent_pools(self):
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(data_vox_size=[2.01, 2.0, 2.0]),
                                summary={"subject": "b"})]
        self.assertEqual(self.run_staging(self.config(folders))["excluded"], [])

    def test_a_voxel_size_beyond_it_does_not(self):
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(), summary={"subject": "b"}),
                   self.dataset("c", qc(data_vox_size=[2.5, 2.0, 2.0]),
                                summary={"subject": "c"})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["excluded"][0]["subjects"], ["c"])
        self.assertIn("voxel size", report["excluded"][0]["reason"])

    def test_the_tolerance_is_measured_from_the_first_subject_as_squad_measures_it(self):
        # 1490, 1500 and 1515 are each within 20 of a neighbour but the ends are
        # 25 apart. SQUAD compares every subject against the first folder in the
        # list, so that is where the line falls here too.
        folders = [self.dataset("a", qc(data_unique_bvals=[1490, 3000]),
                                summary={"subject": "a"}),
                   self.dataset("b", qc(), summary={"subject": "b"}),
                   self.dataset("c", qc(data_unique_bvals=[1515, 3000]),
                                summary={"subject": "c"})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["subjects"], ["a", "b"])
        self.assertEqual(report["excluded"][0]["subjects"], ["c"])

    def test_the_protocol_squad_never_compares_does_not_split_a_cohort(self):
        # Same volumes, distributed differently between the shells. squad_db.py
        # does not compare data_protocol at all.
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(data_protocol=[[1500, 4], [3000, 12]]),
                                summary={"subject": "b"})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["n_subjects"], 2)
        self.assertEqual(report["excluded"], [])

    def test_but_it_is_disclosed_rather_than_passed_over_in_silence(self):
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(data_protocol=[[1500, 4], [3000, 12]]),
                                summary={"subject": "b"})]
        disclosed = self.run_staging(self.config(folders))["chosen"]["disclosed_differences"]
        self.assertEqual([d["field"] for d in disclosed], ["data_protocol"])
        self.assertEqual(sorted(s for value in disclosed[0]["values"]
                                for s in value["subjects"]), ["a", "b"])

    def test_the_report_says_how_each_field_was_compared(self):
        folders = [self.dataset("a", qc()), self.dataset("b", qc())]
        comparison = self.run_staging(self.config(folders))["comparison"]
        self.assertEqual(comparison["exact"], list(si.EXACT_FIELDS))
        self.assertEqual(sorted(comparison["tolerant"]),
                         ["data_unique_bvals", "data_vox_size"])
        self.assertEqual(comparison["disclosed"], list(si.DISCLOSED_FIELDS))

    def test_a_tolerant_field_named_in_signature_fields_is_compared_exactly(self):
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(), summary={"subject": "b"}),
                   self.dataset("c", qc(data_unique_bvals=[1495, 3000]),
                                summary={"subject": "c"})]
        widened = list(si.EXACT_FIELDS) + ["data_unique_bvals"]
        report = self.run_staging(self.config(
            folders, signature_fields=" ".join(widened)))
        self.assertEqual(report["excluded"][0]["subjects"], ["c"])
        self.assertIn("shell b-values differs", report["excluded"][0]["reason"])
        # ...and it is not then also reported as a tolerance gap.
        self.assertNotIn("more than SQUAD tolerates", report["excluded"][0]["reason"])
        self.assertEqual(report["comparison"]["tolerant"], {"data_vox_size":
            {"atol": si.TOLERANT_FIELDS["data_vox_size"][0],
             "rtol": si.TOLERANT_FIELDS["data_vox_size"][1]}})

    def test_signature_fields_can_narrow_what_is_compared(self):
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(), summary={"subject": "b"}),
                   self.dataset("c", qc(data_no_dw_vols=15), summary={"subject": "c"})]
        self.assertEqual(
            len(self.run_staging(self.config(folders))["excluded"]), 1)
        report = self.run_staging(self.config(
            folders, signature_fields="data_no_shells data_unique_bvals"))
        self.assertEqual(report["chosen"]["n_subjects"], 3)
        self.assertEqual(report["excluded"], [])

    def test_a_differing_protocol_is_explained_too(self):
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(), summary={"subject": "b"}),
                   self.dataset("c", qc(data_no_shells=1, data_unique_bvals=[1500]),
                                summary={"subject": "c"})]
        report = self.run_staging(self.config(folders))
        self.assertIn("number of shells differs", report["excluded"][0]["reason"])

    def test_a_cohort_of_one_is_refused_with_the_reason(self):
        # Two subjects that cannot be pooled with each other pass the
        # usable-inputs check and would otherwise produce a "group" of one.
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(data_no_dw_vols=98, data_no_shells=1,
                                        data_unique_bvals=[1000]),
                                summary={"subject": "b"})]
        report = self.run_staging(self.config(folders), expect=1)
        self.assertIn("largest cohort has 1 subject", report["error"])
        self.assertIn("2 cohort(s) present", report["error"])
        self.assertIn("shell b-values", report["error"])
        # The split itself is still reported, so the task page can show it.
        self.assertEqual(len(report["cohorts"]), 2)

    def test_min_subjects_one_lets_a_single_subject_cohort_through(self):
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(data_no_shells=1), summary={"subject": "b"})]
        report = self.run_staging(self.config(folders), min_subjects=1)
        self.assertEqual(report["chosen"]["n_subjects"], 1)
        # Two cohorts of one: the pick was arbitrary and must say so.
        self.assertEqual(len(report["chosen"]["tied_with"]), 1)

    def test_a_genuinely_larger_cohort_is_not_marked_as_tied(self):
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(), summary={"subject": "b"}),
                   self.dataset("c", qc(data_no_shells=1), summary={"subject": "c"})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["tied_with"], [])

    def test_a_named_cohort_can_be_selected(self):
        report = self.run_staging(self.config(self.heterogeneous()))
        minority = next(c for c in report["cohorts"] if c["n_subjects"] == 2)
        again = self.run_staging(
            self.config(self.heterogeneous(), cohort=minority["signature_hash"]))
        self.assertEqual(again["chosen"]["subjects"], ["cpu0", "cpu1"])

    def test_an_unknown_cohort_lists_the_ones_present(self):
        report = self.run_staging(self.config(self.heterogeneous(), cohort="nope"),
                                  expect=1)
        self.assertIn("matches none", report["error"])

    def test_require_homogeneous_refuses_to_pick(self):
        report = self.run_staging(
            self.config(self.heterogeneous(), require_homogeneous=True), expect=1)
        self.assertIn("incompatible cohorts", report["error"])

    def test_a_homogeneous_study_needs_no_exclusions(self):
        folders = [self.dataset("a", qc()), self.dataset("b", qc())]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["excluded"], [])
        self.assertEqual(len(report["cohorts"]), 1)

    def test_the_choice_does_not_depend_on_input_order(self):
        folders = self.heterogeneous()
        first = self.run_staging(self.config(folders))
        second = self.run_staging(self.config(list(reversed(folders))))
        self.assertEqual(first["chosen"]["signature"], second["chosen"]["signature"])


class TestDiagnose(StagingCase):
    """The fallback for a refusal this app did not predict."""

    def list_file(self, databases):
        listing = os.path.join(self.root, "list.txt")
        with open(listing, "w") as fh:
            for name, database in databases.items():
                folder = os.path.join(self.root, "staged", name)
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, "qc.json"), "w") as out:
                    json.dump(database, out)
                fh.write(folder + "\n")
        return listing

    def diagnose(self, databases):
        from contextlib import redirect_stderr
        import io
        captured = io.StringIO()
        with redirect_stderr(captured):
            status = si.main(["--diagnose", self.list_file(databases)])
        return status, captured.getvalue()

    def test_names_the_field_and_who_holds_which_value(self):
        status, output = self.diagnose({
            "sub-01": qc(), "sub-02": qc(),
            "sub-03": qc(data_eddy_para=[[0, 1, 0, 0.1043]]),
        })
        self.assertEqual(status, 0)
        self.assertIn("data_eddy_para", output)
        self.assertIn("topup acquisition parameters", output)
        self.assertIn("sub-03", output)
        self.assertIn("signature_fields", output)

    def test_says_so_when_nothing_differs(self):
        _, output = self.diagnose({"sub-01": qc(), "sub-02": qc()})
        self.assertIn("every eddy input field matches", output)

    def test_ignores_per_subject_file_paths(self):
        _, output = self.diagnose({
            "sub-01": qc(data_file_eddy="/a/eddy_corrected"),
            "sub-02": qc(data_file_eddy="/b/eddy_corrected"),
        })
        self.assertIn("every eddy input field matches", output)


class TestStaging(StagingCase):
    def test_each_qc_json_is_copied_so_update_can_write_back(self):
        folders = [self.dataset("a", qc(), summary={"subject": "sub-01"}),
                   self.dataset("b", qc(), summary={"subject": "sub-02"})]
        report = self.run_staging(self.config(folders))
        with open(report["list_file"]) as fh:
            staged = [line.strip() for line in fh if line.strip()]
        self.assertEqual(len(staged), 2)
        for folder in staged:
            self.assertTrue(os.path.isfile(os.path.join(folder, "qc.json")))
            self.assertTrue(os.path.isfile(os.path.join(folder, "qc.pdf")))
            # Inside our own work directory, not the read-only input.
            self.assertTrue(folder.startswith(self.work))

    def test_the_staged_database_points_at_the_staged_folder(self):
        folders = [self.dataset("a", qc(qc_path="/read/only/input")),
                   self.dataset("b", qc())]
        report = self.run_staging(self.config(folders))
        with open(report["list_file"]) as fh:
            first = fh.readline().strip()
        with open(os.path.join(first, "qc.json")) as fh:
            self.assertEqual(json.load(fh)["qc_path"], first)

    def test_the_metrics_survive_the_copy(self):
        folders = [self.dataset("a", qc()), self.dataset("b", qc())]
        report = self.run_staging(self.config(folders))
        with open(report["list_file"]) as fh:
            first = fh.readline().strip()
        with open(os.path.join(first, "qc.json")) as fh:
            staged = json.load(fh)
        for key, value in qc().items():
            self.assertEqual(staged[key], value, key)

    def test_the_list_order_is_the_subject_order(self):
        folders = [self.dataset("a", qc(), summary={"subject": "sub-01"}),
                   self.dataset("b", qc(), summary={"subject": "sub-02"})]
        report = self.run_staging(self.config(folders))
        with open(report["list_file"]) as fh:
            staged = [os.path.basename(line.strip()) for line in fh if line.strip()]
        self.assertEqual(staged, report["chosen"]["subjects"])

    def test_a_missing_single_subject_pdf_is_reported_not_fatal(self):
        # SQUAD's update step opens every listed subject's qc.pdf, so the group
        # stage has to know before it asks for the update.
        folders = [self.dataset("a", qc(), summary={"subject": "sub-01"}, pdf=False),
                   self.dataset("b", qc(), summary={"subject": "sub-02"})]
        report = self.run_staging(self.config(folders))
        self.assertEqual(report["chosen"]["n_subjects"], 2)
        self.assertEqual(report["missing_reports"], ["sub-01"])

    def test_a_complete_cohort_reports_nothing_missing(self):
        folders = [self.dataset("a", qc()), self.dataset("b", qc())]
        self.assertEqual(self.run_staging(self.config(folders))["missing_reports"], [])


class TestPoolAcrossAcquisition(StagingCase):
    """The opt-in way past the one difference no rearrangement of the key fixes.

    Two sites run the same protocol; their readout times differ in the sixth
    decimal and their acqparams rows are in the other order. squad_db.py
    compares data_eddy_para exactly, so it refuses the study -- and no cohort key
    that mirrors SQUAD can pool them. pool_across_acquisition rewrites the staged
    copies, within stated bounds, and says so everywhere it can.
    """

    # The real values and the real shape, from a two-site study: flat, and
    # with the two sites listing their blip-up and blip-down rows in the
    # opposite order.
    SITE_A = [0.0, 1.0, 0.0, 0.0959097, 0.0, -1.0, 0.0, 0.0959097]
    SITE_B = [0.0, -1.0, 0.0, 0.0965997, 0.0, 1.0, 0.0, 0.0965997]

    def two_sites(self, site_b=None):
        site_b = self.SITE_B if site_b is None else site_b
        return [self.dataset("a", qc(data_eddy_para=self.SITE_A),
                             summary={"subject": "a"}),
                self.dataset("b", qc(data_eddy_para=self.SITE_A),
                             summary={"subject": "b"}),
                self.dataset("c", qc(data_eddy_para=site_b), summary={"subject": "c"}),
                self.dataset("d", qc(data_eddy_para=site_b), summary={"subject": "d"})]

    def test_the_flat_table_eddy_quad_actually_writes_is_read(self):
        # The shape a real qc.json carries: 4N numbers, not N rows of 4. Reading
        # only the nested form made harmonisation refuse every real study with
        # "not a table of [x, y, z, readout] rows" -- a parser failure wearing
        # the words of an acquisition difference.
        self.assertEqual(
            si.acqp_rows([0.0, -1.0, 0.0, 0.0959097, 0.0, 1.0, 0.0, 0.0959097]),
            [((0.0, -1.0, 0.0), 0.0959097), ((0.0, 1.0, 0.0), 0.0959097)])

    def test_the_nested_table_is_read_too(self):
        self.assertEqual(
            si.acqp_rows([[0.0, -1.0, 0.0, 0.0959097], [0.0, 1.0, 0.0, 0.0959097]]),
            [((0.0, -1.0, 0.0), 0.0959097), ((0.0, 1.0, 0.0), 0.0959097)])

    def test_something_that_is_not_an_acqparams_table_is_refused(self):
        for value in ([0.0, 1.0, 0.0], [0.0, 1.0, 0.0, 0.09, 0.0], "axial", [], None):
            self.assertIsNone(si.acqp_rows(value), value)

    def test_the_real_two_site_difference_harmonises(self):
        # MCG vs UMN, exactly as their qc.json carries it: the same two
        # phase-encode vectors in the opposite row order, and readout times
        # 0.00069 s apart.
        mcg = [0.0, -1.0, 0.0, 0.0959097, 0.0, 1.0, 0.0, 0.0959097]
        umn = [0.0, 1.0, 0.0, 0.0965997, 0.0, -1.0, 0.0, 0.0965997]
        self.assertEqual(si.harmonisable(mcg, umn, 0.05), "")

    def test_off_by_default_the_two_sites_are_two_cohorts(self):
        report = self.run_staging(self.config(self.two_sites()))
        self.assertEqual(report["chosen"]["n_subjects"], 2)
        self.assertIn("topup acquisition parameters", report["excluded"][0]["reason"])
        self.assertEqual(report["chosen"]["harmonised"], {})

    def test_on_request_the_two_sites_become_one_cohort(self):
        report = self.run_staging(self.config(self.two_sites(),
                                              pool_across_acquisition=True))
        self.assertEqual(report["chosen"]["n_subjects"], 4)
        self.assertEqual(sorted(report["chosen"]["subjects"]), ["a", "b", "c", "d"])
        self.assertEqual(report["excluded"], [])

    def test_every_original_value_is_recorded(self):
        report = self.run_staging(self.config(self.two_sites(),
                                              pool_across_acquisition=True))
        harmonised = report["chosen"]["harmonised"]
        self.assertEqual(harmonised["field"], "data_eddy_para")
        self.assertIn(harmonised["reference"], (self.SITE_A, self.SITE_B))
        rewritten = harmonised["per_subject"]
        self.assertEqual(sorted(rewritten), ["a", "b"] if harmonised["reference"]
                         == self.SITE_B else ["c", "d"])
        for original in rewritten.values():
            self.assertNotEqual(original, harmonised["reference"])

    def test_the_staged_copies_carry_the_reference_value_and_the_inputs_do_not(self):
        report = self.run_staging(self.config(self.two_sites(),
                                              pool_across_acquisition=True))
        reference = report["chosen"]["harmonised"]["reference"]
        with open(report["list_file"]) as fh:
            folders = [line.strip() for line in fh if line.strip()]
        self.assertEqual(len(folders), 4)
        for folder in folders:
            with open(os.path.join(folder, "qc.json")) as fh:
                self.assertEqual(json.load(fh)["data_eddy_para"], reference)
        # The input dataset is the record of what was acquired; it is read-only
        # on brainlife and must stay untouched here too.
        originals = set()
        for name in ("a", "b", "c", "d"):
            with open(os.path.join(self.root, "inputs", name, "qc.json")) as fh:
                originals.add(json.dumps(json.load(fh)["data_eddy_para"]))
        self.assertEqual(len(originals), 2)

    def test_a_different_phase_encode_vector_is_refused(self):
        # Left-right against anterior-posterior is a different acquisition, not
        # the same one described differently.
        other = [1.0, 0.0, 0.0, 0.0959097, -1.0, 0.0, 0.0, 0.0959097]
        report = self.run_staging(self.config(self.two_sites(other),
                                              pool_across_acquisition=True))
        self.assertEqual(report["chosen"]["n_subjects"], 2)
        self.assertIn("not harmonised", report["excluded"][0]["reason"])
        self.assertIn("phase-encode vectors themselves differ",
                      report["excluded"][0]["reason"])

    def test_a_readout_beyond_the_tolerance_is_refused(self):
        far = [0.0, 1.0, 0.0, 0.5, 0.0, -1.0, 0.0, 0.5]
        report = self.run_staging(self.config(self.two_sites(far),
                                              pool_across_acquisition=True))
        self.assertEqual(report["chosen"]["n_subjects"], 2)
        self.assertIn("readout times differ by", report["excluded"][0]["reason"])
        self.assertIn("pool_readout_tolerance", report["excluded"][0]["reason"])

    def test_the_readout_tolerance_can_be_narrowed(self):
        report = self.run_staging(self.config(self.two_sites(),
                                              pool_across_acquisition=True,
                                              pool_readout_tolerance=0.0001))
        self.assertEqual(report["chosen"]["n_subjects"], 2)
        self.assertIn("readout times differ by", report["excluded"][0]["reason"])

    def test_a_cohort_that_differs_in_more_than_the_parameters_is_left_alone(self):
        # The option is about one field. A subject with no slice-to-volume
        # metrics is excluded as it always was, and nothing is rewritten.
        folders = self.two_sites()
        folders.append(self.dataset("e", qc(qc_s2v_params_flag=False,
                                            data_eddy_para=self.SITE_A),
                                    summary={"subject": "e"}))
        report = self.run_staging(self.config(folders, pool_across_acquisition=True))
        self.assertEqual(report["chosen"]["n_subjects"], 4)
        self.assertEqual(report["excluded"][0]["subjects"], ["e"])
        self.assertIn("slice-to-volume", report["excluded"][0]["reason"])
        self.assertNotIn("not harmonised", report["excluded"][0]["reason"])

    def test_a_differing_number_of_series_is_refused(self):
        single = [0.0, 1.0, 0.0, 0.0959097]
        report = self.run_staging(self.config(self.two_sites(single),
                                              pool_across_acquisition=True))
        self.assertEqual(report["chosen"]["n_subjects"], 2)
        self.assertIn("2 and 1 acquisition parameter row(s)",
                      report["excluded"][0]["reason"])

    def test_nothing_to_harmonise_leaves_the_report_empty_handed(self):
        folders = [self.dataset("a", qc(), summary={"subject": "a"}),
                   self.dataset("b", qc(), summary={"subject": "b"})]
        report = self.run_staging(self.config(folders, pool_across_acquisition=True))
        self.assertEqual(report["chosen"]["harmonised"], {})
        self.assertEqual(report["chosen"]["n_subjects"], 2)


class TestGroupingVariable(StagingCase):
    def two_subjects(self):
        return [self.dataset("a", qc(), summary={"subject": "sub-01"}),
                self.dataset("b", qc(), summary={"subject": "sub-02"})]

    def table(self, text, name="groups.tsv"):
        path = os.path.join(self.root, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_a_participants_style_table_is_matched_by_subject(self):
        table = self.table("participant_id\tgroup\nsub-02\t1\nsub-01\t0\n")
        report = self.run_staging(self.config(self.two_subjects(),
                                              grouping_variable=table))
        with open(report["variable_file"]) as fh:
            lines = [line.strip() for line in fh if line.strip()]
        # Header, 0 for categorical, then one value per subject in list order --
        # sub-01 first, whatever order the table listed them in.
        self.assertEqual(lines, ["group", "0", "0", "1"])
        self.assertIn("matched by subject name", report["variable"]["source"])

    def test_a_comma_separated_table_works_too(self):
        table = self.table("subject,age\nsub-01,31\nsub-02,44\n", "groups.csv")
        report = self.run_staging(self.config(self.two_subjects(),
                                              grouping_variable=table,
                                              variable_is_continuous=True,
                                              variable_name="age"))
        with open(report["variable_file"]) as fh:
            lines = [line.strip() for line in fh if line.strip()]
        self.assertEqual(lines, ["age", "1", "31", "44"])
        self.assertTrue(report["variable"]["continuous"])

    def test_a_table_missing_a_subject_is_refused_before_squad_runs(self):
        # SQUAD matches values to subjects by position, so a short list would
        # silently attribute one subject's value to another.
        table = self.table("subject\tgroup\nsub-01\t0\n")
        report = self.run_staging(self.config(self.two_subjects(),
                                              grouping_variable=table), expect=1)
        self.assertIn("no row for", report["error"])
        self.assertIn("sub-02", report["error"])

    def test_a_table_without_a_subject_column_says_so(self):
        table = self.table("age\tgroup\n31\t0\n44\t1\n")
        report = self.run_staging(self.config(self.two_subjects(),
                                              grouping_variable=table), expect=1)
        self.assertIn("needs a subject column", report["error"])

    def test_squad_s_own_format_is_passed_through(self):
        native = self.table("group\n0\n0\n1\n", "var.txt")
        report = self.run_staging(self.config(self.two_subjects(),
                                              grouping_variable=native))
        with open(report["variable_file"]) as fh:
            self.assertEqual(fh.read().split(), ["group", "0", "0", "1"])
        self.assertIn("by position", report["variable"]["source"])

    def test_squad_s_own_format_with_the_wrong_count_is_refused(self):
        native = self.table("group\n0\n0\n1\n0\n1\n", "var.txt")
        report = self.run_staging(self.config(self.two_subjects(),
                                              grouping_variable=native), expect=1)
        self.assertIn("carries 4 values", report["error"])

    def test_the_values_follow_the_cohort_not_the_inputs(self):
        # A subject excluded from the cohort must not leave its value behind in
        # the variable file, or every later value shifts by one.
        folders = [self.dataset("a", qc(), summary={"subject": "sub-01"}),
                   self.dataset("b", qc(), summary={"subject": "sub-02"}),
                   self.dataset("c", qc(qc_field_flag=False),
                                summary={"subject": "sub-03"})]
        table = self.table("subject\tgroup\nsub-01\t0\nsub-02\t1\nsub-03\t1\n")
        report = self.run_staging(self.config(folders, grouping_variable=table))
        self.assertEqual(report["chosen"]["subjects"], ["sub-01", "sub-02"])
        with open(report["variable_file"]) as fh:
            self.assertEqual(fh.read().split(), ["group", "0", "0", "1"])

    def test_a_missing_variable_file_is_named(self):
        report = self.run_staging(
            self.config(self.two_subjects(), grouping_variable="/no/such/file"),
            expect=1)
        self.assertIn("does not exist", report["error"])


if __name__ == "__main__":
    unittest.main()
