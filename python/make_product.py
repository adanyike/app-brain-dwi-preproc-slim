#!/usr/bin/env python3
"""Write ``product.json``, the summary brainlife renders on the task page.

Contains a short provenance block, any warnings worth surfacing (missing
reverse-phase-encode data, ROIs that fell outside the field of view, high
motion), and two plotly figures: mean FA per ROI, and per-volume motion from
eddy's movement-RMS file when it exists.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Sequence


def read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def read_movement_rms(path: str) -> List[float]:
    """Column 1 of eddy_movement_rms: total RMS displacement per volume (mm)."""
    if not os.path.exists(path):
        return []
    values = []
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if parts:
                try:
                    values.append(float(parts[0]))
                except ValueError:
                    pass
    return values


def fa_figure(stats: List[dict]) -> Dict[str, Any] | None:
    rows = [r for r in stats if r.get("metric") == "FA"]
    if not rows:
        return None
    rows.sort(key=lambda r: r["roi_index"])
    labels = ["%d %s" % (r["roi_index"], r["roi_abbreviation"]) for r in rows]
    means = [None if r["mean"] != r["mean"] else round(r["mean"], 4) for r in rows]
    hover = ["%s<br>%d voxels" % (r["roi_name"], r["n_voxels"]) for r in rows]
    return {
        "type": "plotly",
        "name": "Mean FA per JHU ROI",
        "data": [{
            "type": "bar",
            "x": labels,
            "y": means,
            "text": hover,
            "hoverinfo": "text+y",
            "marker": {"color": "#4c78a8"},
        }],
        "layout": {
            "title": "Mean fractional anisotropy per JHU ICBM-DTI-81 ROI",
            "xaxis": {"title": "ROI", "tickangle": -60, "automargin": True},
            "yaxis": {"title": "mean FA", "range": [0, 1]},
            "margin": {"b": 140},
        },
    }


def motion_figure(rms: Sequence[float]) -> Dict[str, Any] | None:
    if not rms:
        return None
    return {
        "type": "plotly",
        "name": "Motion (eddy)",
        "data": [{
            "type": "scatter",
            "mode": "lines",
            "y": [round(v, 4) for v in rms],
            "line": {"color": "#e45756"},
            "name": "total RMS",
        }],
        "layout": {
            "title": "Estimated movement per volume",
            "xaxis": {"title": "volume"},
            "yaxis": {"title": "RMS displacement (mm)"},
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prep", required=True, help="prep.json from stage 0")
    ap.add_argument("--roi-stats", help="roi_stats.json from stage 5")
    ap.add_argument("--shells", help="shells.json from stage 3")
    ap.add_argument("--eddy-movement-rms", help="<eddy_base>.eddy_movement_rms")
    ap.add_argument("--eddy-binary", default="")
    ap.add_argument("--slice-to-volume", default="false")
    ap.add_argument("--topup-applied", default="false")
    ap.add_argument("--shell", default="")
    ap.add_argument("--motion-warn-mm", type=float, default=2.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    prep = read_json(args.prep)
    roi = read_json(args.roi_stats) if args.roi_stats else {}
    rms = read_movement_rms(args.eddy_movement_rms) if args.eddy_movement_rms else []

    messages: List[Dict[str, str]] = []
    truthy = {"true", "1", "yes"}
    topup_applied = args.topup_applied.lower() in truthy
    s2v = args.slice_to_volume.lower() in truthy

    messages.append({"type": "info", "msg":
        "Preprocessed %d volumes (%d forward + %d reverse phase-encoded), "
        "%d b=0 of which %d were used for topup." % (
            prep.get("n_volumes", 0), prep.get("n_volumes_forward", 0),
            prep.get("n_volumes_reverse", 0), prep.get("n_b0_total", 0),
            prep.get("n_b0_for_topup", 0))})

    if topup_applied:
        messages.append({"type": "info", "msg":
            "Susceptibility distortion corrected with topup using opposing "
            "phase-encoding directions."})
    else:
        messages.append({"type": "warning", "msg":
            "No reverse phase-encoded data was available, so topup was skipped "
            "and susceptibility-induced distortion has NOT been corrected."})

    messages.append({
        "type": "info" if s2v else "warning",
        "msg": ("Slice-to-volume (within-volume motion) correction was applied with %s."
                % args.eddy_binary) if s2v else
               ("Slice-to-volume correction was NOT applied (%s has no CUDA support "
                "or no slice timing was available); only volume-to-volume motion "
                "correction was performed." % (args.eddy_binary or "eddy"))})

    shell_report = read_json(args.shells) if args.shells else {}
    detected = shell_report.get("shells") or prep.get("shells") or []
    if detected:
        messages.append({"type": "info", "msg":
            "Shells detected: %s." % ", ".join(
                "b=%d (%d volumes)%s" % (s["b"], s["n_volumes"],
                                         " [baseline]" if s.get("is_b0") else "")
                for s in detected)})
    if args.shell:
        resolved = shell_report.get("resolved", {})
        if resolved.get("mode") == "all":
            messages.append({"type": "info", "msg":
                "Tensor fitted to every volume."})
        elif resolved:
            messages.append({"type": "info", "msg":
                "Tensor fitted on b=%s using %d of %d volumes (%s)."
                % (args.shell, resolved.get("n_volumes", 0),
                   prep.get("n_volumes", 0), resolved.get("reason", ""))})
        else:
            messages.append({"type": "info", "msg":
                "Tensor fitted on shell(s): %s." % args.shell})

    if rms:
        peak = max(rms)
        mean_rms = sum(rms) / len(rms)
        level = "warning" if peak > args.motion_warn_mm else "info"
        messages.append({"type": level, "msg":
            "Estimated motion: mean %.2f mm, peak %.2f mm RMS displacement."
            % (mean_rms, peak)})

    empty = roi.get("empty_rois") or []
    if empty:
        messages.append({"type": "warning", "msg":
            "%d atlas ROI(s) contained no voxels after warping to native space "
            "(%s). Check the field of view and the FA-to-template registration."
            % (len(empty), ", ".join(empty))})
    elif roi:
        messages.append({"type": "info", "msg":
            "Extracted %s for all %d JHU ICBM-DTI-81 white-matter ROIs."
            % ("/".join(roi.get("metrics", [])), roi.get("n_rois", 0))})

    figures = [f for f in (fa_figure(roi.get("stats", [])), motion_figure(rms)) if f]

    product: Dict[str, Any] = {
        "brainlife": messages + figures,
        "provenance": {
            "phase_encoding": prep.get("phase_encoding", []),
            "n_volumes": prep.get("n_volumes"),
            "topup_applied": topup_applied,
            "eddy_binary": args.eddy_binary,
            "slice_to_volume_correction": s2v,
            "shells_detected": detected,
            "tensor_shell": args.shell,
        },
    }

    with open(args.out, "w") as fh:
        json.dump(product, fh, indent=2)
    print("make_product: wrote %s (%d messages, %d figures)"
          % (args.out, len(messages), len(figures)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
