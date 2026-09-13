#!/usr/bin/env python3
"""Write ``product.json``, the summary brainlife renders on the task page.

Contains a short provenance block, any warnings worth surfacing (missing
reverse-phase-encode data, ROIs that fell outside the field of view, high
motion, a brain mask that does not cover the brain), and up to three plotly
figures: mean FA per ROI, per-volume motion from eddy's movement-RMS file, and
the per-slice brain-mask coverage profile.
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


def mask_qc_messages(reports: Dict[str, dict]) -> List[Dict[str, str]]:
    """What the brain-mask coverage check found, per mask.

    A repair is reported as a change in results, not as housekeeping: the
    stage-1 mask is eddy's, so a repaired mask means the corrected data differs
    from what an unrepaired run would have produced, and nobody reading the task
    page later should have to infer that.
    """
    messages: List[Dict[str, str]] = []
    for label, report in sorted(reports.items()):
        if not report:
            continue
        verdict = report.get("verdict", "?")
        missing = report.get("missing", {})
        repair = report.get("repair", {})
        where = ("; worst around %s mm"
                 % report["worst_block"]["centre_mm"]) if (
                     report.get("worst_block", {}).get("centre_mm")
                     and report["worst_block"].get("fraction", 0) > 0.25) else ""

        if verdict == "ok":
            level, text = "info", (
                "Brain mask (%s) covers the brain: %.0f ml, nothing brain-bright "
                "left outside it beyond %.1f%% of its volume."
                % (label, report.get("volume_ml", 0),
                   100.0 * missing.get("bright_fraction", 0.0)))
        elif verdict == "implausible":
            level, text = "warning", (
                "Brain mask (%s) is not a brain: %s. It was NOT repaired -- "
                "growing it would hide the problem. Check qc/mask_overlay_%s.png "
                "and re-run with a different bet threshold."
                % (label, "; ".join(report.get("reasons", [])) or "see the report",
                   label))
        else:
            level, text = "warning", (
                "Brain mask (%s) looks like it is missing brain: %s%s."
                % (label, "; ".join(report.get("reasons", [])) or "see the report",
                   where))
        messages.append({"type": level, "msg": text})

        if repair.get("applied"):
            messages.append({"type": "warning", "msg":
                "Brain mask (%s) was repaired: %d voxels added (%.1f%% of it), %s. "
                "Everything computed from this mask therefore differs from an "
                "unrepaired run%s."
                % (label, repair.get("added_voxels", 0),
                   100.0 * repair.get("added_fraction", 0.0),
                   "; ".join(step["step"] for step in repair.get("steps", []))
                   or "additively",
                   " -- including eddy's corrected data, since this is the mask "
                   "eddy was given" if label == "eddy" else "")})
        elif repair and repair.get("reason"):
            messages.append({"type": "warning", "msg":
                "Brain mask (%s) was left as bet made it: %s"
                % (label, repair["reason"])})

        for note in report.get("notes", []):
            if report.get("dropout") and "dark" in note:
                messages.append({"type": "warning", "msg":
                    "Brain mask (%s): %s" % (label, note)})
            elif "field of view" in note and "clipped" in note:
                messages.append({"type": "warning", "msg":
                    "Brain mask (%s): %s" % (label, note)})
    return messages


def mask_qc_figure(reports: Dict[str, dict]) -> Dict[str, Any] | None:
    """Per-slice mask area, with the missing area beside it.

    Two traces per mask: a bite shows as a dip in the area curve with a bump in
    the missing one at the same slice, which is the cheapest way to see *where*
    without opening the images.
    """
    traces = []
    palette = {"eddy": "#4c78a8", "final": "#54a968"}
    for label, report in sorted(reports.items()):
        profile = (report or {}).get("slice_profile") or {}
        area = profile.get("mask_area") or []
        if not area:
            continue
        colour = palette.get(label, "#9d755d")
        traces.append({"type": "scatter", "mode": "lines", "y": area,
                       "name": "%s mask" % label, "line": {"color": colour}})
        missing = profile.get("missing_area") or []
        if any(missing):
            traces.append({"type": "scatter", "mode": "lines", "y": missing,
                           "name": "%s missing" % label,
                           "line": {"color": "#e45756", "dash": "dot"}})
    if not traces:
        return None
    return {
        "type": "plotly",
        "name": "Brain mask coverage",
        "data": traces,
        "layout": {
            "title": "Mask area per slice (dotted: brain-bright signal outside the mask)",
            "xaxis": {"title": "slice"},
            "yaxis": {"title": "voxels"},
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
    ap.add_argument("--eddy-qc", help="squad_ready.json from stage 6")
    ap.add_argument("--mask-qc", action="append", default=[], metavar="LABEL=PATH",
                    help="a brain-mask coverage report; repeatable")
    ap.add_argument("--eddy-movement-rms", help="<eddy_base>.eddy_movement_rms")
    ap.add_argument("--eddy-binary", default="")
    ap.add_argument("--slice-to-volume", default="false")
    ap.add_argument("--topup-applied", default="false")
    ap.add_argument("--topup-config", default="",
                    help="the topup configuration file the run resolved to")
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

    # The group-QC signature. Saying this out loud on every task page is what
    # makes a later SQUAD run predictable: two subjects whose signatures differ
    # cannot be pooled, and the reason is almost always visible right here --
    # one of them ran without a GPU, or without a reverse phase-encoded series.
    eddy_qc = read_json(args.eddy_qc) if args.eddy_qc else {}
    if eddy_qc:
        flags = eddy_qc.get("eddy_flags", {})
        off = [eddy_qc.get("flag_meanings", {}).get(name, name)
               for name, on in sorted(flags.items()) if not on]
        messages.append({"type": "info", "msg":
            "Group QC: eddy cohort signature %s (%s). Only subjects sharing this "
            "signature can be pooled by eddy_squad.%s"
            % (eddy_qc.get("signature_hash", "?"), eddy_qc.get("signature", ""),
               " Not available for this subject: %s." % "; ".join(off) if off else "")})

    mask_reports: Dict[str, dict] = {}
    for item in args.mask_qc:
        label, _, path = item.partition("=")
        if not path:
            print("make_product: ignoring --mask-qc %r (expected LABEL=PATH)" % item,
                  file=sys.stderr)
            continue
        mask_reports[label] = read_json(path)
    messages.extend(mask_qc_messages(mask_reports))

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

    figures = [f for f in (fa_figure(roi.get("stats", [])), motion_figure(rms),
                           mask_qc_figure(mask_reports)) if f]

    product: Dict[str, Any] = {
        "brainlife": messages + figures,
        "provenance": {
            "phase_encoding": prep.get("phase_encoding", []),
            "n_volumes": prep.get("n_volumes"),
            "topup_applied": topup_applied,
            "topup_config": args.topup_config,
            "eddy_binary": args.eddy_binary,
            "slice_to_volume_correction": s2v,
            "shells_detected": detected,
            "tensor_shell": args.shell,
            "eddy_qc": {k: eddy_qc[k] for k in ("signature", "signature_hash",
                                                "eddy_flags", "protocol")
                        if k in eddy_qc},
            # The masks, and whether either was repaired, belong in provenance
            # rather than only in the log: a repaired mask is the difference
            # between two runs of the same data.
            "brain_mask": {
                label: {key: report[key]
                        for key in ("verdict", "volume_ml", "n_voxels", "reasons",
                                    "repair", "dropout")
                        if key in report}
                for label, report in sorted(mask_reports.items()) if report},
        },
    }

    with open(args.out, "w") as fh:
        json.dump(product, fh, indent=2)
    print("make_product: wrote %s (%d messages, %d figures)"
          % (args.out, len(messages), len(figures)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
