#!/usr/bin/env python3
"""Write ``product.json`` for a group SQUAD task.

The study-wise PDF is the deliverable, but a reviewer should be able to tell
from the task page alone how many subjects pooled, who did not, and which
subjects sit in the tail of the motion / outlier / CNR distributions. So this
reads SQUAD's ``group_db.json`` together with the cohort report written by
squad_inputs.py, and draws the three distributions that decide whether a dataset
is usable, each with its own subjects named on hover.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Sequence

# Matches the single-subject product: one blue for the data, one red for trouble.
BLUE = "#4c78a8"
RED = "#e45756"


def read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def column(db: dict, key: str, index: int = 0) -> List[float]:
    """One number per subject out of a group_db matrix.

    SQUAD stores each QC index as one row per subject, in the order of the
    folder list it was given, with the quantities packed along the row:
    ``qc_motion`` is [absolute, relative], ``qc_outliers`` is [total %, then
    per-shell, then per-phase-encode], ``qc_cnr`` is [b=0 SNR, then CNR per
    shell]. A subject whose row is short or non-numeric is skipped rather than
    shifting every later subject's label onto the wrong bar.
    """
    rows = db.get(key)
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        value = row[index] if isinstance(row, list) and len(row) > index else (
            row if not isinstance(row, list) and index == 0 else None)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:
            out.append(number)
    return out


def subject_scatter(name: str, title: str, axis: str, labels: Sequence[str],
                    values: Sequence[float], warn_above: float | None) -> Dict[str, Any] | None:
    """One point per subject, ordered worst first, with the tail coloured.

    A box plot hides which subject is the outlier, and naming the subject is the
    entire point of study-wise QC.
    """
    if not values:
        return None
    paired = sorted(zip(values, list(labels) + [""] * (len(values) - len(labels))),
                    key=lambda pair: -pair[0])
    ordered = [round(value, 4) for value, _ in paired]
    names = [label or "?" for _, label in paired]
    colours = [RED if warn_above is not None and value > warn_above else BLUE
               for value in ordered]
    return {
        "type": "plotly",
        "name": name,
        "data": [{
            "type": "bar",
            "x": names,
            "y": ordered,
            "marker": {"color": colours},
            "hoverinfo": "x+y",
        }],
        "layout": {
            "title": title,
            "xaxis": {"title": "subject (worst first)", "tickangle": -60,
                      "automargin": True},
            "yaxis": {"title": axis},
            "margin": {"b": 140},
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cohorts", required=True, help="cohorts.json from squad_inputs.py")
    ap.add_argument("--group-db", help="group_db.json written by eddy_squad")
    ap.add_argument("--updated-reports", type=int, default=0,
                    help="how many single-subject reports were updated")
    ap.add_argument("--motion-warn-mm", type=float, default=2.0)
    ap.add_argument("--outlier-warn-pct", type=float, default=5.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    cohorts = read_json(args.cohorts)
    db = read_json(args.group_db) if args.group_db else {}

    messages: List[Dict[str, str]] = []
    chosen = cohorts.get("chosen", {})
    labels = chosen.get("subjects", [])

    if cohorts.get("error"):
        messages.append({"type": "error", "msg": cohorts["error"]})

    if chosen:
        protocol = chosen.get("protocol", {})
        messages.append({"type": "info", "msg":
            "Pooled %d subject(s) in cohort %s: %s shell(s) at b=%s, %s "
            "phase-encode direction(s)."
            % (chosen.get("n_subjects", 0), chosen.get("signature_hash", "?"),
               protocol.get("n_shells"),
               "/".join(str(b) for b in protocol.get("unique_bvals", [])) or "?",
               protocol.get("n_pe_directions"))})

        off = [cohorts.get("flag_meanings", {}).get(name, name)
               for name, on in sorted((chosen.get("eddy_flags") or {}).items()) if not on]
        if off:
            messages.append({"type": "warning", "msg":
                "QC metrics NOT available for this cohort, because eddy was not "
                "run with the corresponding option: %s." % "; ".join(off)})

    # SQUAD counts the subjects it actually read. A disagreement with what was
    # staged means a qc.json was dropped between staging and the group run, and
    # every figure below would be labelled one subject out of step.
    pooled = db.get("data_no_subjects")
    if pooled and chosen.get("n_subjects") and pooled != chosen["n_subjects"]:
        messages.append({"type": "warning", "msg":
            "eddy_squad read %s subject(s) but %d were staged; the per-subject "
            "labels on the figures below may not line up."
            % (pooled, chosen["n_subjects"])})

    tied = chosen.get("tied_with") or []
    if tied:
        messages.append({"type": "warning", "msg":
            "This cohort is no larger than %d other cohort(s) (%s), so which one "
            "was reported on was decided arbitrarily, not by weight of numbers. "
            "Set 'cohort' to a signature to choose deliberately."
            % (len(tied), ", ".join(tied))})

    for excluded in cohorts.get("excluded", []):
        messages.append({"type": "warning", "msg":
            "Excluded %d subject(s) (%s): %s. eddy_squad can only pool subjects "
            "whose eddy runs used the same features -- re-run this App on that "
            "cohort separately to get its own group report."
            % (len(excluded.get("subjects", [])),
               ", ".join(excluded.get("subjects", [])[:10])
               + ("..." if len(excluded.get("subjects", [])) > 10 else ""),
               excluded.get("reason", "signature differs"))})

    for rejected in cohorts.get("rejected", []):
        messages.append({"type": "warning", "msg":
            "Input '%s' was not usable: %s." % (rejected.get("input", "?"),
                                                rejected.get("reason", "?"))})

    variable = cohorts.get("variable") or {}
    if variable:
        messages.append({"type": "info", "msg":
            "Grouped by '%s' (%s), from %s. %s plots were added to the report."
            % (variable.get("name", "?"),
               "continuous" if variable.get("continuous") else "categorical",
               variable.get("source", "?"),
               "Scatter" if variable.get("continuous") else "Violin")})

    missing_reports = cohorts.get("missing_reports") or []
    if missing_reports:
        messages.append({"type": "warning", "msg":
            "%d pooled subject(s) published no single-subject report (%s), so "
            "their reports cannot be updated with the group's context. Their QC "
            "metrics are still in the study-wise report."
            % (len(missing_reports), ", ".join(missing_reports[:10])
               + ("..." if len(missing_reports) > 10 else ""))})

    if args.updated_reports:
        messages.append({"type": "info", "msg":
            "Updated %d single-subject report(s) with study-wise context; each "
            "subject's metrics are flagged against the group in "
            "squad/updated/<subject>_qc_updated.pdf." % args.updated_reports})

    motion = column(db, "qc_motion", 0)
    relative = column(db, "qc_motion", 1)
    outliers = column(db, "qc_outliers", 0)
    cnr = column(db, "qc_cnr", 0)

    if motion:
        worst = max(motion)
        messages.append({
            "type": "warning" if worst > args.motion_warn_mm else "info",
            "msg": "Absolute motion across the group: median %.2f mm, worst "
                   "%.2f mm." % (sorted(motion)[len(motion) // 2], worst)})
    if outliers:
        worst = max(outliers)
        messages.append({
            "type": "warning" if worst > args.outlier_warn_pct else "info",
            "msg": "Outlier slices across the group: median %.2f%%, worst "
                   "%.2f%%." % (sorted(outliers)[len(outliers) // 2], worst)})

    figures = [f for f in (
        subject_scatter("Absolute motion", "Average absolute motion per subject",
                        "mm", labels, motion, args.motion_warn_mm),
        subject_scatter("Relative motion", "Average relative (volume-to-volume) "
                        "motion per subject", "mm", labels, relative, None),
        subject_scatter("Outlier slices", "Outlier slices per subject",
                        "% of slices", labels, outliers, args.outlier_warn_pct),
        subject_scatter("SNR / CNR", "b=0 SNR per subject (first shell of qc_cnr_avg)",
                        "SNR", labels, cnr, None),
    ) if f]

    product: Dict[str, Any] = {
        "brainlife": messages + figures,
        "provenance": {
            "analysis": "eddy_squad (study-wise eddy QC)",
            "n_inputs": cohorts.get("n_inputs"),
            "n_pooled": chosen.get("n_subjects"),
            "cohort_signature": chosen.get("signature"),
            "cohort_signature_hash": chosen.get("signature_hash"),
            "cohorts": cohorts.get("cohorts", []),
            "excluded": cohorts.get("excluded", []),
            "grouping_variable": variable,
            "single_subject_reports_updated": args.updated_reports,
        },
    }

    with open(args.out, "w") as fh:
        json.dump(product, fh, indent=2)
    print("make_group_product: wrote %s (%d messages, %d figures)"
          % (args.out, len(messages), len(figures)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
