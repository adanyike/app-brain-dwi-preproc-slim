#!/usr/bin/env python3
"""Unit tests for python/make_slspec.py."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import make_slspec as ms  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REFERENCE = os.path.join(HERE, "..", "templates", "philips_84_slices_slspec.txt")


def read_slspec(path):
    with open(path) as fh:
        return [[int(v) for v in line.split()] for line in fh if line.strip()]


class TestBuildSlspec(unittest.TestCase):
    def test_matches_the_reference_philips_file(self):
        """SliceTiming reconstructed from the shipped slspec round-trips."""
        reference = read_slspec(REFERENCE)
        n_slices = sum(len(row) for row in reference)
        times = [0.0] * n_slices
        for group_index, row in enumerate(reference):
            for slice_index in row:
                times[slice_index] = group_index * 0.0714

        rows, multiband = ms.build_slspec(times)
        self.assertEqual(rows, reference)
        self.assertEqual(multiband, 4)
        self.assertEqual(len(rows), 21)

    def test_single_band_interleaved(self):
        times = [0.0, 0.3, 0.1, 0.4, 0.2]      # 5 slices, interleaved, no MB
        rows, multiband = ms.build_slspec(times)
        self.assertEqual(multiband, 1)
        self.assertEqual(rows, [[0], [2], [4], [1], [3]])

    def test_multiband_two(self):
        times = [0.0, 0.1, 0.0, 0.1]           # slices 0/2 then 1/3
        rows, multiband = ms.build_slspec(times)
        self.assertEqual(multiband, 2)
        self.assertEqual(rows, [[0, 2], [1, 3]])

    def test_uneven_groups_are_rejected(self):
        with self.assertRaises(ms.SlspecError):
            ms.build_slspec([0.0, 0.0, 0.0, 0.1])

    def test_empty_is_rejected(self):
        with self.assertRaises(ms.SlspecError):
            ms.build_slspec([])


class TestReadSliceTimes(unittest.TestCase):
    def test_prefers_slice_timing(self):
        times, source = ms.read_slice_times(
            {"SliceTiming": [0.0, 0.1], "CsaImage.MosaicRefAcqTimes": [0.0, 500.0]})
        self.assertEqual(source, "SliceTiming")
        self.assertEqual(times, [0.0, 0.1])

    def test_falls_back_to_siemens_mosaic_times_in_ms(self):
        times, source = ms.read_slice_times({"CsaImage.MosaicRefAcqTimes": [0.0, 500.0]})
        self.assertEqual(source, "CsaImage.MosaicRefAcqTimes")
        self.assertEqual(times, [0.0, 0.5])

    def test_missing_timing_raises(self):
        with self.assertRaises(ms.SlspecError):
            ms.read_slice_times({"RepetitionTime": 2.0})


class TestDropSlice(unittest.TestCase):
    def test_renumbers_slices_above_the_dropped_one(self):
        rows = [[0, 2], [1, 3]]
        self.assertEqual(ms.drop_slice(rows, 0, 4), [[1], [0, 2]])


class TestCli(unittest.TestCase):
    def test_slice_count_mismatch_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = os.path.join(tmp, "dwi.json")
            with open(sidecar, "w") as fh:
                json.dump({"SliceTiming": [0.0, 0.1]}, fh)
            with self.assertRaises(ms.SlspecError):
                ms.main(["--json", sidecar, "--out", os.path.join(tmp, "s.txt"),
                         "--n-slices", "84"])

    def test_writes_slspec_and_multiband_factor(self):
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = os.path.join(tmp, "dwi.json")
            with open(sidecar, "w") as fh:
                json.dump({"SliceTiming": [0.0, 0.1, 0.0, 0.1]}, fh)
            out, mb = os.path.join(tmp, "s.txt"), os.path.join(tmp, "mb.txt")
            self.assertEqual(ms.main(["--json", sidecar, "--out", out,
                                      "--mb-out", mb, "--n-slices", "4"]), 0)
            self.assertEqual(read_slspec(out), [[0, 2], [1, 3]])
            with open(mb) as fh:
                self.assertEqual(fh.read().strip(), "2")


class DicomDumpSidecarTests(unittest.TestCase):
    """A raw DICOM parameter dump nests the timings and repeats them per volume."""

    # 23 excitations, multiband 4 -> 92 slices, with the decimal round-trip
    # jitter a real dump shows (697.49999999 in one volume, 697.50000001 in
    # the next).
    EXCITATIONS = [0.0, 1675.0, 140.0, 1815.0, 280.0, 1955.0, 420.0, 2092.5,
                   557.5, 2232.5, 697.49999999, 2372.5, 837.49999999, 2512.5,
                   977.49999999, 2652.5, 1117.49999999, 2792.5, 1257.49999999,
                   2929.99999999, 1395.0, 3069.99999999, 1535.0]

    def volumes(self, n):
        first = self.EXCITATIONS * 4
        jittered = [t + 1e-8 if t else 0.0 for t in self.EXCITATIONS] * 4
        return [first if i % 2 == 0 else jittered for i in range(n)]

    def write(self, tmp, meta):
        path = os.path.join(tmp, "dic_param.json")
        with open(path, "w") as fh:
            json.dump(meta, fh)
        return path

    def test_nested_per_volume_mosaic_times_yield_a_slspec(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = {"global": {"const": {"Manufacturer": "Siemens"}},
                    "time": {"samples":
                             {"CsaImage.MosaicRefAcqTimes": self.volumes(99)}}}
            out = os.path.join(tmp, "slspec.txt")
            mb = os.path.join(tmp, "mb.txt")
            ms.main(["--json", self.write(tmp, meta), "--out", out,
                     "--mb-out", mb, "--n-slices", "92"])
            rows = [line.split() for line in open(out).read().splitlines()]
            self.assertEqual(len(rows), 23)
            self.assertTrue(all(len(r) == 4 for r in rows))
            self.assertEqual(open(mb).read().strip(), "4")
            # every slice appears exactly once
            self.assertEqual(sorted(int(s) for r in rows for s in r),
                             list(range(92)))

    def test_slice_count_mismatch_is_still_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = {"time": {"samples":
                             {"CsaImage.MosaicRefAcqTimes": self.volumes(2)}}}
            with self.assertRaises(ms.SlspecError):
                ms.main(["--json", self.write(tmp, meta),
                         "--out", os.path.join(tmp, "slspec.txt"),
                         "--n-slices", "60"])

    def test_timings_that_vary_between_volumes_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            volumes = self.volumes(2)
            volumes[1] = list(reversed(volumes[1]))
            meta = {"time": {"samples":
                             {"CsaImage.MosaicRefAcqTimes": volumes}}}
            with self.assertRaises(ms.SlspecError) as ctx:
                ms.main(["--json", self.write(tmp, meta),
                         "--out", os.path.join(tmp, "slspec.txt")])
            self.assertIn("volume 1", str(ctx.exception))

    def test_a_sidecar_with_no_timings_anywhere_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = {"global": {"const": {"Manufacturer": "Siemens"}}}
            with self.assertRaises(ms.SlspecError) as ctx:
                ms.main(["--json", self.write(tmp, meta),
                         "--out", os.path.join(tmp, "slspec.txt")])
            self.assertIn("slspec", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
