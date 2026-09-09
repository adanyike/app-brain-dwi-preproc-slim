#!/usr/bin/env python3
"""Derive the FSL topup/eddy bookkeeping files for a brain DWI pair.

Given the forward-phase-encoded DWI series (and, optionally, its reverse-phase
-encoded counterpart) this writes:

  acqparams.txt   one row per b=0 volume handed to ``topup --imain``
  index.txt       one entry per volume of the merged series, pointing at the
                  acqparams row that describes its phase-encoding
  bvals / bvecs   the merged gradient table (FSL layout)
  topup_b0s.txt   0-based volume indices of the b=0 images selected for topup
  prep.json       everything the shell stages need to know

The original MATLAB pipeline read pre-existing ``.acqparams``/``.index`` files
and then rewrote them with a study-specific heuristic.  Here they are built
directly from the BIDS sidecars (``PhaseEncodingDirection`` and
``TotalReadoutTime``), which is what brainlife uploads alongside the NIfTI, so
no hand-made bookkeeping file has to travel with the data.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Sequence

import sidecar

from shells import detect_shells

# BIDS axis codes map onto the first three columns of an FSL acqparams row.
PE_AXIS = {"i": 0, "j": 1, "k": 2, "x": 0, "y": 1, "z": 2}


def warn(message: str) -> None:
    print("prepare_inputs: WARNING: %s" % message, file=sys.stderr)


class PrepError(RuntimeError):
    """Raised when the inputs cannot be turned into a consistent eddy setup."""


def read_bvals(path: str) -> List[float]:
    with open(path) as fh:
        vals = fh.read().split()
    if not vals:
        raise PrepError("%s is empty" % path)
    return [float(v) for v in vals]


def read_bvecs(path: str) -> List[List[float]]:
    """Return bvecs as 3 rows x N columns, accepting either FSL layout."""
    with open(path) as fh:
        rows = [line.split() for line in fh if line.strip()]
    if not rows:
        raise PrepError("%s is empty" % path)
    grid = [[float(v) for v in r] for r in rows]
    if len(grid) == 3:
        width = {len(r) for r in grid}
        if len(width) != 1:
            raise PrepError("%s has ragged rows" % path)
        return grid
    if all(len(r) == 3 for r in grid):  # N rows x 3 columns -> transpose
        return [[r[i] for r in grid] for i in range(3)]
    raise PrepError("%s is not a 3xN or Nx3 bvecs file" % path)


def write_bvals(path: str, bvals: Sequence[float]) -> None:
    with open(path, "w") as fh:
        fh.write(" ".join("%g" % b for b in bvals) + "\n")


def write_bvecs(path: str, bvecs: Sequence[Sequence[float]]) -> None:
    with open(path, "w") as fh:
        for row in bvecs:
            fh.write(" ".join("%.10f" % v for v in row) + "\n")


def pe_vector(pe_dir: str) -> List[float]:
    """Turn a BIDS PhaseEncodingDirection string into an acqparams triplet."""
    if not pe_dir:
        raise PrepError("phase-encoding direction is empty")
    code = pe_dir.strip()
    sign = -1.0 if code.endswith("-") else 1.0
    axis = code[0].lower()
    if axis not in PE_AXIS:
        raise PrepError("unrecognised PhaseEncodingDirection %r" % pe_dir)
    vec = [0.0, 0.0, 0.0]
    vec[PE_AXIS[axis]] = sign
    return vec


def load_meta(path: str | None) -> dict:
    if not path:
        return {}
    with open(path) as fh:
        return json.load(fh)


def pe_from_csa(field) -> str | None:
    """Build a BIDS phase-encoding code from the Siemens CSA fields.

    A DICOM dump has no ``PhaseEncodingDirection``, but it carries the two
    halves separately: ``InPlanePhaseEncodingDirection`` (DICOM 0018,1312) gives
    the axis as COL/ROW, and ``CsaImage.PhaseEncodingDirectionPositive`` gives
    the sign as 1/0.

    Note the sign convention.  ``dcm2niix`` reports an axial Siemens AP series
    as ``j-``, whereas the CSA flag reads 1 (positive) for that same series, so
    this derivation yields ``j`` where a BIDS sidecar would say ``j-``.  That
    is a global flip of both series, which leaves topup's correction unchanged:
    the estimated field flips with it and eddy applies both consistently.  What
    has to be right is the *axis*, and that the forward and reverse series come
    out opposite -- both of which this preserves.
    """
    axis_code = field("InPlanePhaseEncodingDirection",
                      "CsaImage.InPlanePhaseEncodingDirection")
    positive = field("CsaImage.PhaseEncodingDirectionPositive",
                     "PhaseEncodingDirectionPositive")
    if axis_code is None or positive is None:
        return None

    axis = {"COL": "j", "ROW": "i"}.get(str(axis_code).strip().upper())
    if axis is None:
        raise PrepError(
            "unrecognised InPlanePhaseEncodingDirection %r; expected COL or ROW"
            % (axis_code,)
        )
    return axis if int(positive) else axis + "-"


def resolve_pe_dir(field, pe_override: str) -> tuple[str | None, str]:
    """Return (BIDS phase-encoding code, where it came from)."""
    if pe_override:
        return pe_override, "config.json"

    pe_dir = field("PhaseEncodingDirection", "PhaseEncodingAxis")
    if pe_dir:
        return pe_dir, "PhaseEncodingDirection"

    pe_dir = pe_from_csa(field)
    if pe_dir:
        return pe_dir, "CsaImage.PhaseEncodingDirectionPositive"
    return None, ""


def resolve_readout_time(field, trt_override: float | None) -> tuple[float | None, str]:
    """Return (total readout time in seconds, where it came from).

    Tried in order of how directly the sidecar states it.  Every source is a
    constant multiple of the others, and the fourth acqparams column only sets
    the scale of the field topup estimates -- a scale eddy then divides back
    out -- so the choice does not change the corrected data as long as *both*
    series use the same one.  ``main`` warns when they do not.
    """
    if trt_override is not None:
        return float(trt_override), "config.json"

    trt = field("TotalReadoutTime")
    if trt is not None:
        return float(trt), "TotalReadoutTime"

    ees = field("EffectiveEchoSpacing")
    matrix_pe = field("ReconMatrixPE")
    if ees and matrix_pe:
        # TotalReadoutTime = EffectiveEchoSpacing * (ReconMatrixPE - 1)
        return float(ees) * (float(matrix_pe) - 1.0), "EffectiveEchoSpacing"

    # Siemens CSA (0019,1028).  1/BandwidthPerPixelPhaseEncode is the readout
    # duration including the final echo spacing, so it is larger than the
    # EffectiveEchoSpacing form above by ReconMatrixPE/(ReconMatrixPE - 1) --
    # a fraction of a percent at any realistic matrix size, and a common
    # factor across both series.
    bwppe = field("CsaImage.BandwidthPerPixelPhaseEncode",
                  "BandwidthPerPixelPhaseEncode")
    if bwppe:
        return 1.0 / float(bwppe), "BandwidthPerPixelPhaseEncode"

    # dcm2niix falls back to these names when it could only estimate the timing.
    trt = field("EstimatedTotalReadoutTime")
    if trt is not None:
        return float(trt), "EstimatedTotalReadoutTime"

    ees = field("EstimatedEffectiveEchoSpacing")
    if ees and matrix_pe:
        return float(ees) * (float(matrix_pe) - 1.0), "EstimatedEffectiveEchoSpacing"

    return None, ""


def series_pe(meta: dict, pe_override: str, trt_override: float | None,
              label: str, cfg_prefix: str = "") -> tuple[List[float], float, dict]:
    """Resolve phase-encoding vector and readout time for one series.

    ``cfg_prefix`` is the config.json key prefix for this series ("" for the
    forward series, "r" for the reverse one) so the error messages name the
    key the user actually has to set.  The third return value records which
    field each answer came from, for the provenance in prep.json.
    """
    def field(*names):
        try:
            return sidecar.find_scalar(meta, *names)[0]
        except sidecar.SidecarError as exc:
            raise PrepError("%s: %s" % (label, exc))

    pe_dir, pe_source = resolve_pe_dir(field, pe_override)
    if not pe_dir:
        raise PrepError(
            "%s: could not determine the phase-encoding direction. The sidecar "
            "has no PhaseEncodingDirection, and no InPlanePhaseEncodingDirection "
            "+ CsaImage.PhaseEncodingDirectionPositive to derive it from; set "
            "'%spe_dir' in config.json (a BIDS code: i, i-, j, j-, k or k-)"
            % (label, cfg_prefix)
        )

    trt, trt_source = resolve_readout_time(field, trt_override)
    if trt is None:
        raise PrepError(
            "%s: could not determine the total readout time. The sidecar has "
            "none of TotalReadoutTime, EffectiveEchoSpacing+ReconMatrixPE or "
            "BandwidthPerPixelPhaseEncode; set '%sreadout_time' in config.json "
            "(seconds). Philips sidecars usually need this."
            % (label, cfg_prefix)
        )
    if trt <= 0:
        raise PrepError("%s: total readout time is %g, which cannot be right"
                        % (label, trt))

    sources = {"pe_dir": pe_dir, "pe_source": pe_source, "trt_source": trt_source}
    return pe_vector(pe_dir), float(trt), sources


def evenly_spaced(items: Sequence[int], k: int) -> List[int]:
    """Pick ``k`` entries spread across ``items`` (endpoints included)."""
    n = len(items)
    if k >= n:
        return list(items)
    if k == 1:
        return [items[0]]
    picks = {round(i * (n - 1) / (k - 1)) for i in range(k)}
    return [items[i] for i in sorted(picks)]


def select_topup_b0s(bvals: Sequence[float], series_of_volume: Sequence[int],
                     b0_threshold: float, b0_per_pedir: int,
                     reduce_above: int) -> List[int]:
    """Choose which b=0 volumes go into ``topup --imain``.

    Mirrors the intent of the original MATLAB block: use every b=0 when there
    are only a handful, otherwise thin them down to a small balanced set spread
    across the acquisition so topup stays fast and well conditioned.
    """
    b0s = [i for i, b in enumerate(bvals) if b <= b0_threshold]
    if not b0s:
        raise PrepError(
            "no volume has b <= %g; check the bvals file or lower "
            "'b0_threshold'" % b0_threshold
        )
    if len(b0s) < reduce_above:
        return b0s

    selected: List[int] = []
    for series in sorted(set(series_of_volume)):
        in_series = [i for i in b0s if series_of_volume[i] == series]
        if in_series:
            selected.extend(evenly_spaced(in_series, b0_per_pedir))
    return sorted(selected)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bvals", required=True)
    ap.add_argument("--bvecs", required=True)
    ap.add_argument("--json", help="sidecar for the forward series")
    ap.add_argument("--pe-dir", default="", help="override PhaseEncodingDirection, e.g. j-")
    ap.add_argument("--readout-time", type=float, help="override TotalReadoutTime, seconds")
    ap.add_argument("--nvols", type=int, required=True, help="volumes in the forward series")

    ap.add_argument("--rbvals")
    ap.add_argument("--rbvecs")
    ap.add_argument("--rjson")
    ap.add_argument("--rpe-dir", default="")
    ap.add_argument("--rreadout-time", type=float)
    ap.add_argument("--rnvols", type=int, default=0)

    ap.add_argument("--b0-threshold", type=float, default=50.0)
    ap.add_argument("--b0-per-pedir", type=int, default=2)
    ap.add_argument("--reduce-b0-above", type=int, default=8,
                    help="use every b=0 for topup while there are fewer than this many")
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args(argv)

    bvals = read_bvals(args.bvals)
    bvecs = read_bvecs(args.bvecs)
    if len(bvals) != args.nvols or len(bvecs[0]) != args.nvols:
        raise PrepError(
            "forward series: image has %d volumes but bvals/bvecs have %d/%d"
            % (args.nvols, len(bvals), len(bvecs[0]))
        )

    pe_fwd, trt_fwd, src_fwd = series_pe(load_meta(args.json), args.pe_dir,
                                         args.readout_time, "dwi")
    series_pe_rows = [pe_fwd + [trt_fwd]]
    series_sources = [src_fwd]
    series_of_volume = [0] * args.nvols

    if args.rnvols:
        if not (args.rbvals and args.rbvecs):
            raise PrepError("reverse series given without its bvals/bvecs")
        rbvals = read_bvals(args.rbvals)
        rbvecs = read_bvecs(args.rbvecs)
        if len(rbvals) != args.rnvols or len(rbvecs[0]) != args.rnvols:
            raise PrepError(
                "reverse series: image has %d volumes but bvals/bvecs have %d/%d"
                % (args.rnvols, len(rbvals), len(rbvecs[0]))
            )
        pe_rev, trt_rev, src_rev = series_pe(load_meta(args.rjson), args.rpe_dir,
                                             args.rreadout_time, "rdwi", "r")
        series_sources.append(src_rev)
        if src_rev["trt_source"] != src_fwd["trt_source"]:
            # Each source is a slightly different definition of the same
            # readout.  A common factor cancels; two different ones leave a
            # real ratio error between the acqparams rows.
            warn("the two series take their readout time from different fields "
                 "(%s vs %s); set 'readout_time'/'rreadout_time' explicitly if "
                 "topup looks wrong"
                 % (src_fwd["trt_source"], src_rev["trt_source"]))
        if pe_rev == pe_fwd:
            raise PrepError(
                "both series report the same phase-encoding vector %s; topup "
                "needs opposing directions. Check the sidecars, or set "
                "'pe_dir'/'rpe_dir' in config.json" % (pe_fwd,)
            )
        series_pe_rows.append(pe_rev + [trt_rev])
        series_of_volume += [1] * args.rnvols
        bvals = bvals + rbvals
        bvecs = [bvecs[i] + rbvecs[i] for i in range(3)]

    topup_b0s = select_topup_b0s(bvals, series_of_volume, args.b0_threshold,
                                 args.b0_per_pedir, args.reduce_b0_above)

    # acqparams gets one row per selected b=0, in the order they appear in the
    # 4D file that topup receives.  eddy indexes every volume onto the first
    # acqparams row that carries its own phase-encoding.
    acq_rows = [series_pe_rows[series_of_volume[v]] for v in topup_b0s]
    first_row_for_series = {}
    for row_number, volume in enumerate(topup_b0s, start=1):
        first_row_for_series.setdefault(series_of_volume[volume], row_number)
    for series in sorted(set(series_of_volume)):
        if series not in first_row_for_series:
            raise PrepError(
                "series %d contributed no b=0 volume to topup; it cannot be "
                "distortion corrected" % series
            )
    index = [first_row_for_series[s] for s in series_of_volume]

    out = args.outdir.rstrip("/")
    with open("%s/acqparams.txt" % out, "w") as fh:
        for row in acq_rows:
            fh.write("%g %g %g %g\n" % tuple(row))
    with open("%s/index.txt" % out, "w") as fh:
        fh.write(" ".join(str(i) for i in index) + "\n")
    with open("%s/topup_b0s.txt" % out, "w") as fh:
        fh.write("\n".join(str(v) for v in topup_b0s) + "\n")
    write_bvals("%s/merged.bvals" % out, bvals)
    write_bvecs("%s/merged.bvecs" % out, bvecs)

    shells = [s.as_dict() for s in detect_shells(bvals, args.b0_threshold)]
    prep = {
        "n_volumes": len(bvals),
        "n_volumes_forward": args.nvols,
        "n_volumes_reverse": args.rnvols,
        "has_reverse_pe": bool(args.rnvols),
        "phase_encoding": [
            {"series": i, "vector": r[:3], "total_readout_time": r[3],
             "pe_dir": series_sources[i]["pe_dir"],
             "pe_source": series_sources[i]["pe_source"],
             "readout_time_source": series_sources[i]["trt_source"]}
            for i, r in enumerate(series_pe_rows)
        ],
        "b0_threshold": args.b0_threshold,
        "min_bval": min(bvals),
        "n_b0_total": sum(1 for b in bvals if b <= args.b0_threshold),
        "n_b0_for_topup": len(topup_b0s),
        "topup_b0_volumes": topup_b0s,
        "acqparams_rows": len(acq_rows),
        "index_values": sorted(set(index)),
        "shells": shells,
    }
    with open("%s/prep.json" % out, "w") as fh:
        json.dump(prep, fh, indent=2)

    for i, src in enumerate(series_sources):
        print(
            "prepare_inputs: series %d: pe_dir %s (from %s), readout %.5gs "
            "(from %s)"
            % (i, src["pe_dir"], src["pe_source"], series_pe_rows[i][3],
               src["trt_source"]),
            file=sys.stderr,
        )
    print(
        "prepare_inputs: %d volumes (%d forward + %d reverse), %d b=0 total, "
        "%d used for topup, %d acqparams rows"
        % (prep["n_volumes"], args.nvols, args.rnvols, prep["n_b0_total"],
           len(topup_b0s), len(acq_rows)),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PrepError as exc:
        print("prepare_inputs: %s" % exc, file=sys.stderr)
        sys.exit(2)
