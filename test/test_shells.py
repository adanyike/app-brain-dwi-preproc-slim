#!/usr/bin/env python3
"""Unit tests for python/shells.py."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import shells as sh  # noqa: E402


def bvals_0_1500_3000():
    """The acquisition this app was written for: b=0/1500/3000, jittered."""
    out = []
    for i in range(120):
        if i % 20 == 0:
            out.append(5.0)
        elif i % 2:
            out.append(1495.0 + (i % 7))
        else:
            out.append(2998.0 + (i % 5))
    return out


class TestDetectShells(unittest.TestCase):
    def test_three_shells_are_separated(self):
        found = sh.detect_shells(bvals_0_1500_3000())
        self.assertEqual([s.b for s in found], [5, 1500, 3000])
        self.assertEqual([s.is_b0 for s in found], [True, False, False])

    def test_shells_come_back_lowest_first(self):
        found = sh.detect_shells([3000, 0, 1000, 3000, 0])
        self.assertEqual([s.b for s in found], [0, 1000, 3000])

    def test_jitter_within_tolerance_stays_one_shell(self):
        found = sh.detect_shells([0, 0] + [1490, 1495, 1500, 1505, 1510])
        self.assertEqual(len(found), 2)
        self.assertEqual(found[1].b, 1500)
        self.assertEqual(len(found[1].indices), 5)

    def test_shell_label_is_the_nominal_b_value(self):
        # A jittered b=1500 shell whose raw mean is 1499 is still called 1500.
        found = sh.detect_shells([0] * 2 + [1498, 1499, 1500, 1501, 1502,
                                            1498, 1499, 1500, 1498, 1499, 1500])
        self.assertEqual(found[1].b, 1500)
        self.assertAlmostEqual(found[1].mean, 1499.45, places=2)

    def test_low_baselines_keep_their_exact_value(self):
        self.assertEqual(sh.nominal_bvalue(0), 0)
        self.assertEqual(sh.nominal_bvalue(5), 5)
        self.assertEqual(sh.nominal_bvalue(40), 40)
        self.assertEqual(sh.nominal_bvalue(1499.45), 1500)
        self.assertEqual(sh.nominal_bvalue(2998.0), 3000)
        self.assertEqual(sh.nominal_bvalue(740.0), 750)
        self.assertEqual(sh.nominal_bvalue(700.0), 700)

    def test_tolerance_controls_splitting(self):
        bvals = [0, 1000, 1200]
        self.assertEqual(len(sh.detect_shells(bvals, tolerance=100)), 3)
        self.assertEqual(len(sh.detect_shells(bvals, tolerance=300)), 2)

    def test_b0_threshold_controls_the_baseline_flag(self):
        loose = sh.detect_shells([0, 0, 300, 300, 1500], b0_threshold=400)
        self.assertEqual([s.is_b0 for s in loose], [True, True, False])
        tight = sh.detect_shells([0, 0, 300, 300, 1500], b0_threshold=50)
        self.assertEqual([s.is_b0 for s in tight], [True, False, False])

    def test_indices_are_recorded(self):
        found = sh.detect_shells([0, 1500, 0, 1500])
        self.assertEqual(found[0].indices, [0, 2])
        self.assertEqual(found[1].indices, [1, 3])

    def test_empty_is_rejected(self):
        with self.assertRaises(sh.ShellError):
            sh.detect_shells([])


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.shells = sh.detect_shells(bvals_0_1500_3000())

    def test_all_fits_everything(self):
        for word in ("all", "all_bvalues", "0", "ALL", ""):
            plan = sh.resolve(self.shells, word)
            self.assertEqual(plan["mode"], "all", word)
            self.assertIsNone(plan["extract_arg"])
            self.assertEqual(plan["n_volumes"], 120)

    def test_lowest_picks_the_low_diffusion_shell(self):
        plan = sh.resolve(self.shells, "lowest")
        self.assertEqual(plan["shell"], 1500)
        self.assertEqual(plan["extract_arg"], "5,1500")
        self.assertEqual(plan["label"], "1500")

    def test_highest_picks_the_high_diffusion_shell(self):
        plan = sh.resolve(self.shells, "highest")
        self.assertEqual(plan["shell"], 3000)
        self.assertEqual(plan["extract_arg"], "5,3000")

    def test_b0_is_never_treated_as_the_lowest_shell(self):
        # The whole point: "lowest" means lowest *diffusion-weighted*, not b=0.
        self.assertNotEqual(sh.resolve(self.shells, "lowest")["shell"], 5)

    def test_numeric_matches_the_nearest_shell(self):
        self.assertEqual(sh.resolve(self.shells, "1500")["shell"], 1500)
        self.assertEqual(sh.resolve(self.shells, 3000)["shell"], 3000)

    def test_numeric_that_matches_nothing_is_rejected(self):
        with self.assertRaises(sh.ShellError) as caught:
            sh.resolve(self.shells, "800")
        self.assertIn("Shells present", str(caught.exception))

    def test_baseline_is_always_included_in_the_extraction(self):
        for word in ("lowest", "highest", "1500"):
            plan = sh.resolve(self.shells, word)
            self.assertTrue(plan["extract_arg"].startswith("5,"), word)

    def test_every_b0_shell_is_kept(self):
        # Two distinct baselines below the threshold. This needs a tolerance
        # tighter than the threshold, otherwise they merge into one shell --
        # which is what happens with the defaults, and is fine.
        found = sh.detect_shells([0] * 3 + [40] * 3 + [1500] * 10,
                                 b0_threshold=50, tolerance=10)
        self.assertEqual([s.is_b0 for s in found], [True, True, False])
        plan = sh.resolve(found, "lowest", tolerance=10)
        self.assertEqual(plan["extract_arg"], "0,40,1500")
        self.assertEqual(plan["n_volumes"], 16)

    def test_nearby_baselines_merge_under_the_default_tolerance(self):
        found = sh.detect_shells([0] * 3 + [40] * 3 + [1500] * 10)
        self.assertEqual([s.b for s in found], [20, 1500])
        self.assertEqual(sh.resolve(found, "lowest")["extract_arg"], "20,1500")

    def test_single_shell_data_resolves_either_way(self):
        found = sh.detect_shells([0] * 5 + [1000] * 30)
        self.assertEqual(sh.resolve(found, "lowest")["shell"], 1000)
        self.assertEqual(sh.resolve(found, "highest")["shell"], 1000)

    def test_no_diffusion_shell_is_rejected(self):
        found = sh.detect_shells([0] * 5)
        with self.assertRaises(sh.ShellError):
            sh.resolve(found, "lowest")

    def test_no_baseline_is_rejected_for_a_single_shell(self):
        found = sh.detect_shells([300] * 5 + [1500] * 20)
        with self.assertRaises(sh.ShellError) as caught:
            sh.resolve(found, "highest")
        self.assertIn("b0_threshold", str(caught.exception))

    def test_no_baseline_is_fine_when_fitting_everything(self):
        found = sh.detect_shells([300] * 5 + [1500] * 20)
        self.assertEqual(sh.resolve(found, "all")["mode"], "all")

    def test_nonsense_selection_is_rejected(self):
        with self.assertRaises(sh.ShellError):
            sh.resolve(self.shells, "middling")


class TestCli(unittest.TestCase):
    def _run(self, bvals, select, **extra):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "dwi.bvals")
        with open(path, "w") as fh:
            fh.write(" ".join("%g" % b for b in bvals))
        out = os.path.join(tmp, "shells.json")
        argv = ["--bvals", path, "--select", str(select), "--out", out]
        for key, value in extra.items():
            argv += ["--" + key.replace("_", "-"), str(value)]
        sh.main(argv)
        with open(out) as fh:
            return json.load(fh)

    def test_report_records_request_and_resolution(self):
        report = self._run(bvals_0_1500_3000(), "lowest")
        self.assertEqual(report["requested"], "lowest")
        self.assertEqual(report["resolved"]["label"], "1500")
        self.assertEqual(len(report["shells"]), 3)
        self.assertEqual(report["n_volumes"], 120)

    def test_report_for_all(self):
        report = self._run(bvals_0_1500_3000(), "all")
        self.assertEqual(report["resolved"]["mode"], "all")
        self.assertIsNone(report["resolved"]["extract_arg"])

    def test_tolerance_is_honoured_by_the_cli(self):
        report = self._run([0, 0, 1000, 1200], "highest", tolerance=300)
        self.assertEqual(len(report["shells"]), 2)
        self.assertEqual(report["resolved"]["shell"], 1100)


if __name__ == "__main__":
    unittest.main()
