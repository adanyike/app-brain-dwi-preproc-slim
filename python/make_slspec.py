#!/usr/bin/env python3
"""Build an FSL ``eddy`` slice-specification (slspec) file from BIDS metadata.

Two things make this safe to run unattended inside a container:

* ``SliceTiming`` (seconds, BIDS) is the primary source.  Siemens
  ``CsaImage.MosaicRefAcqTimes`` (milliseconds) is honoured when present, so
  older dcm2niix sidecars work too.  Either may sit nested inside a DICOM
  parameter dump and be repeated once per volume; see ``sidecar.py``.
* Slice groups are formed by *equal acquisition time* rather than by reshaping
  the sort order blindly.  For a well formed multiband acquisition the two are
  identical, but grouping by time detects a non-uniform multiband factor and
  fails loudly instead of writing a silently wrong slspec.

Where the sidecar carries no timings at all -- some Philips exports -- the
excitation order can instead be *declared* from the protocol and built by
``generate_slspec``.  That is an assertion about the acquisition, not a
measurement of it, so it is opt-in and the caller is expected to cross-check it
against ``SliceTiming`` wherever one is available.

The written file has one row per excitation (in temporal order) and one column
per simultaneously excited slice; slice indices are 0-based, as ``eddy``
expects.
"""

from __future__ import annotations

import argparse
import json
import math
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

    Ties keep their natural slice order inside a group.
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


# Excitation orders a scanner protocol can specify.  "philips_default" is the
# one to treat with suspicion: Philips's default mode interleaves with a step of
# about sqrt(slices per package) rather than the step of 2 everyone else means
# by "interleaved", and the exact rounding is not something this code can verify
# against the data.  Check it against the protocol printout before trusting it,
# or give --slice-step explicitly.
SLICE_ORDERS = ("ascending", "descending", "interleaved", "rev_interleaved",
                "philips_default", "step")


def step_order(length: int, step: int) -> List[int]:
    """Indices 0..length-1 visited in `step`-interleaved order.

    step=1 is ascending, step=2 is the usual odd/even interleave.
    """
    if step < 1:
        raise SlspecError("slice step must be at least 1, got %d" % step)
    out: List[int] = []
    for start in range(step):
        out.extend(range(start, length, step))
    return out


def package_order(length: int, order: str, step: int | None = None) -> List[int]:
    """The order in which one package's slices are excited."""
    if order == "ascending":
        return list(range(length))
    if order == "descending":
        return list(range(length - 1, -1, -1))
    if order == "interleaved":
        return step_order(length, 2)
    if order == "rev_interleaved":
        return list(reversed(step_order(length, 2)))
    if order == "philips_default":
        return step_order(length, max(1, int(round(math.sqrt(length)))))
    if order == "step":
        if step is None:
            raise SlspecError("slice order 'step' needs an explicit slice step")
        return step_order(length, step)
    raise SlspecError("unknown slice order %r; choose one of %s"
                      % (order, ", ".join(SLICE_ORDERS)))


def generate_slspec(n_slices: int, multiband: int = 1, packages: int = 1,
                    order: str = "ascending", step: int | None = None
                    ) -> Tuple[List[List[int]], int]:
    """Build a slspec from declared protocol parameters rather than timings.

    Simultaneously excited slices are assumed to be evenly spaced through the
    volume, which is how multiband acquisitions are laid out: with M bands the
    slices excited together are n/M apart.  Within one band the slices are
    split into `packages` contiguous groups and `order` is applied inside each,
    the groups being taken in turn.

    Nothing here is checked against the data -- it cannot be.  Compare the
    result against a derived slspec whenever the sidecar has timings.
    """
    if n_slices < 1:
        raise SlspecError("slice count must be positive, got %d" % n_slices)
    if multiband < 1:
        raise SlspecError("multiband factor must be at least 1, got %d" % multiband)
    if packages < 1:
        raise SlspecError("package count must be at least 1, got %d" % packages)
    if n_slices % multiband:
        raise SlspecError("%d slices is not divisible by the multiband factor %d"
                          % (n_slices, multiband))

    per_band = n_slices // multiband
    if per_band % packages:
        raise SlspecError(
            "%d slices per band is not divisible by %d package(s)"
            % (per_band, packages)
        )
    per_package = per_band // packages

    first_band: List[int] = []
    for package in range(packages):
        first_band.extend(package * per_package + s
                          for s in package_order(per_package, order, step))

    rows = [[s + band * per_band for band in range(multiband)] for s in first_band]
    return rows, multiband


def format_slspec(rows: Sequence[Sequence[int]]) -> str:
    return "\n".join(" ".join("%3d" % s for s in row) for row in rows) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", help="BIDS/brainlife sidecar for the DWI series")
    ap.add_argument("--slice-order", choices=SLICE_ORDERS,
                    help="build the slspec from this declared excitation order "
                         "instead of from the sidecar's timings")
    ap.add_argument("--multiband", type=int, default=1,
                    help="multiband factor, when generating (default 1)")
    ap.add_argument("--packages", type=int, default=1,
                    help="number of Philips packages, when generating (default 1)")
    ap.add_argument("--slice-step", type=int,
                    help="interleave step for --slice-order step")
    ap.add_argument("--out", required=True, help="path of the slspec file to write")
    ap.add_argument("--mb-out", help="optional path to write the multiband factor to")
    ap.add_argument("--n-slices", type=int,
                    help="number of slices; required when generating, and a "
                         "mismatch is a hard error when deriving")
    args = ap.parse_args(argv)

    if bool(args.json) == bool(args.slice_order):
        raise SlspecError("give either --json (derive from timings) or "
                          "--slice-order (generate from the protocol), not both")

    if args.slice_order:
        if args.n_slices is None:
            raise SlspecError("--n-slices is required when generating a slspec")
        rows, multiband_factor = generate_slspec(
            args.n_slices, multiband=args.multiband, packages=args.packages,
            order=args.slice_order, step=args.slice_step,
        )
        n_slices = args.n_slices
        source = ("declared slice order '%s'%s, multiband %d, %d package(s)"
                  % (args.slice_order,
                     "" if args.slice_step is None else " step %d" % args.slice_step,
                     args.multiband, args.packages))
    else:
        with open(args.json) as fh:
            meta = json.load(fh)

        slice_times, source = read_slice_times(meta)
        if args.n_slices is not None and len(slice_times) != args.n_slices:
            raise SlspecError(
                f"{source} has {len(slice_times)} entries but the image has "
                f"{args.n_slices} slices"
            )
        rows, multiband_factor = build_slspec(slice_times)
        n_slices = len(slice_times)

    with open(args.out, "w") as fh:
        fh.write(format_slspec(rows))
    if args.mb_out:
        with open(args.mb_out, "w") as fh:
            fh.write("%d\n" % multiband_factor)

    print(
        "slspec: %d slices, multiband factor %d, %d excitations (from %s)"
        % (n_slices, multiband_factor, len(rows), source),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SlspecError as exc:
        print("make_slspec: %s" % exc, file=sys.stderr)
        sys.exit(2)
