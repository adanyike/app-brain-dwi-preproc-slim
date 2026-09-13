#!/usr/bin/env python3
"""Generate a tiny synthetic AP/PA dataset for an end-to-end smoke test.

This is *not* a validation dataset -- the signal is a crude ellipsoid with
direction-dependent attenuation.  It exists so that `./run.sh` can be exercised
from stage 0 to stage 6 inside the container in a couple of minutes, catching
plumbing mistakes (wrong file names, mismatched volume counts, bad acqparams)
that unit tests cannot.

    python3 test/make_test_data.py --outdir test/input
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np
import nibabel as nib

RNG = np.random.default_rng(20240101)


def brain_phantom(shape):
    """An ellipsoidal 'brain' that bet can find, with some internal structure."""
    zz, yy, xx = np.meshgrid(*[np.linspace(-1, 1, n) for n in shape], indexing="ij")
    radius = (zz / 0.85) ** 2 + (yy / 0.75) ** 2 + (xx / 0.65) ** 2
    brain = np.clip(1.4 - radius, 0, None)
    brain[radius > 1] = 0
    # A couple of bright bands so the tensor fit has anisotropic structure.
    brain += 0.4 * np.exp(-((yy / 0.12) ** 2)) * (brain > 0)
    return brain.astype(np.float32)


def dropout(data, fraction=0.18):
    """Zero a chunk on one side of the head, as a signal dropout does.

    The smoke test needs this because a dropout is the one mask defect no repair
    can fix: the image is empty there too, so the coverage check has to report it
    as the data's problem rather than growing the mask over it.
    """
    shape = data.shape[:3]
    centre = [shape[0] // 4, shape[1] // 2, shape[2] // 2]
    radius = [max(2, int(fraction * n)) for n in shape]
    grid = np.meshgrid(*[np.arange(n) for n in shape], indexing="ij")
    inside = sum(((grid[axis] - centre[axis]) / float(radius[axis])) ** 2
                 for axis in range(3)) < 1.0
    data[inside] = 0.0
    return data


def synth_series(shape, bvals, bvecs, base):
    """Attenuate the phantom per volume so dtifit has something to fit."""
    n = len(bvals)
    data = np.zeros(shape + (n,), dtype=np.float32)
    # A fibre population running along y, so FA is non-trivial.
    fibre = np.array([0.0, 1.0, 0.0])
    for i in range(n):
        b = bvals[i]
        if b <= 50:
            volume = base * 1000.0
        else:
            direction = np.array([bvecs[0][i], bvecs[1][i], bvecs[2][i]])
            norm = np.linalg.norm(direction)
            direction = direction / norm if norm > 0 else fibre
            parallel = float(abs(np.dot(direction, fibre)))
            adc = 0.0003 + 0.0014 * (1.0 - parallel)   # mm^2/s
            volume = base * 1000.0 * np.exp(-b * adc)
        data[..., i] = volume + RNG.normal(0, 4, size=shape)
    return np.clip(data, 0, None)


def unit_directions(n):
    """Roughly uniform directions on the sphere (golden-angle spiral)."""
    golden = np.pi * (3 - np.sqrt(5))
    out = []
    for i in range(n):
        z = 1 - (2 * i + 1) / n
        r = np.sqrt(max(0.0, 1 - z * z))
        theta = golden * i
        out.append([r * np.cos(theta), r * np.sin(theta), z])
    return out


def gradient_table(n_directions, bvalues, n_b0, b0_every):
    """b=0 volumes interleaved through one set of directions per shell.

    A little jitter is added to each shell's b-value so the shell detection is
    exercised on values that do not all land on a round number, the way real
    scanner-written bvals do not.
    """
    directions = unit_directions(n_directions)
    plan = []
    for bval in bvalues:
        for i, direction in enumerate(directions):
            plan.append((float(bval) + (i % 5) - 2, direction))

    bvals, vecs = [], []
    taken = 0
    placed_b0 = 0
    while taken < len(plan) or placed_b0 < n_b0:
        if placed_b0 < n_b0 and len(bvals) % b0_every == 0:
            bvals.append(5.0)
            vecs.append([0.0, 0.0, 0.0])
            placed_b0 += 1
        elif taken < len(plan):
            bval, direction = plan[taken]
            bvals.append(bval)
            vecs.append(direction)
            taken += 1
        else:
            break
    return bvals, [[v[i] for v in vecs] for i in range(3)]


def slice_timing(n_slices, multiband, tr=4.0):
    """Interleaved multiband slice order, as SliceTiming seconds."""
    if n_slices % multiband:
        raise ValueError("slice count must be divisible by the multiband factor")
    per_band = n_slices // multiband
    order = list(range(0, per_band, 2)) + list(range(1, per_band, 2))
    times = [0.0] * n_slices
    for position, first in enumerate(order):
        for band in range(multiband):
            times[first + band * per_band] = round(position * tr / per_band, 6)
    return times


def write_atlas(directory, n_labels, shape=(16, 16, 12), voxel=2.0):
    """A stand-in FA template and label image for the atlas registration stage.

    The real ones ship with FSL; these exist so the dry run can exercise stages
    4 and 5 without a neuroimaging toolchain.  Labels are scattered across the
    grid so that every one of them survives resampling into the subject's space
    and no ROI comes out empty by construction.
    """
    os.makedirs(directory, exist_ok=True)
    affine = np.diag([voxel, voxel, voxel, 1.0])

    template = brain_phantom(shape)
    template = (template / max(template.max(), 1e-6)).astype(np.float32)
    nib.save(nib.Nifti1Image(template, affine),
             os.path.join(directory, "template_fa.nii.gz"))

    labels = (np.arange(int(np.prod(shape))) % n_labels + 1).astype(np.int16)
    nib.save(nib.Nifti1Image(labels.reshape(shape), affine),
             os.path.join(directory, "atlas_labels.nii.gz"))


def shipped_label_count(default=50):
    """How many ROIs templates/JHU-ICBM-labels.json describes."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "templates", "JHU-ICBM-labels.json")
    try:
        with open(path) as fh:
            return len(json.load(fh)["labels"])
    except (OSError, ValueError, KeyError):
        return default


