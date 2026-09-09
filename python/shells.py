#!/usr/bin/env python3
"""Detect the b-value shells in an acquisition and pick the one to fit.

`dtifit` fits a monoexponential tensor, which is only valid over a limited
b-value range.  On a multi-shell acquisition you therefore fit one shell plus
the b=0 volumes, and which shell that should be depends on the data: for
0/1500/3000 the usual choice is 1500, because the b=3000 signal has already
departed from the Gaussian regime.

The shells are found from the bvals rather than assumed, so `dtifit_shell` can
say *what you want* instead of *what number to type*:

    all       fit every volume (no extraction)
    lowest    the lowest diffusion-weighted shell, whatever it turns out to be
    highest   the highest diffusion-weighted shell
    1500      that specific shell, if the data actually contains it

Volumes are grouped into shells by single-linkage clustering of the sorted
b-values: a gap wider than the tolerance starts a new shell.  Each shell is
represented by the mean of its members, which is what MRtrix's own shell
matching compares against.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Sequence

ALL_ALIASES = {"all", "all_bvalues", "all_bvals", "0", "none", ""}
LOWEST_ALIASES = {"lowest", "low", "lower", "min", "minimum"}
HIGHEST_ALIASES = {"highest", "high", "higher", "max", "maximum"}


class ShellError(RuntimeError):
    """Raised when the requested shell cannot be satisfied by the data."""


def nominal_bvalue(mean: float) -> int:
    """Snap a shell's mean b-value to the number people actually call it.

    Scanners write jittered b-values (1498, 1501, 1502 ...), so the raw mean of
    a b=1500 shell can come out at 1499 -- which then shows up in provenance and
    filenames as "b=1499".  Shells at or above 100 are rounded to the nearest
    50; low baselines are kept as they are, since the difference between b=0 and
    b=5 is worth seeing.  The rounding is far finer than the gap between real
    shells, so it never changes which shell a value matches.
    """
    if mean < 100:
        return int(round(mean))
    return int(round(mean / 50.0) * 50)


class Shell:
    def __init__(self, values: Sequence[float], indices: Sequence[int], is_b0: bool):
        self.values = list(values)
        self.indices = list(indices)
        self.is_b0 = is_b0
        self.mean = sum(values) / len(values)
        self.b = nominal_bvalue(self.mean)

    def as_dict(self) -> dict:
        return {
            "b": self.b,
            "mean_bval": round(self.mean, 2),
            "n_volumes": len(self.indices),
            "min_bval": min(self.values),
            "max_bval": max(self.values),
            "is_b0": self.is_b0,
        }

    def __repr__(self) -> str:
        kind = "b=0" if self.is_b0 else "DW"
        return "<Shell b=%d n=%d %s>" % (self.b, len(self.indices), kind)


def read_bvals(path: str) -> List[float]:
    with open(path) as fh:
        values = fh.read().split()
    if not values:
        raise ShellError("%s is empty" % path)
    return [float(v) for v in values]


def detect_shells(bvals: Sequence[float], b0_threshold: float = 50.0,
                  tolerance: float = 100.0) -> List[Shell]:
    """Group b-values into shells, lowest first."""
    if not bvals:
        raise ShellError("no b-values given")

    order = sorted(range(len(bvals)), key=lambda i: bvals[i])
    clusters: List[List[int]] = [[order[0]]]
    for index in order[1:]:
        if bvals[index] - bvals[clusters[-1][-1]] > tolerance:
            clusters.append([index])
        else:
            clusters[-1].append(index)

    shells = []
    for cluster in clusters:
        values = [bvals[i] for i in cluster]
        mean = sum(values) / len(values)
        shells.append(Shell(values, sorted(cluster), is_b0=mean <= b0_threshold))
    return shells


def diffusion_shells(shells: Sequence[Shell]) -> List[Shell]:
    return [s for s in shells if not s.is_b0]


def b0_shells(shells: Sequence[Shell]) -> List[Shell]:
    return [s for s in shells if s.is_b0]


def describe(shells: Sequence[Shell]) -> str:
    return ", ".join("b=%d (%d vol%s)" % (s.b, len(s.indices),
                                          "" if len(s.indices) == 1 else "s")
                     for s in shells)


def resolve(shells: Sequence[Shell], selection, tolerance: float = 100.0) -> dict:
    """Turn a `dtifit_shell` setting into a concrete extraction plan."""
    text = str(selection).strip().lower()

    if text in ALL_ALIASES:
        total = sum(len(s.indices) for s in shells)
        return {"mode": "all", "label": "all", "shell": None,
                "extract_arg": None, "n_volumes": total,
                "reason": "every volume is fitted"}

    weighted = diffusion_shells(shells)
    baselines = b0_shells(shells)

    if not weighted:
        raise ShellError(
            "the data contains no diffusion-weighted shell (only %s); nothing "
            "to fit a tensor to" % describe(shells))

    if text in LOWEST_ALIASES:
        chosen = weighted[0]
        reason = "lowest of %d diffusion-weighted shell(s): %s" % (
            len(weighted), describe(weighted))
    elif text in HIGHEST_ALIASES:
        chosen = weighted[-1]
        reason = "highest of %d diffusion-weighted shell(s): %s" % (
            len(weighted), describe(weighted))
    else:
        try:
            wanted = float(text)
        except ValueError:
            raise ShellError(
                "dtifit_shell must be a b-value, 'all', 'lowest' or 'highest'; "
                "got %r" % selection)
        chosen = min(weighted, key=lambda s: abs(s.b - wanted))
        if abs(chosen.b - wanted) > tolerance:
            raise ShellError(
                "no shell near b=%g in this data. Shells present: %s. Either "
                "name one of those, or use 'lowest'/'highest'/'all'."
                % (wanted, describe(shells)))
        reason = "requested b=%g, matched shell b=%d" % (wanted, chosen.b)

    if not baselines:
        raise ShellError(
            "fitting a single shell needs b=0 volumes as a baseline, but no "
            "shell falls at or below the b=0 threshold. Shells present: %s. "
            "Raise 'b0_threshold' if your lowest shell is the baseline, or set "
            "dtifit_shell to 'all'." % describe(shells))

    # dwiextract takes a comma-separated list of shell b-values; every b=0
    # shell goes in so the baseline is never dropped.
    wanted_bs = [s.b for s in baselines] + [chosen.b]
    n_volumes = sum(len(s.indices) for s in baselines) + len(chosen.indices)
    return {
        "mode": "single",
        "label": str(chosen.b),
        "shell": chosen.b,
        "extract_arg": ",".join(str(b) for b in wanted_bs),
        "n_volumes": n_volumes,
        "reason": reason,
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bvals", required=True)
    ap.add_argument("--select", default="all",
                    help="all | lowest | highest | a b-value such as 1500")
    ap.add_argument("--b0-threshold", type=float, default=50.0)
    ap.add_argument("--tolerance", type=float, default=100.0,
                    help="b-value gap that starts a new shell")
    ap.add_argument("--out", help="write the full shell report here as JSON")
    args = ap.parse_args(argv)

    bvals = read_bvals(args.bvals)
    shells = detect_shells(bvals, args.b0_threshold, args.tolerance)
    plan = resolve(shells, args.select, args.tolerance)

    report = {
        "b0_threshold": args.b0_threshold,
        "tolerance": args.tolerance,
        "n_volumes": len(bvals),
        "shells": [s.as_dict() for s in shells],
        "requested": str(args.select),
        "resolved": plan,
    }
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2)

    print("shells: detected %s" % describe(shells), file=sys.stderr)
    print("shells: %s -> %s (%d of %d volumes)"
          % (args.select, plan["reason"], plan["n_volumes"], len(bvals)),
          file=sys.stderr)

    # stdout is the machine-readable answer: "all", or the dwiextract argument.
    print(plan["extract_arg"] if plan["mode"] == "single" else "all")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ShellError as exc:
        print("shells: %s" % exc, file=sys.stderr)
        sys.exit(2)
