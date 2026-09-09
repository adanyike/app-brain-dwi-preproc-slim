#!/usr/bin/env python3
"""Unit tests for python/prepare_inputs.py."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import prepare_inputs as pi  # noqa: E402


def write_series(directory, name, bvals, pe_dir, readout=0.0342):
    bval_path = os.path.join(directory, name + ".bvals")
    bvec_path = os.path.join(directory, name + ".bvecs")
    json_path = os.path.join(directory, name + ".json")
    with open(bval_path, "w") as fh:
        fh.write(" ".join("%g" % b for b in bvals) + "\n")
    with open(bvec_path, "w") as fh:
        for _ in range(3):
            fh.write(" ".join("0.5" for _ in bvals) + "\n")
    with open(json_path, "w") as fh:
        json.dump({"PhaseEncodingDirection": pe_dir,
                   "TotalReadoutTime": readout}, fh)
    return bval_path, bvec_path, json_path


class TestPeVector(unittest.TestCase):
    def test_axes_and_signs(self):
        self.assertEqual(pi.pe_vector("j-"), [0.0, -1.0, 0.0])
        self.assertEqual(pi.pe_vector("j"), [0.0, 1.0, 0.0])
        self.assertEqual(pi.pe_vector("i"), [1.0, 0.0, 0.0])
        self.assertEqual(pi.pe_vector("k-"), [0.0, 0.0, -1.0])

    def test_unknown_axis_raises(self):
        with self.assertRaises(pi.PrepError):
            pi.pe_vector("q-")


class TestBvecLayouts(unittest.TestCase):
    def test_accepts_3xn_and_nx3(self):
        with tempfile.TemporaryDirectory() as tmp:
            wide = os.path.join(tmp, "wide.bvecs")
            tall = os.path.join(tmp, "tall.bvecs")
            with open(wide, "w") as fh:
                fh.write("1 0 0\n0 1 0\n0 0 1\n")          # 3 rows x 3 cols
            with open(tall, "w") as fh:
                fh.write("1 0 0\n0 1 0\n0 0 1\n0 0 1\n")   # 4 rows x 3 cols
            self.assertEqual(len(pi.read_bvecs(wide)[0]), 3)
            self.assertEqual(len(pi.read_bvecs(tall)[0]), 4)


class TestEvenlySpaced(unittest.TestCase):
    def test_endpoints_are_included(self):
        self.assertEqual(pi.evenly_spaced([0, 10, 20, 30], 2), [0, 30])
        # 4 items into 3 picks has no symmetric answer; positions 0, 1.5, 3
        # round to 0, 2, 3.
        self.assertEqual(pi.evenly_spaced([0, 10, 20, 30], 3), [0, 20, 30])
        self.assertEqual(pi.evenly_spaced([0, 20, 40, 60, 80], 3), [0, 40, 80])

    def test_returns_everything_when_asked_for_too_many(self):
        self.assertEqual(pi.evenly_spaced([1, 2], 5), [1, 2])


class TestSelectTopupB0s(unittest.TestCase):
    def test_uses_every_b0_when_there_are_few(self):
        bvals = [5, 1500, 1500, 5, 1500, 5]
        series = [0] * 3 + [1] * 3
        self.assertEqual(
            pi.select_topup_b0s(bvals, series, 50, 2, 8), [0, 3, 5])

    def test_thins_down_when_there_are_many(self):
        bvals = [5 if i % 4 == 0 else 1500 for i in range(24)]   # 6 b=0
        series = [0] * 12 + [1] * 12
        picked = pi.select_topup_b0s(bvals, series, 50, 2, 6)
        self.assertEqual(picked, [0, 8, 12, 20])                 # first+last each

    def test_no_b0_is_fatal(self):
        with self.assertRaises(pi.PrepError):
            pi.select_topup_b0s([1500, 1500], [0, 0], 50, 2, 8)


class TestEndToEnd(unittest.TestCase):
    def _run(self, tmp, ap_bvals, pa_bvals, **extra):
        ap = write_series(tmp, "ap", ap_bvals, "j-")
        pa = write_series(tmp, "pa", pa_bvals, "j")
        argv = ["--bvals", ap[0], "--bvecs", ap[1], "--json", ap[2],
                "--nvols", str(len(ap_bvals)),
                "--rbvals", pa[0], "--rbvecs", pa[1], "--rjson", pa[2],
                "--rnvols", str(len(pa_bvals)),
                "--outdir", tmp]
        for key, value in extra.items():
            argv += ["--" + key.replace("_", "-"), str(value)]
        pi.main(argv)
        with open(os.path.join(tmp, "prep.json")) as fh:
            return json.load(fh)

    def test_acqparams_index_and_gradients_are_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = [5 if i % 33 == 0 else 1500 for i in range(99)]
            pa = [5 if i % 17 == 0 else 1500 for i in range(18)]
            prep = self._run(tmp, ap, pa)

            self.assertEqual(prep["n_volumes"], 117)
            self.assertTrue(prep["has_reverse_pe"])

            with open(os.path.join(tmp, "acqparams.txt")) as fh:
                acq = [line.split() for line in fh if line.strip()]
            with open(os.path.join(tmp, "topup_b0s.txt")) as fh:
                b0s = [int(v) for v in fh.read().split()]
            with open(os.path.join(tmp, "index.txt")) as fh:
                index = [int(v) for v in fh.read().split()]
            with open(os.path.join(tmp, "merged.bvals")) as fh:
                bvals = fh.read().split()

            # one acqparams row per b=0 handed to topup, in the same order
            self.assertEqual(len(acq), len(b0s))
            # index covers every volume of the merged series
            self.assertEqual(len(index), 117)
            self.assertEqual(len(bvals), 117)
            # every index value points at a real acqparams row
            self.assertTrue(all(1 <= v <= len(acq) for v in index))
            # forward volumes use a row with PE -1 in j, reverse volumes +1
            self.assertEqual(acq[index[0] - 1][1], "-1")
            self.assertEqual(acq[index[-1] - 1][1], "1")
            # the two directions are described by different rows
            self.assertEqual(sorted(set(index[:99])), [index[0]])
            self.assertNotEqual(index[0], index[-1])

    def test_matching_pe_directions_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = write_series(tmp, "ap", [5, 1500], "j-")
            pa = write_series(tmp, "pa", [5, 1500], "j-")
            with self.assertRaises(pi.PrepError):
                pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", ap[2],
                         "--nvols", "2",
                         "--rbvals", pa[0], "--rbvecs", pa[1], "--rjson", pa[2],
                         "--rnvols", "2", "--outdir", tmp])

    def test_volume_count_mismatch_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = write_series(tmp, "ap", [5, 1500, 1500], "j-")
            with self.assertRaises(pi.PrepError):
                pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", ap[2],
                         "--nvols", "99", "--outdir", tmp])

    def test_single_direction_still_produces_a_usable_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = write_series(tmp, "ap", [5, 1500, 1500, 5], "j-")
            pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", ap[2],
                     "--nvols", "4", "--outdir", tmp])
            with open(os.path.join(tmp, "prep.json")) as fh:
                prep = json.load(fh)
            self.assertFalse(prep["has_reverse_pe"])
            self.assertEqual(prep["index_values"], [1])

    def test_pe_direction_derived_from_the_siemens_csa_flags(self):
        """A DICOM dump has the axis and the sign in two separate fields."""
        ap = {"global": {"const": {"InPlanePhaseEncodingDirection": "COL",
                                   "CsaImage.PhaseEncodingDirectionPositive": 1,
                                   "CsaImage.BandwidthPerPixelPhaseEncode": 29.24}}}
        pa = {"global": {"const": {"InPlanePhaseEncodingDirection": "COL",
                                   "CsaImage.PhaseEncodingDirectionPositive": 0,
                                   "CsaImage.BandwidthPerPixelPhaseEncode": 29.24}}}
        vec_ap, trt, src = pi.series_pe(ap, "", None, "dwi")
        self.assertEqual(vec_ap, [0.0, 1.0, 0.0])
        self.assertEqual(src["pe_dir"], "j")
        self.assertEqual(src["pe_source"], "CsaImage.PhaseEncodingDirectionPositive")
        self.assertEqual(src["trt_source"], "BandwidthPerPixelPhaseEncode")
        self.assertAlmostEqual(trt, 1.0 / 29.24)

        vec_pa, _, _ = pi.series_pe(pa, "", None, "rdwi", "r")
        self.assertEqual(vec_pa, [0.0, -1.0, 0.0])
        # the pair is opposite, which is what topup needs
        self.assertNotEqual(vec_ap, vec_pa)

    def test_a_positive_flag_of_zero_is_not_read_as_missing(self):
        meta = {"InPlanePhaseEncodingDirection": "COL",
                "CsaImage.PhaseEncodingDirectionPositive": 0,
                "TotalReadoutTime": 0.0342}
        self.assertEqual(pi.series_pe(meta, "", None, "dwi")[2]["pe_dir"], "j-")

    def test_row_phase_encoding_gives_the_i_axis(self):
        meta = {"InPlanePhaseEncodingDirection": "ROW",
                "CsaImage.PhaseEncodingDirectionPositive": 1,
                "TotalReadoutTime": 0.0342}
        self.assertEqual(pi.series_pe(meta, "", None, "dwi")[0], [1.0, 0.0, 0.0])

    def test_a_bids_sidecar_still_wins_over_the_csa_flags(self):
        meta = {"PhaseEncodingDirection": "j-",
                "InPlanePhaseEncodingDirection": "COL",
                "CsaImage.PhaseEncodingDirectionPositive": 1,
                "TotalReadoutTime": 0.0342}
        src = pi.series_pe(meta, "", None, "dwi")[2]
        self.assertEqual(src["pe_dir"], "j-")
        self.assertEqual(src["pe_source"], "PhaseEncodingDirection")

    def test_config_wins_over_every_sidecar_field(self):
        meta = {"PhaseEncodingDirection": "j-", "TotalReadoutTime": 0.0342}
        vec, trt, src = pi.series_pe(meta, "i", 0.05, "dwi")
        self.assertEqual(vec, [1.0, 0.0, 0.0])
        self.assertEqual(trt, 0.05)
        self.assertEqual(src["pe_source"], "config.json")
        self.assertEqual(src["trt_source"], "config.json")

    def test_readout_time_sources_are_tried_in_order(self):
        cases = [
            ({"TotalReadoutTime": 0.0342}, "TotalReadoutTime", 0.0342),
            ({"EffectiveEchoSpacing": 0.0005, "ReconMatrixPE": 69},
             "EffectiveEchoSpacing", 0.034),
            ({"CsaImage.BandwidthPerPixelPhaseEncode": 29.24},
             "BandwidthPerPixelPhaseEncode", 1.0 / 29.24),
            ({"EstimatedTotalReadoutTime": 0.031},
             "EstimatedTotalReadoutTime", 0.031),
            ({"EstimatedEffectiveEchoSpacing": 0.0005, "ReconMatrixPE": 69},
             "EstimatedEffectiveEchoSpacing", 0.034),
        ]
        for extra, expected_source, expected_value in cases:
            meta = dict(extra, PhaseEncodingDirection="j-")
            _, trt, src = pi.series_pe(meta, "", None, "dwi")
            self.assertEqual(src["trt_source"], expected_source, extra)
            self.assertAlmostEqual(trt, expected_value, places=6)

    def test_an_unusable_readout_time_names_the_manual_escape_hatch(self):
        """A Philips sidecar carries none of the timing fields."""
        meta = {"PhaseEncodingDirection": "j-", "Manufacturer": "Philips"}
        with self.assertRaises(pi.PrepError) as ctx:
            pi.series_pe(meta, "", None, "rdwi", "r")
        self.assertIn("'rreadout_time'", str(ctx.exception))

    def test_a_nonsense_phase_encoding_axis_is_refused(self):
        meta = {"InPlanePhaseEncodingDirection": "DIAGONAL",
                "CsaImage.PhaseEncodingDirectionPositive": 1,
                "TotalReadoutTime": 0.0342}
        with self.assertRaises(pi.PrepError):
            pi.series_pe(meta, "", None, "dwi")

    def test_provenance_is_recorded_for_both_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            ap = write_series(tmp, "ap", [5, 1500], "j-")
            pa = write_series(tmp, "pa", [5, 1500], "j")
            pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", ap[2],
                     "--nvols", "2",
                     "--rbvals", pa[0], "--rbvecs", pa[1], "--rjson", pa[2],
                     "--rnvols", "2", "--outdir", tmp])
            with open(os.path.join(tmp, "prep.json")) as fh:
                entries = json.load(fh)["phase_encoding"]
            self.assertEqual([e["pe_dir"] for e in entries], ["j-", "j"])
            self.assertTrue(all(e["pe_source"] == "PhaseEncodingDirection"
                                for e in entries))
            self.assertTrue(all(e["readout_time_source"] == "TotalReadoutTime"
                                for e in entries))

    def test_nested_per_volume_sidecar_fields_are_found(self):
        """A DICOM dump nests the fields and repeats them once per volume."""
        with tempfile.TemporaryDirectory() as tmp:
            dump = os.path.join(tmp, "dic_param.json")
            with open(dump, "w") as fh:
                json.dump({"global": {"const": {"PhaseEncodingDirection": "j-"}},
                           "time": {"samples": {"TotalReadoutTime": [0.0342] * 2}}},
                          fh)
            ap = write_series(tmp, "ap", [5, 1500], "j-")
            pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", dump,
                     "--nvols", "2", "--outdir", tmp])
            with open(os.path.join(tmp, "acqparams.txt")) as fh:
                row = fh.read().split()
            self.assertEqual(row[:3], ["0", "-1", "0"])
            self.assertAlmostEqual(float(row[3]), 0.0342)

    def test_a_field_that_varies_per_volume_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = os.path.join(tmp, "dic_param.json")
            with open(dump, "w") as fh:
                json.dump({"PhaseEncodingDirection": "j-",
                           "TotalReadoutTime": [0.0342, 0.0400]}, fh)
            ap = write_series(tmp, "ap", [5, 1500], "j-")
            with self.assertRaises(pi.PrepError) as ctx:
                pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", dump,
                         "--nvols", "2", "--outdir", tmp])
            self.assertIn("volume 1", str(ctx.exception))

    def test_missing_pe_direction_names_the_real_config_keys(self):
        """The message must name a key config.json actually understands."""
        with tempfile.TemporaryDirectory() as tmp:
            bare = os.path.join(tmp, "bare.json")
            with open(bare, "w") as fh:
                json.dump({"TotalReadoutTime": 0.0342}, fh)
            ap = write_series(tmp, "ap", [5, 1500], "j-")
            pa = write_series(tmp, "pa", [5, 1500], "j")

            with self.assertRaises(pi.PrepError) as ctx:
                pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", bare,
                         "--nvols", "2", "--outdir", tmp])
            self.assertIn("'pe_dir'", str(ctx.exception))

            with self.assertRaises(pi.PrepError) as ctx:
                pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", ap[2],
                         "--nvols", "2",
                         "--rbvals", pa[0], "--rbvecs", pa[1], "--rjson", bare,
                         "--rnvols", "2", "--outdir", tmp])
            self.assertIn("'rpe_dir'", str(ctx.exception))

    def test_missing_readout_time_names_the_real_config_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare = os.path.join(tmp, "bare.json")
            with open(bare, "w") as fh:
                json.dump({"PhaseEncodingDirection": "j"}, fh)
            ap = write_series(tmp, "ap", [5, 1500], "j-")
            pa = write_series(tmp, "pa", [5, 1500], "j")

            with self.assertRaises(pi.PrepError) as ctx:
                pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", bare,
                         "--nvols", "2", "--outdir", tmp])
            self.assertIn("'readout_time'", str(ctx.exception))

            with self.assertRaises(pi.PrepError) as ctx:
                pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", ap[2],
                         "--nvols", "2",
                         "--rbvals", pa[0], "--rbvecs", pa[1], "--rjson", bare,
                         "--rnvols", "2", "--outdir", tmp])
            self.assertIn("'rreadout_time'", str(ctx.exception))

    def test_config_overrides_win_over_a_sidecar_without_pe_fields(self):
        """A raw DICOM dump carries none of the BIDS fields; the config fills in."""
        with tempfile.TemporaryDirectory() as tmp:
            bare = os.path.join(tmp, "bare.json")
            with open(bare, "w") as fh:
                json.dump({"Manufacturer": "Siemens"}, fh)
            ap = write_series(tmp, "ap", [5, 1500], "j-")
            pa = write_series(tmp, "pa", [5, 1500], "j")
            pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", bare,
                     "--pe-dir", "j-", "--readout-time", "0.0342",
                     "--nvols", "2",
                     "--rbvals", pa[0], "--rbvecs", pa[1], "--rjson", bare,
                     "--rpe-dir", "j", "--rreadout-time", "0.0342",
                     "--rnvols", "2", "--outdir", tmp])
            with open(os.path.join(tmp, "prep.json")) as fh:
                prep = json.load(fh)
            self.assertTrue(prep["has_reverse_pe"])
            vectors = [e["vector"] for e in prep["phase_encoding"]]
            self.assertEqual(vectors, [[0.0, -1.0, 0.0], [0.0, 1.0, 0.0]])

    def test_missing_readout_time_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bare.json")
            with open(path, "w") as fh:
                json.dump({"PhaseEncodingDirection": "j-"}, fh)
            ap = write_series(tmp, "ap", [5, 1500], "j-")
            with self.assertRaises(pi.PrepError):
                pi.main(["--bvals", ap[0], "--bvecs", ap[1], "--json", path,
                         "--nvols", "2", "--outdir", tmp])

    def test_readout_time_derived_from_echo_spacing(self):
        meta = {"PhaseEncodingDirection": "j-",
                "EffectiveEchoSpacing": 0.0005, "ReconMatrixPE": 69}
        vec, trt, _ = pi.series_pe(meta, "", None, "dwi")
        self.assertEqual(vec, [0.0, -1.0, 0.0])
        self.assertAlmostEqual(trt, 0.034, places=6)


if __name__ == "__main__":
    unittest.main()
