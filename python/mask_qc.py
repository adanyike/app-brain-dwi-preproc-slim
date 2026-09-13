#!/usr/bin/env python3
"""Check that a brain mask really covers the brain -- and repair it when it does not.

`bet` sometimes returns a mask with a bite taken out of it, or one that stops
short of the temporal lobes: a signal dropout, a spike or a bias field moves the
intensity it thresholds on.  Nothing downstream notices.  The stage-1 mask is
what `eddy --mask` is given, where a clipped mask biases the Gaussian-process
predictions and the outlier detection -- so it corrupts the corrected data
*everywhere*, not only near the defect -- and it also bounds `eddy_quad`'s
voxel-wise metrics, so a bad mask flatters its own QC.  The stage-3 mask bounds
`dtifit` and every ROI average.

Three independent criteria mark a voxel as missing brain, and the report records
which of them fired.  They are independent on purpose, because each is blind to
a case the others catch:

* **mirror** -- reflect the mask about its own centroid along the left-right
  axis.  Where the other hemisphere has brain and this side does not, something
  was removed.  A bite is unilateral and compact; real anatomical asymmetry is
  diffuse and a few percent.
* **hull band** -- inside the *union* of the three directional span fills and
  within a few voxels of the mask.  It must be the union: the *intersection* of
  the fills is the orthogonal convex hull, and a bite that reaches the mask
  boundary lies outside that hull, so an intersection test recovers nothing of
  exactly the defect it was written for.  The distance band is what keeps the
  neck, the eyes and skull marrow from sweeping in.
* **truncation** -- the per-slice mask area against a moving median. A mask that
  stops while the image still has tissue is the one defect neither of the others
  sees: a flat cut is bilateral, so the mirror finds nothing, and the tissue
  beyond the cut is outside every span fill, so the hull band does not reach it.

A fourth measurement is deliberately *not* a criterion. Bright voxels in the band
just outside the mask ("the boundary cuts through tissue") sounds like the most
direct test of all, and on a b=0 EPI it fires on every subject: the scalp, the
orbital fat and the skull marrow are all bright, so a correct mask is surrounded
by bright voxels by construction. It is recorded as `boundary_through_tissue` for
the eye, and never drives the verdict.

The same reasoning limits the mirror test, which would otherwise map the brain
onto the scalp on the other side of the head: its deficit is intersected with the
span-fill union -- a bite is inside the head's own extent along at least one
axis, the opposite scalp is not -- and thinned, so a one-voxel mismatch from
reflecting a curved surface about a rounded centroid cannot accumulate into a
percentage.

Candidates are then split in two, because they call for opposite responses:

* `missing_bright` -- there is signal there and the mask excluded it.  BET's
  fault, and repairable.
* `missing_dark` -- image and mask are both empty there.  The *data's* fault:
  growing a mask over a dropout adds voxels with no signal in them, which is
  worse than leaving them out.  Reported, never repaired; eddy's outlier
  replacement is the thing that addresses it.

Pure numpy and nibabel: the system interpreter in the container has no scipy and
no matplotlib, so every morphological operation here is written out, and the
montage is written with `zlib` and `struct` from the standard library.  That is
not only a constraint -- a PNG written by the same interpreter the tests run
under is a PNG the tests can decode and assert on.

Usage:

    mask_qc.py check  --image meanb0.nii.gz --mask mask.nii.gz --out report.json
    mask_qc.py repair --image meanb0.nii.gz --mask mask.nii.gz \\
                      --permissive permissive_mask.nii.gz --report report.json
    mask_qc.py figure --image meanb0.nii.gz --mask mask.nii.gz \\
                      --report report.json --out overlay.png

`check` prints its verdict (`ok`, `suspicious`, `implausible`) on stdout so the
shell can branch on it, the way stage 3 branches on `shells.py`.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import nibabel as nib

# The band around the mask within which a hull deficit counts, and the band
# outside it searched for tissue the boundary cut through. Two voxels at the
# 1.5-2.5 mm resolutions of a DWI acquisition.
BAND = 2
SHELL = 2

# Localisation block: generous in plane, shallow through slices, because a
# dropout takes out part of a slab rather than a cube.
BLOCK = (8, 8, 4)

# A human brain, masked on a b=0 image (so including CSF and some surrounding
# signal), between a child's and a large adult's with room to spare. Applied
# only when the field of view is itself head-sized -- see plausible_fov().
MIN_BRAIN_ML = 250.0
MAX_BRAIN_ML = 2500.0

# A head-sized field of view, in litres. A phantom or a test fixture falls
# outside this and the absolute volume check is skipped with a recorded reason.
MIN_FOV_ML = 800.0
MAX_FOV_ML = 12000.0

# A mask that ends at more than this fraction of its widest slice ended abruptly.
# A brain's superior and inferior extremes are a few voxels across; half the
# widest slice is not an end, it is a cut.
ABRUPT_END_FRACTION = 0.4

VERDICTS = ("ok", "suspicious", "implausible")


class MaskQcError(RuntimeError):
    pass


# ------------------------------------------------------------- geometry ----

def voxel_volume_mm3(affine: np.ndarray) -> float:
    return float(abs(np.linalg.det(np.asarray(affine)[:3, :3])))


def axis_of(affine: np.ndarray, codes: Sequence[str]) -> int | None:
    """The array axis whose direction is one of `codes` ('L'/'R', 'S'/'I', ...).

    Returns None when the affine is oblique enough that nibabel cannot name the
    axis -- in which case the caller records why it skipped the test rather than
    mirroring along an axis that is not left-right.
    """
    try:
        orientation = nib.aff2axcodes(np.asarray(affine))
    except Exception:                                   # pragma: no cover
        return None
    for index, code in enumerate(orientation):
        if code in codes:
            return index
    return None


def to_world(affine: np.ndarray, voxel: Sequence[float]) -> List[float]:
    point = np.asarray(affine) @ np.array([voxel[0], voxel[1], voxel[2], 1.0])
    return [round(float(v), 1) for v in point[:3]]


# -------------------------------------------------------- morphology ----

def _shift_or(dst: np.ndarray, src: np.ndarray, axis: int, step: int) -> None:
    """dst |= src shifted by `step` along `axis`, *without wrapping*.

    np.roll would wrap, which would make a mask touching the bottom slice
    appear to touch the top one as well -- and silently invalidate the
    field-of-view contact metric, which is the one thing saying "no repair can
    recover data that was never acquired".
    """
    dst_index: List[Any] = [slice(None)] * dst.ndim
    src_index: List[Any] = [slice(None)] * dst.ndim
    if step > 0:
        dst_index[axis] = slice(step, None)
        src_index[axis] = slice(None, -step)
    else:
        dst_index[axis] = slice(None, step)
        src_index[axis] = slice(-step, None)
    dst[tuple(dst_index)] |= src[tuple(src_index)]


def dilate(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    """6-connected dilation, no wrap-around."""
    out = np.asarray(mask, dtype=bool).copy()
    for _ in range(max(0, int(iterations))):
        grown = out.copy()
        for axis in range(out.ndim):
            _shift_or(grown, out, axis, 1)
            _shift_or(grown, out, axis, -1)
        out = grown
    return out


def erode(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    """Erosion as the complement of the dilation of the complement.

    Voxels outside the array are *not* treated as background, so a mask running
    off the edge of the field of view keeps its boundary there -- which is what
    the overlay should draw.
    """
    return ~dilate(~np.asarray(mask, dtype=bool), iterations)


def boundary(mask: np.ndarray) -> np.ndarray:
    mask = np.asarray(mask, dtype=bool)
    return mask & ~erode(mask, 1)


def thicken(flag: np.ndarray, iterations: int = 1) -> np.ndarray:
    """Keep only the clusters of `flag` at least `2 * iterations + 1` voxels thick.

    Erode to the cores, then dilate the cores back but never outside the
    original -- morphological reconstruction, the cheap version. A missing chunk
    is a chunk; a one-voxel skin over half the mask surface is an artefact of
    comparing a curved surface with its own reflection.
    """
    flag = np.asarray(flag, dtype=bool)
    core = erode(flag, iterations)
    if not core.any():
        return np.zeros_like(flag)
    out = core
    for _ in range(2 * iterations):
        out = dilate(out, 1) & flag
    return out


def span_fill(mask: np.ndarray, axis: int) -> np.ndarray:
    """Fill between the first and last set voxel along `axis`."""
    mask = np.asarray(mask, dtype=bool)
    forward = np.cumsum(mask, axis=axis) > 0
    backward = np.flip(np.cumsum(np.flip(mask, axis=axis), axis=axis) > 0, axis=axis)
    return forward & backward


def span_fill_union(mask: np.ndarray) -> np.ndarray:
    """Union of the three directional span fills.

    Union, not intersection: the intersection is the orthogonal convex hull, and
    a bite open to the outside is not inside it. Verified as a regression test.
    """
    out = np.zeros_like(np.asarray(mask, dtype=bool))
    for axis in range(np.asarray(mask).ndim):
        out |= span_fill(mask, axis)
    return out


def span_fill_intersection(mask: np.ndarray) -> np.ndarray:
    """Intersection of the three fills: mask plus voxels enclosed on all axes."""
    out = np.ones_like(np.asarray(mask, dtype=bool))
    for axis in range(np.asarray(mask).ndim):
        out &= span_fill(mask, axis)
    return out


def mirror_about_centroid(mask: np.ndarray, axis: int) -> np.ndarray:
    """The mask reflected about its own centroid along `axis`.

    About the mask's centroid rather than the array centre, because a head is
    rarely centred in the field of view and an off-centre reflection would
    report the offset as missing brain on one side.
    """
    mask = np.asarray(mask, dtype=bool)
    positions = np.nonzero(mask)
    if not len(positions[0]):
        return np.zeros_like(mask)
    centre = float(np.mean(positions[axis]))
    length = mask.shape[axis]
    source = np.rint(2.0 * centre - np.arange(length)).astype(int)
    valid = (source >= 0) & (source < length)

    moved = np.moveaxis(mask, axis, 0)
    out = np.zeros_like(moved)
    out[np.nonzero(valid)[0]] = moved[source[valid]]
    return np.moveaxis(out, 0, axis)


def block_sums(flag: np.ndarray, block: Sequence[int]) -> np.ndarray:
    """Sum `flag` over non-overlapping blocks with np.add.reduceat."""
    out = np.asarray(flag).astype(np.int64)
    for axis, size in enumerate(block):
        starts = np.arange(0, out.shape[axis], max(1, int(size)))
        out = np.add.reduceat(out, starts, axis=axis)
    return out


# ------------------------------------------------------------ measures ----

def tissue_threshold(image: np.ndarray, mask: np.ndarray) -> float:
    """A robust "there is signal here" level, from the intensities inside the mask.

    Half the in-mask median: brain on a b=0 image is an order of magnitude above
    background, so the exact fraction does not matter, while a mean would be
    dragged by the bright CSF the mask usually includes.
    """
    inside = np.asarray(image)[np.asarray(mask, dtype=bool)]
    if inside.size == 0:
        positive = np.asarray(image)[np.asarray(image) > 0]
        return float(0.5 * np.median(positive)) if positive.size else 0.0
    return float(0.5 * np.median(inside))


def slice_profile(mask: np.ndarray, bright: np.ndarray, missing: np.ndarray,
                  axis: int) -> Dict[str, Any]:
    """Per-slice areas: the mask, the tissue, and the brain the mask is missing.

    The curve is what a reader actually looks at -- a bite is a dip in one, a
    truncation a cliff -- and it is small enough to carry on the task page.
    """
    other = tuple(a for a in range(np.asarray(mask).ndim) if a != axis)
    return {
        "axis": int(axis),
        "mask_area": [int(v) for v in np.asarray(mask, dtype=bool).sum(axis=other)],
        "tissue_area": [int(v) for v in np.asarray(bright, dtype=bool).sum(axis=other)],
        "missing_area": [int(v) for v in np.asarray(missing, dtype=bool).sum(axis=other)],
    }


def moving_median(values: Sequence[float], window: int = 5) -> List[float]:
    out = []
    half = max(1, window // 2)
    for index in range(len(values)):
        lo, hi = max(0, index - half), min(len(values), index + half + 1)
        out.append(float(np.median(values[lo:hi])))
    return out


def truncation(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Slices where the mask stops although the brain plainly does not.

    Two shapes of the same defect: a slice inside the mask's span collapsing
    against its neighbours (`interior`), and the mask *ending* at close to its
    full cross-section (`boundary`) -- a brain tapers to nothing at the vertex
    and at the foramen magnum, so a mask that stops at half its widest slice
    stopped early.

    Both are judged on the mask's own area profile, never on how much bright
    tissue lies beyond the end. On a b=0 EPI there is always bright tissue beyond
    the end of the brain -- the scalp -- so a test phrased that way flags every
    subject. Measured against a moving median, not the global maximum, so the
    normal taper does not read as a step.
    """
    area = np.asarray(profile["mask_area"], dtype=float)
    tissue = np.asarray(profile["tissue_area"], dtype=float)
    present = np.nonzero(area > 0)[0]
    result: Dict[str, Any] = {"slices": [], "kinds": []}
    if not present.size:
        return result

    first, last = int(present[0]), int(present[-1])
    smooth = moving_median(area.tolist())

    interior = []
    for index in range(first, last + 1):
        reference = smooth[index]
        if reference <= 0:
            continue
        # A real dip: less than half the local typical area, while that slice
        # still holds at least as much tissue as the mask usually covers there.
        if area[index] < 0.5 * reference and tissue[index] > 0.8 * reference:
            interior.append(index)

    widest = float(area.max()) or 1.0
    boundary_slices = []
    for end in (first, last):
        if area[end] / widest > ABRUPT_END_FRACTION:
            boundary_slices.append(int(end))

    if interior:
        result["kinds"].append("interior")
    if boundary_slices:
        result["kinds"].append("boundary")
    result["slices"] = sorted(set(interior + boundary_slices))
    return result


