#!/usr/bin/env python3
"""Summarise diffusion scalar maps inside every atlas ROI.

Every ROI and every metric is measured in one pass over the label image, and
alongside the mean each row carries the context needed to spot a bad
registration: how many voxels the ROI actually covered, and the spread of the
values inside it.

Three views of the same numbers are written:

  roi_stats.csv      tidy/long -- one row per (metric, ROI); best for analysis
  <metric>_mean.csv  wide -- one row per subject, one column per ROI, which is
                     the shape a spreadsheet wants
  roi_stats.json     the same content plus provenance, for product.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Dict, List, Sequence

import numpy as np
import nibabel as nib


class RoiStatsError(RuntimeError):
    pass


def load_labels(path: str) -> List[dict]:
    with open(path) as fh:
        meta = json.load(fh)
    labels = meta.get("labels")
    if not labels:
        raise RoiStatsError("%s contains no 'labels' array" % path)
    return labels


def voxel_volume_mm3(img: nib.Nifti1Image) -> float:
    return float(abs(np.linalg.det(img.affine[:3, :3])))


def summarise(values: np.ndarray) -> Dict[str, float | int]:
    if values.size == 0:
        return {"mean": float("nan"), "std": float("nan"),
                "median": float("nan"), "min": float("nan"),
                "max": float("nan"), "n_voxels": 0}
    return {
        "mean": float(np.mean(values)),
        "std": float(np.std(values, ddof=0)),
        "median": float(np.median(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "n_voxels": int(values.size),
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--atlas", required=True, help="label image in native space")
    ap.add_argument("--labels", required=True, help="atlas label metadata JSON")
    ap.add_argument("--metric", action="append", default=[], metavar="NAME=PATH",
                    help="scalar map to summarise; repeatable")
    ap.add_argument("--brain-mask", help="restrict every ROI to this mask")
    ap.add_argument("--exclude-zeros", action="store_true",
                    help="drop exactly-zero voxels before averaging")
    ap.add_argument("--subject", default="subject")
    ap.add_argument("--session", default="")
    ap.add_argument("--run-id", default="",
                    help="identifier tying these rows back to the run that produced them")
    ap.add_argument("--shell", default="", help="recorded in the output for provenance")
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args(argv)

    if not args.metric:
        raise RoiStatsError("at least one --metric NAME=PATH is required")

    atlas_img = nib.load(args.atlas)
    atlas = np.asanyarray(atlas_img.dataobj)
    # MultiLabel/NearestNeighbor keep integers, but a Linear-interpolated atlas
    # arrives as float; round so the == comparison below behaves.
    atlas = np.rint(atlas).astype(np.int32)
    vox_mm3 = voxel_volume_mm3(atlas_img)

    brain = None
    if args.brain_mask:
        brain = np.asanyarray(nib.load(args.brain_mask).dataobj) > 0
        if brain.shape != atlas.shape:
            raise RoiStatsError("brain mask and atlas have different shapes")

    labels = load_labels(args.labels)
    metrics: Dict[str, np.ndarray] = {}
    for spec in args.metric:
        if "=" not in spec:
            raise RoiStatsError("--metric expects NAME=PATH, got %r" % spec)
        name, path = spec.split("=", 1)
        if not os.path.exists(path):
            print("roi_stats: %s map missing at %s -- skipping" % (name, path),
                  file=sys.stderr)
            continue
        data = np.asanyarray(nib.load(path).dataobj).astype(np.float64)
        if data.shape != atlas.shape:
            raise RoiStatsError(
                "%s has shape %s but the atlas has %s; they must share a grid"
                % (name, data.shape, atlas.shape)
            )
        metrics[name] = data
    if not metrics:
        raise RoiStatsError("none of the requested metric maps exist")

    os.makedirs(args.outdir, exist_ok=True)

    rows: List[dict] = []
    empty_rois: List[str] = []
    for label in labels:
        index = int(label["index"])
        roi = atlas == index
        if brain is not None:
            roi &= brain
        n_roi = int(roi.sum())
        if n_roi == 0:
            empty_rois.append(label.get("abbreviation", str(index)))

        for name, data in metrics.items():
            values = data[roi]
            if args.exclude_zeros:
                values = values[values != 0]
            stats = summarise(values)
            rows.append({
                "subject": args.subject,
                "session": args.session,
                "run_id": args.run_id,
                "metric": name,
                "roi_index": index,
                "roi_abbreviation": label.get("abbreviation", ""),
                "roi_name": label.get("name", ""),
                "hemisphere": label.get("hemisphere", ""),
                "n_voxels": stats["n_voxels"],
                "volume_mm3": round(stats["n_voxels"] * vox_mm3, 3),
                "mean": stats["mean"],
                "std": stats["std"],
                "median": stats["median"],
                "min": stats["min"],
                "max": stats["max"],
            })

    tidy = os.path.join(args.outdir, "roi_stats.csv")
    fields = ["subject", "session", "run_id", "metric", "roi_index", "roi_abbreviation",
              "roi_name", "hemisphere", "n_voxels", "volume_mm3",
              "mean", "std", "median", "min", "max"]
    with open(tidy, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    # Wide per-metric files: one row per subject, one column per ROI.
    for name in metrics:
        wide = os.path.join(args.outdir, "%s_mean.csv" % name)
        ordered = [r for r in rows if r["metric"] == name]
        ordered.sort(key=lambda r: r["roi_index"])
        with open(wide, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["subject", "session", "run_id"] +
                            ["%d_%s" % (r["roi_index"], r["roi_abbreviation"])
                             for r in ordered])
            writer.writerow([args.subject, args.session, args.run_id] +
                            ["%.6f" % r["mean"] if r["mean"] == r["mean"] else ""
                             for r in ordered])

    payload = {
        "subject": args.subject,
        "session": args.session,
        "run_id": args.run_id,
        "atlas": os.path.basename(args.atlas),
        "atlas_labels": os.path.basename(args.labels),
        "shell": args.shell,
        "voxel_volume_mm3": round(vox_mm3, 6),
        "brain_mask_applied": bool(args.brain_mask),
        "zeros_excluded": bool(args.exclude_zeros),
        "metrics": sorted(metrics),
        "n_rois": len(labels),
        "empty_rois": empty_rois,
        "stats": rows,
    }
    with open(os.path.join(args.outdir, "roi_stats.json"), "w") as fh:
        json.dump(payload, fh, indent=2)

    print("roi_stats: %d ROIs x %d metrics -> %s"
          % (len(labels), len(metrics), tidy), file=sys.stderr)
    if empty_rois:
        print("roi_stats: %d ROI(s) had no voxels in native space (%s) -- check "
              "the registration and the field of view"
              % (len(empty_rois), ", ".join(empty_rois)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RoiStatsError as exc:
        print("roi_stats: %s" % exc, file=sys.stderr)
        sys.exit(2)
