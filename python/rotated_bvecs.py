#!/usr/bin/env python3
"""Clean the gradient directions ``eddy`` writes back after rotating them.

An unweighted volume has no gradient direction, and scanners record that as
``0 0 0`` alongside a b-value that is frequently a small non-zero number -- 5 is
common -- rather than exactly zero.  Rotating a zero-length vector and
renormalising it produces NaN, which MRtrix then refuses::

    Corrupt content in bvecs/bvals data
    (NaN bvec direction but non-zero value in bval)

Restoring ``0 0 0`` for those volumes is not a guess: it is the only meaningful
direction for a volume that carries no diffusion weighting.  A non-finite
direction on a genuinely diffusion-weighted volume means something else has
gone wrong, and is refused rather than repaired.
"""

from __future__ import annotations

import argparse
import math
import sys
from typing import List, Sequence, Tuple


class BvecError(RuntimeError):
    """Raised when a gradient table cannot be made usable."""


def read_bvecs(path: str) -> List[List[float]]:
    with open(path) as fh:
        rows = [[float(v) for v in line.split()] for line in fh if line.strip()]
    if len(rows) != 3:
        raise BvecError("%s has %d rows; an FSL bvecs file has 3" % (path, len(rows)))
    if len({len(r) for r in rows}) != 1:
        raise BvecError("%s has rows of differing length" % path)
    return rows


def read_bvals(path: str) -> List[float]:
    with open(path) as fh:
        return [float(v) for v in fh.read().split()]


def format_bvecs(rows: Sequence[Sequence[float]]) -> str:
    return "\n".join(" ".join("%.6f" % v for v in row) for row in rows) + "\n"


def sanitise(bvecs: Sequence[Sequence[float]], bvals: Sequence[float],
             b0_threshold: float = 50.0) -> Tuple[List[List[float]], List[int]]:
    """Return (cleaned bvecs, indices of the volumes that were cleaned)."""
    n = len(bvecs[0])
    if len(bvals) != n:
        raise BvecError("%d gradient directions but %d b-values" % (n, len(bvals)))

    out = [list(row) for row in bvecs]
    repaired: List[int] = []
    for i in range(n):
        direction = [out[axis][i] for axis in range(3)]
        if all(math.isfinite(v) for v in direction):
            continue
        if bvals[i] > b0_threshold:
            raise BvecError(
                "volume %d has a non-finite gradient direction (%s) but b=%g, "
                "which is above the b=0 threshold of %g -- eddy's rotated "
                "bvecs cannot be repaired without inventing a direction"
                % (i, direction, bvals[i], b0_threshold)
            )
        for axis in range(3):
            out[axis][i] = 0.0
        repaired.append(i)
    return out, repaired


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bvecs", required=True, help="rotated bvecs written by eddy")
    ap.add_argument("--bvals", required=True, help="the matching b-values")
    ap.add_argument("--out", required=True, help="where to write the cleaned bvecs")
    ap.add_argument("--b0-threshold", type=float, default=50.0,
                    help="b-values at or below this carry no gradient direction")
    args = ap.parse_args(argv)

    bvecs = read_bvecs(args.bvecs)
    bvals = read_bvals(args.bvals)
    cleaned, repaired = sanitise(bvecs, bvals, args.b0_threshold)

    with open(args.out, "w") as fh:
        fh.write(format_bvecs(cleaned))

    if repaired:
        print("rotated_bvecs: restored 0 0 0 for %d unweighted volume(s) whose "
              "rotated direction came back non-finite (%s)"
              % (len(repaired), ", ".join(str(i) for i in repaired)),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BvecError as exc:
        print("rotated_bvecs: %s" % exc, file=sys.stderr)
        sys.exit(2)