def fov_contact(mask: np.ndarray) -> Dict[str, Any]:
    """How much of the mask sits on a face of the field of view.

    A brain that touches the edge of the acquisition was clipped by the
    acquisition, and no repair can recover what was never sampled -- so this
    exists to stop the report blaming BET for the scanner.
    """
    mask = np.asarray(mask, dtype=bool)
    faces: Dict[str, int] = {}
    names = (("x_low", "x_high"), ("y_low", "y_high"), ("z_low", "z_high"))
    for axis in range(mask.ndim):
        low: List[Any] = [slice(None)] * mask.ndim
        high: List[Any] = [slice(None)] * mask.ndim
        low[axis] = 0
        high[axis] = mask.shape[axis] - 1
        faces[names[axis][0]] = int(mask[tuple(low)].sum())
        faces[names[axis][1]] = int(mask[tuple(high)].sum())
    total = int(sum(faces.values()))
    return {"voxels": total,
            "faces": {name: count for name, count in faces.items() if count},
            "fraction": round(total / float(max(1, mask.sum())), 4)}


def centroid(flag: np.ndarray) -> np.ndarray | None:
    positions = np.nonzero(np.asarray(flag, dtype=bool))
    if not len(positions[0]):
        return None
    return np.array([float(np.mean(p)) for p in positions])