def write_series(directory, name, data, bvals, bvecs, sidecar, voxel=2.0):
    os.makedirs(directory, exist_ok=True)
    affine = np.diag([voxel, voxel, voxel, 1.0])
    nib.save(nib.Nifti1Image(data, affine), os.path.join(directory, name + ".nii.gz"))
    with open(os.path.join(directory, name + ".bvals"), "w") as fh:
        fh.write(" ".join("%g" % b for b in bvals) + "\n")
    with open(os.path.join(directory, name + ".bvecs"), "w") as fh:
        for row in bvecs:
            fh.write(" ".join("%.6f" % v for v in row) + "\n")
    with open(os.path.join(directory, name + ".json"), "w") as fh:
        json.dump(sidecar, fh, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outdir", default="test/input")
    ap.add_argument("--size", type=int, default=32, help="in-plane matrix")
    ap.add_argument("--slices", type=int, default=12)
    ap.add_argument("--multiband", type=int, default=2)
    ap.add_argument("--directions", type=int, default=12)
    ap.add_argument("--rdirections", type=int, default=3)
    ap.add_argument("--shells", default="1500",
                    help="comma-separated shell b-values, e.g. 1500,3000")
    ap.add_argument("--dropout", action="store_true",
                    help="zero a chunk of the forward series, as a signal dropout "
                         "does, so the brain-mask coverage check has something "
                         "unrepairable to report")
    args = ap.parse_args()

    shape = (args.size, args.size, args.slices)
    base = brain_phantom(shape)
    timing = slice_timing(args.slices, args.multiband)

    shells = [float(v) for v in args.shells.split(",")]
    fwd_bvals, fwd_bvecs = gradient_table(args.directions, shells, n_b0=3, b0_every=5)
    rev_bvals, rev_bvecs = gradient_table(args.rdirections, shells[:1], n_b0=2, b0_every=3)

    common = {
        "SliceTiming": timing,
        "TotalReadoutTime": 0.0342,
        "RepetitionTime": 4.0,
        "EchoTime": 0.09,
        "MultibandAccelerationFactor": args.multiband,
        "Manufacturer": "synthetic",
    }

    forward = synth_series(shape, fwd_bvals, fwd_bvecs, base)
    if args.dropout:
        forward = dropout(forward)
    write_series(os.path.join(args.outdir, "dwi"), "dwi", forward,
                 fwd_bvals, fwd_bvecs,
                 dict(common, PhaseEncodingDirection="j-"))
    write_series(os.path.join(args.outdir, "rdwi"), "dwi",
                 synth_series(shape, rev_bvals, rev_bvecs, base),
                 rev_bvals, rev_bvecs,
                 dict(common, PhaseEncodingDirection="j"))

    write_atlas(os.path.join(args.outdir, "atlas"), shipped_label_count())

    print("wrote %d forward and %d reverse volumes of %dx%dx%d to %s"
          % (len(fwd_bvals), len(rev_bvals), *shape, args.outdir))


if __name__ == "__main__":
    main()
