#!/usr/bin/env python3
"""Build an FSL ``eddy`` slice-specification (slspec) file from BIDS metadata.

This is a port of ``do_slspec.m`` from the original MATLAB pipeline, with two
differences that make it safe to run unattended inside a container:

* ``SliceTiming`` (seconds, BIDS) is the primary source.  Siemens
  ``CsaImage.MosaicRefAcqTimes`` (milliseconds) is still honoured when present
  so that legacy dcm2niix sidecars keep working.  Either may sit nested inside
  a DICOM parameter dump and be repeated once per volume; see ``sidecar.py``.
* Slice groups are formed by *equal acquisition time* rather than by reshaping
  the sort order blindly.  For a well formed multiband acquisition the two are
  identical, but grouping by time detects a non-uniform multiband factor and
  fails loudly instead of writing a silently wrong slspec.

The written file has one row per excitation (in temporal order) and one column
per simultaneously excited slice; slice indices are 0-based, as ``eddy``
expects.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from typing import List, Sequence, Tuple

import sidecar

# SliceTiming values are floats; times that differ by less than this are treated
# as the same excitation.  1 microsecond is far below any real slice spacing and
# far above JSON float round-tripping error.
TIME_TOLERANCE = 1e-6


class SlspecError(RuntimeError):
    """Raised when a usable slspec cannot be derived from the metadata."""


def read_slice_times(meta: dict) -> Tuple[List[float], str]:
    """Return (slice times in seconds, path of the field they came from)."""
    try:
        times, path = sidecar.find_list(meta, "SliceTiming")
        if times is not None:
            return [float(t) for t in times], path

        # Legacy Siemens sidecars: mosaic reference acquisition times, in ms.
        times, path = sidecar.find_list(meta, "CsaImage.MosaicRefAcqTimes",
                                        "MosaicRefAcqTimes")
        if times is not None:
            return [float(t) / 1000.0 for t in times], path
    except sidecar.SidecarError as exc:
        raise SlspecError(str(exc))

    raise SlspecError(
        "no SliceTiming (or CsaImage.MosaicRefAcqTimes) field anywhere in the "
        "sidecar; slice-to-volume correction needs one of them, or an explicit "
        "'slspec' file"
    )


def group_by_time(slice_times: Sequence[float]) -> List[List[int]]:
    """Group 0-based slice indices by acquisition time, earliest group first.

    Ties keep their natural slice order inside a group, which reproduces the
    stable sort used by the MATLAB implementation.
    """
    groups: "OrderedDict[float, List[int]]" = OrderedDict()
    for slice_index in sorted(range(len(slice_times)), key=lambda i: slice_times[i]):
        t = slice_times[slice_index]
        for key in groups:
            if abs(key - t) <= TIME_TOLERANCE:
                groups[key].append(slice_index)
                break
        else:
            groups[t] = [slice_index]
    return [sorted(v) for v in groups.values()]


def build_slspec(slice_times: Sequence[float]) -> Tuple[List[List[int]], int]:
    """Return (slspec rows, multiband factor)."""
    if not slice_times:
        raise SlspecError("slice timing list is empty")

    groups = group_by_time(slice_times)
    sizes = {len(g) for g in groups}
    if len(sizes) != 1:
        raise SlspecError(
            "slice timings do not describe a uniform multiband factor "
            f"(group sizes seen: {sorted(sizes)}). Supply an explicit slspec "
            "file via the 'slspec' input instead."
        )

    multiband_factor = sizes.pop()
    if len(slice_times) % multiband_factor:
        raise SlspecError(
            f"{len(slice_times)} slices is not divisible by the derived "
            f"multiband factor {multiband_factor}"
        )
    return groups, multiband_factor


def drop_slice(rows: List[List[int]], dropped: int, n_slices: int) -> List[List[int]]:
    """Renumber a slspec after one slice was cropped off the volume.

    ``dropped`` is the 0-based index of the removed slice.  Slices above it move
    down by one; the row that held the removed slice loses one column, which
    ``eddy`` rejects, so the caller is expected to fall back to ``--mb``/
    ``--mb_offs`` in that situation.  Kept here so the renumbering is available
    and testable.
    """
    if not 0 <= dropped < n_slices:
        raise SlspecError(f"dropped slice {dropped} outside 0..{n_slices - 1}")
    out = []
    for row in rows:
        new_row = [s - 1 if s > dropped else s for s in row if s != dropped]
        if new_row:
            out.append(new_row)
    return out


def format_slspec(rows: Sequence[Sequence[int]]) -> str:
    return "\n".join(" ".join("%3d" % s for s in row) for row in rows) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", required=True, help="BIDS/brainlife sidecar for the DWI series")
    ap.add_argument("--out", required=True, help="path of the slspec file to write")
    ap.add_argument("--mb-out", help="optional path to write the multiband factor to")
    ap.add_argument("--n-slices", type=int,
                    help="expected number of slices; mismatch is a hard error")
    args = ap.parse_args(argv)

    with open(args.json) as fh:
        meta = json.load(fh)

    slice_times, source = read_slice_times(meta)
    if args.n_slices is not None and len(slice_times) != args.n_slices:
        raise SlspecError(
            f"{source} has {len(slice_times)} entries but the image has "
            f"{args.n_slices} slices"
        )

    rows, multiband_factor = build_slspec(slice_times)
    with open(args.out, "w") as fh:
        fh.write(format_slspec(rows))
    if args.mb_out:
        with open(args.mb_out, "w") as fh:
            fh.write("%d\n" % multiband_factor)

    print(
        "slspec: %d slices, multiband factor %d, %d excitations (from %s)"
        % (len(slice_times), multiband_factor, len(rows), source),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SlspecError as exc:
        print("make_slspec: %s" % exc, file=sys.stderr)
        sys.exit(2)