def plausible_fov(shape: Sequence[int], voxel_mm3: float) -> bool:
    """Is this field of view head-sized, so that absolute volumes mean anything?

    The synthetic fixtures the tests and dry runs use are a few centimetres
    across; applying a brain-volume range to them would fail every run for the
    wrong reason.
    """
    litres = float(np.prod(shape)) * voxel_mm3 / 1000.0
    return MIN_FOV_ML <= litres <= MAX_FOV_ML


# -------------------------------------------------------------- assess ----

def assess(image: np.ndarray, mask: np.ndarray, affine: np.ndarray,
           label: str = "mask", warn_fraction: float = 0.01,
           block: Sequence[int] = BLOCK, band: int = BAND,
           min_block_voxels: int = 32) -> Dict[str, Any]:
    """Measure how well `mask` covers the brain in `image`, and return a verdict."""
    image = np.asarray(image, dtype=np.float64)
    if image.ndim == 4:
        image = image.mean(axis=3)
    mask = np.asarray(mask) > 0
    if image.shape != mask.shape:
        raise MaskQcError("image %s and mask %s are different shapes"
                          % (image.shape, mask.shape))

    voxel_mm3 = voxel_volume_mm3(affine)
    n_mask = int(mask.sum())
    notes: List[str] = []
    report: Dict[str, Any] = {
        "label": label,
        "shape": [int(v) for v in mask.shape],
        "voxel_volume_mm3": round(voxel_mm3, 4),
        "n_voxels": n_mask,
        "volume_ml": round(n_mask * voxel_mm3 / 1000.0, 1),
        "warn_fraction": float(warn_fraction),
    }

    if n_mask == 0:
        report.update({"verdict": "implausible", "repairable": False,
                       "reasons": ["the mask is empty"], "notes": notes})
        return report

    threshold = tissue_threshold(image, mask)
    bright = image > threshold
    report["tissue_threshold"] = round(threshold, 4)
    report["intensity_p995"] = round(float(np.percentile(image[mask], 99.5)), 4)

    # --- the three independent criteria ---
    lr_axis = axis_of(affine, ("L", "R"))
    if lr_axis is None:
        mirrored = np.zeros_like(mask)
        notes.append("no left-right axis could be named from the affine, so the "
                     "mirror test was skipped")
    else:
        mirrored = mirror_about_centroid(mask, lr_axis) & ~mask

    inside_hull = span_fill_union(mask) & ~mask
    hull_band = inside_hull & dilate(mask, band)
    mirror_deficit = thicken(mirrored & inside_hull)

    candidates = mirror_deficit | hull_band
    missing_bright = candidates & bright
    missing_dark = candidates & ~bright
    # Recorded, never a verdict: see the module docstring.
    shell = dilate(mask, SHELL) & ~mask & bright

    n_bright = int(missing_bright.sum())
    n_dark = int(missing_dark.sum())
    report["missing"] = {
        "bright_voxels": n_bright,
        "bright_fraction": round(n_bright / float(n_mask), 4),
        "bright_volume_ml": round(n_bright * voxel_mm3 / 1000.0, 2),
        "dark_voxels": n_dark,
        "dark_fraction": round(n_dark / float(n_mask), 4),
        "by_criterion": {
            "mirror": int((mirror_deficit & bright).sum()),
            "hull_band": int((hull_band & bright).sum()),
        },
    }
    report["boundary_through_tissue"] = {
        "voxels": int(shell.sum()),
        "fraction": round(int(shell.sum()) / float(n_mask), 4),
    }

    # --- localisation: which chunk, and where in the world ---
    missing_blocks = block_sums(missing_bright, block)
    covered_blocks = block_sums(mask, block)
    total = missing_blocks + covered_blocks
    with np.errstate(invalid="ignore", divide="ignore"):
        fractions = np.where(total >= min_block_voxels,
                             missing_blocks / np.maximum(total, 1), 0.0)
    worst = {"fraction": 0.0}
    if fractions.size and fractions.max() > 0:
        index = np.unravel_index(int(np.argmax(fractions)), fractions.shape)
        centre = [(index[axis] + 0.5) * block[axis] for axis in range(len(block))]
        worst = {
            "fraction": round(float(fractions[index]), 4),
            "voxels": int(missing_blocks[index]),
            "block": [int(v) for v in index],
            "centre_mm": to_world(affine, centre),
        }
    report["worst_block"] = worst

    # --- truncation, holes, field of view, centroid ---
    slice_axis = axis_of(affine, ("S", "I"))
    if slice_axis is None:
        slice_axis = mask.ndim - 1
        notes.append("no superior-inferior axis could be named from the affine, "
                     "so the last array axis was profiled")
    profile = slice_profile(mask, bright, missing_bright, slice_axis)
    report["slice_profile"] = profile
    report["truncation"] = truncation(profile)

    holes = span_fill_intersection(mask) & ~mask
    n_holes = int(holes.sum())
    report["holes"] = {"voxels": n_holes,
                       "fraction": round(n_holes / float(n_mask), 4)}

    report["fov_contact"] = fov_contact(mask)

    mask_centre = centroid(mask)
    image_centre = centroid(bright)
    if mask_centre is not None and image_centre is not None:
        offset_voxels = mask_centre - image_centre
        offset_mm = np.asarray(affine)[:3, :3] @ offset_voxels
        extent = float(np.mean(np.asarray(mask.shape)))
        report["centroid_offset"] = {
            "mm": round(float(np.linalg.norm(offset_mm)), 2),
            "fraction_of_fov": round(float(np.linalg.norm(offset_voxels) / extent), 4),
        }
    else:
        report["centroid_offset"] = {"mm": 0.0, "fraction_of_fov": 0.0}

    # --- verdict ---
    reasons: List[str] = []
    fov_fraction = n_mask / float(np.prod(mask.shape))
    report["fov_fraction"] = round(fov_fraction, 4)

    implausible: List[str] = []
    if plausible_fov(mask.shape, voxel_mm3):
        if report["volume_ml"] < MIN_BRAIN_ML:
            implausible.append("the mask is %.0f ml, below the %.0f ml a brain "
                               "can be" % (report["volume_ml"], MIN_BRAIN_ML))
        elif report["volume_ml"] > MAX_BRAIN_ML:
            implausible.append("the mask is %.0f ml, above the %.0f ml a brain "
                               "can be" % (report["volume_ml"], MAX_BRAIN_ML))
    else:
        notes.append("the field of view is not head-sized, so the absolute "
                     "brain-volume range was not applied")
    if fov_fraction < 0.02:
        implausible.append("the mask covers %.1f%% of the field of view"
                           % (100.0 * fov_fraction))
    elif fov_fraction > 0.90:
        implausible.append("the mask covers %.1f%% of the field of view, so it is "
                           "not a brain extraction" % (100.0 * fov_fraction))
    if report["centroid_offset"]["fraction_of_fov"] > 0.15:
        implausible.append("the mask centre is %.1f mm from the centre of the "
                           "signal" % report["centroid_offset"]["mm"])

    if implausible:
        verdict = "implausible"
        reasons = implausible
    else:
        if report["missing"]["bright_fraction"] > warn_fraction:
            reasons.append("%.1f%% of the mask volume again is brain-bright "
                           "signal just outside it"
                           % (100.0 * report["missing"]["bright_fraction"]))
        if worst["fraction"] > 0.25:
            reasons.append("one %dx%dx%d block is %.0f%% missing%s"
                           % (block[0], block[1], block[2],
                              100.0 * worst["fraction"],
                              " near %s mm" % worst.get("centre_mm")
                              if worst.get("centre_mm") else ""))
        if report["truncation"]["slices"]:
            reasons.append("the mask ends abruptly rather than tapering "
                           "(%s slice(s): %s)"
                           % ("/".join(report["truncation"]["kinds"]) or "?",
                              ", ".join(str(s) for s in
                                        report["truncation"]["slices"][:8])))
        verdict = "suspicious" if reasons else "ok"

    report["verdict"] = verdict
    report["repairable"] = verdict == "suspicious"
    report["reasons"] = reasons
    if report["missing"]["dark_fraction"] > warn_fraction:
        notes.append("%.1f%% of the mask volume again is *dark* just outside it: "
                     "signal dropout, not a mask error -- no mask can recover "
                     "signal that is not there, and eddy's outlier replacement "
                     "is what addresses it"
                     % (100.0 * report["missing"]["dark_fraction"]))
        report["dropout"] = True
    else:
        report["dropout"] = False
    if report["fov_contact"]["fraction"] > 0.02:
        notes.append("%.1f%% of the mask lies on a face of the field of view "
                     "(%s): the acquisition itself clipped the head, which no "
                     "repair can undo"
                     % (100.0 * report["fov_contact"]["fraction"],
                        ", ".join(sorted(report["fov_contact"]["faces"]))))
    report["notes"] = notes
    return report


# -------------------------------------------------------------- repair ----

def repair(image: np.ndarray, mask: np.ndarray, permissive: np.ndarray | None = None,
           missing_dark: np.ndarray | None = None, confine: np.ndarray | None = None,
           grow: int = 0, cap: float = 0.25, hole_cap: float = 0.05,
           threshold: float | None = None,
           ceiling: float | None = None) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Repair a mask, strictly additively, locally, and within a cap.

    Every step is recorded, and the whole repair is discarded rather than
    applied when it would add more than `cap` of the original volume: a repair
    that large is not a repair, it is a different mask, and silently replacing
    BET's would be worse than reporting that the mask is broken.

    `confine` limits where anything may be added -- normally the neighbourhood of
    the defect the check found. Without it, a union with a lower-threshold `bet`
    adds a shell over the *whole* surface, which changes a mask that was right
    everywhere except one chunk. With it, the good 95% of the mask stays exactly
    as BET made it and the repair is legible as a repair.
    """
    image = np.asarray(image, dtype=np.float64)
    if image.ndim == 4:
        image = image.mean(axis=3)
    original = np.asarray(mask) > 0
    out = original.copy()
    steps: List[Dict[str, Any]] = []
    n_original = int(original.sum())
    if n_original == 0:
        return original, {"applied": False,
                          "reason": "the mask is empty; there is nothing to grow",
                          "steps": steps}

    if threshold is None:
        threshold = tissue_threshold(image, original)
    if ceiling is None:
        ceiling = float(np.percentile(image[original], 99.5))
    forbidden = np.zeros_like(out) if missing_dark is None \
        else (np.asarray(missing_dark) > 0)
    steps.append({"step": "exclude known dropout",
                  "excluded_voxels": int(forbidden.sum())}) \
        if forbidden.any() else None

    if permissive is not None:
        other = np.asarray(permissive) > 0
        if other.shape != out.shape:
            raise MaskQcError("the permissive mask is a different shape")
        if confine is not None:
            other = other & (np.asarray(confine) > 0)
        # Only where there is signal, and never into a known dropout. A
        # permissive bet happily reaches across a hole the data does not contain,
        # and adding signal-free voxels to the mask eddy is given is worse than
        # leaving the hole out: eddy would model noise as brain.
        other = other & (image > threshold) & ~forbidden
        # Union, never replace: BET's surface model shifts with f, so a mask at a
        # lower threshold is not guaranteed to contain the one at a higher.
        added = other & ~out
        out = out | other
        steps.append({"step": "union with a permissive bet, where there is signal"
                               + (" near the defect" if confine is not None else ""),
                      "added_voxels": int(added.sum())})

    holes = span_fill_intersection(out) & ~out & ~forbidden
    n_holes = int(holes.sum())
    if n_holes and n_holes <= hole_cap * n_original:
        out = out | holes
        steps.append({"step": "fill enclosed holes", "added_voxels": n_holes})
    elif n_holes:
        steps.append({"step": "fill enclosed holes", "added_voxels": 0,
                      "skipped": "%d voxels is more than %.0f%% of the mask; a "
                                 "hole that large is not a hole"
                                 % (n_holes, 100.0 * hole_cap)})

    # Intensity growth is off by default. It is bounded above as well as below:
    # fat and skull marrow are bright and contiguous with brain, so a flood fill
    # with only a lower bound walks straight out of the head.
    for iteration in range(max(0, int(grow))):
        candidate = dilate(out, 1) & ~out & (image > threshold) \
            & (image <= ceiling) & ~forbidden
        if confine is not None:
            candidate = candidate & (np.asarray(confine) > 0)
        n_added = int(candidate.sum())
        if not n_added:
            break
        out = out | candidate
        steps.append({"step": "grow into tissue (iteration %d)" % (iteration + 1),
                      "added_voxels": n_added})

    total_added = int(out.sum()) - n_original
    record: Dict[str, Any] = {
        "steps": steps,
        "voxels_before": n_original,
        "voxels_after": int(out.sum()),
        "added_voxels": total_added,
        "added_fraction": round(total_added / float(n_original), 4),
        "cap": float(cap),
        "grow_iterations": int(grow),
    }

    if total_added > cap * n_original:
        record["applied"] = False
        record["reason"] = ("the repair would have added %.1f%% of the mask "
                            "volume, above the %.0f%% cap; the original mask was "
                            "kept and the mask should be inspected"
                            % (100.0 * record["added_fraction"], 100.0 * cap))
        record["voxels_after"] = n_original
        return original, record

    # Additivity is the property the whole design rests on: a repair that can
    # remove a voxel could quietly shrink a good mask.
    if (original & ~out).any():                         # pragma: no cover
        raise MaskQcError("internal error: the repair removed voxels")

    record["applied"] = total_added > 0
    if not record["applied"]:
        record["reason"] = "nothing to add: the permissive mask and the hole " \
                           "fill found no voxels the mask was missing"
    return out, record


# -------------------------------------------------------------- figure ----

def write_png(path: str, rgb: np.ndarray) -> None:
    """Write an 8-bit RGB PNG with zlib and struct only."""
    rgb = np.ascontiguousarray(np.asarray(rgb, dtype=np.uint8))
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise MaskQcError("expected an (h, w, 3) array, got %s" % (rgb.shape,))
    height, width = rgb.shape[:2]
    raw = b"".join(b"\x00" + rgb[row].tobytes() for row in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        fh.write(chunk(b"IEND", b""))


def normalise(plane: np.ndarray) -> np.ndarray:
    plane = np.asarray(plane, dtype=np.float64)
    finite = plane[np.isfinite(plane)]
    if not finite.size:
        return np.zeros(plane.shape, dtype=np.uint8)
    top = float(np.percentile(finite, 99.0))
    if top <= 0:
        top = float(finite.max()) or 1.0
    scaled = np.clip(plane / top, 0.0, 1.0) * 255.0
    return scaled.astype(np.uint8)


def montage(image: np.ndarray, mask: np.ndarray, axis: int,
            overlays: Sequence[Tuple[np.ndarray, Tuple[int, int, int]]] = (),
            n_slices: int = 12, columns: int = 4) -> np.ndarray:
    """Tile slices of `image` with the mask outline and overlays drawn on them.

    The outline, not a filled overlay: a filled mask hides exactly the tissue
    you are trying to judge the boundary against.
    """
    image = np.asarray(image, dtype=np.float64)
    if image.ndim == 4:
        image = image.mean(axis=3)
    mask = np.asarray(mask) > 0
    present = np.nonzero(mask.sum(axis=tuple(a for a in range(3) if a != axis)))[0]
    if present.size:
        low, high = int(present[0]), int(present[-1])
    else:
        low, high = 0, mask.shape[axis] - 1
    count = max(1, min(int(n_slices), high - low + 1))
    indices = np.unique(np.rint(np.linspace(low, high, count)).astype(int))

    outline = boundary(mask)
    tiles = []
    for index in indices:
        plane = np.take(image, index, axis=axis)
        tile = np.repeat(normalise(plane)[:, :, None], 3, axis=2)
        for flag, colour in list(overlays) + [(outline, (228, 66, 86))]:
            painted = np.take(np.asarray(flag) > 0, index, axis=axis)
            tile[painted] = colour
        # Rows run superior-to-inferior on screen for the usual axes, which is
        # only cosmetic -- the tile is transposed so the in-plane axes are not
        # swapped relative to the voxel grid.
        tiles.append(np.flipud(np.transpose(tile, (1, 0, 2))))

    height = max(t.shape[0] for t in tiles)
    width = max(t.shape[1] for t in tiles)
    columns = max(1, min(int(columns), len(tiles)))
    rows = (len(tiles) + columns - 1) // columns
    canvas = np.zeros((rows * height, columns * width, 3), dtype=np.uint8)
    for position, tile in enumerate(tiles):
        row, column = divmod(position, columns)
        canvas[row * height:row * height + tile.shape[0],
               column * width:column * width + tile.shape[1]] = tile
    return canvas


# ----------------------------------------------------------------- CLI ----

def load_mask(path: str) -> Tuple[np.ndarray, Any]:
    img = nib.load(path)
    return np.asanyarray(img.dataobj) > 0, img


def load_image(path: str) -> Tuple[np.ndarray, Any]:
    img = nib.load(path)
    data = np.asanyarray(img.dataobj).astype(np.float64)
    if data.ndim == 4:
        data = data.mean(axis=3)
    return data, img


def read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_json(path: str, payload: dict) -> None:
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)


def candidates_of(image: np.ndarray, mask: np.ndarray, affine: np.ndarray,
                  band: int = BAND) -> Tuple[np.ndarray, np.ndarray]:
    """The missing-bright / missing-dark volumes, recomputed for a repair.

    `assess` reports counts, not volumes -- the arrays would not fit in a JSON
    report -- so the repair step rebuilds them from the same three criteria.
    """
    threshold = tissue_threshold(image, mask)
    bright = image > threshold
    lr_axis = axis_of(affine, ("L", "R"))
    inside_hull = span_fill_union(mask) & ~mask
    mirrored = (mirror_about_centroid(mask, lr_axis) & ~mask) if lr_axis is not None \
        else np.zeros_like(mask)
    candidates = thicken(mirrored & inside_hull) | (inside_hull & dilate(mask, band))
    return candidates & bright, candidates & ~bright


def cmd_check(args: argparse.Namespace) -> int:
    image, _ = load_image(args.image)
    mask, mask_img = load_mask(args.mask)
    report = assess(image, mask, mask_img.affine, label=args.label,
                    warn_fraction=args.warn_fraction)
    if args.out:
        write_json(args.out, report)
    print(report["verdict"])
    for reason in report.get("reasons", []):
        print("  %s" % reason, file=sys.stderr)
    for note in report.get("notes", []):
        print("  note: %s" % note, file=sys.stderr)
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    image, _ = load_image(args.image)
    mask, mask_img = load_mask(args.mask)
    permissive = load_mask(args.permissive)[0] if args.permissive else None
    missing_bright, missing_dark = candidates_of(image, mask, mask_img.affine)
    confine = dilate(missing_bright, BAND) if args.confine == "local" else None

    repaired, record = repair(image, mask, permissive=permissive,
                              missing_dark=missing_dark, confine=confine,
                              grow=args.grow, cap=args.cap)
    record["confined"] = args.confine == "local"
    if record["applied"]:
        nib.save(nib.Nifti1Image(repaired.astype(np.uint8), mask_img.affine,
                                 mask_img.header), args.out_mask)

    report = read_json(args.report) if args.report else {}
    report["repair"] = record
    if record["applied"]:
        report["after"] = assess(image, repaired, mask_img.affine,
                                 label="%s (repaired)" % report.get("label", "mask"),
                                 warn_fraction=args.warn_fraction)
    if args.report:
        write_json(args.report, report)
    print("repaired" if record["applied"] else "kept")
    if record.get("reason"):
        print("  %s" % record["reason"], file=sys.stderr)
    return 0


def cmd_figure(args: argparse.Namespace) -> int:
    image, _ = load_image(args.image)
    mask, mask_img = load_mask(args.mask)
    overlays: List[Tuple[np.ndarray, Tuple[int, int, int]]] = []
    missing_bright, _ = candidates_of(image, mask, mask_img.affine)
    # Yellow for what the check thinks is missing, drawn under the red outline.
    overlays.append((missing_bright, (242, 201, 76)))
    if args.added:
        added = load_mask(args.added)[0] & ~mask
        overlays.append((added, (84, 169, 104)))
    axis = axis_of(mask_img.affine, ("S", "I"))
    if axis is None:
        axis = mask.ndim - 1
    write_png(args.out, montage(image, mask, axis, overlays,
                                n_slices=args.slices, columns=args.columns))
    print(args.out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = ap.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="measure mask coverage")
    check.add_argument("--image", required=True, help="the image bet was run on")
    check.add_argument("--mask", required=True)
    check.add_argument("--label", default="mask")
    check.add_argument("--warn-fraction", type=float, default=0.01)
    check.add_argument("--out", help="write the report here")
    check.set_defaults(func=cmd_check)

    fix = subparsers.add_parser("repair", help="grow a mask additively, within a cap")
    fix.add_argument("--image", required=True)
    fix.add_argument("--mask", required=True)
    fix.add_argument("--permissive", help="a mask from bet at a lower threshold")
    fix.add_argument("--out-mask", required=True)
    fix.add_argument("--report", help="the report from `check`, updated in place")
    fix.add_argument("--grow", type=int, default=0,
                     help="intensity growth iterations (default: none)")
    fix.add_argument("--cap", type=float, default=0.25,
                     help="discard the repair if it adds more than this fraction")
    fix.add_argument("--warn-fraction", type=float, default=0.01)
    fix.add_argument("--confine", choices=("local", "none"), default="local",
                     help="add voxels only around the defect the check found "
                          "(local, the default), or anywhere the permissive mask "
                          "and hole fill suggest (none)")
    fix.set_defaults(func=cmd_repair)

    figure = subparsers.add_parser("figure", help="write a mask-overlay montage")
    figure.add_argument("--image", required=True)
    figure.add_argument("--mask", required=True)
    figure.add_argument("--added", help="the repaired mask, to colour what was added")
    figure.add_argument("--report", help="unused; accepted so callers can pass it")
    figure.add_argument("--slices", type=int, default=12)
    figure.add_argument("--columns", type=int, default=4)
    figure.add_argument("--out", required=True)
    figure.set_defaults(func=cmd_figure)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except MaskQcError as exc:
        print("mask_qc: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
