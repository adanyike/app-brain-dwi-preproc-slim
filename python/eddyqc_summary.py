#!/usr/bin/env python3
"""Summarise QUAD's ``qc.json`` into the small file a group SQUAD run needs.

``eddy_squad`` reads nothing but ``qc.json`` from each subject folder, and it
refuses the lot outright -- ``ValueError: Eddy output inconsistency detected!``
-- when the subjects were not all processed with the same ``eddy`` features.
That is easy to trip on brainlife, where every subject is an independently
launched task: slice-to-volume correction depends on a GPU being visible, the
susceptibility field on the subject having a reverse phase-encoded series, and
outlier replacement / CNR maps on config keys that drift between submissions.

So each subject publishes a signature alongside its database: the five feature
flags SQUAD compares, plus the shell structure that has to match for the
group's per-shell arrays to line up. The group app buckets subjects by that
signature and reports what it had to leave out, instead of dying on subject 37.

Written by stage 6 into ``output/eddyqc/squad_ready.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Sequence

# The flags squad_db.py compares across subjects before it will build a group
# database. qc_rss_flag is in that comparison too, but QUAD only writes it when
# eddy was given --residuals, so it is treated like the rest: absent means
# False, and False for everyone is consistent.
SQUAD_FLAGS = (
    "qc_params_flag",
    "qc_s2v_params_flag",
    "qc_field_flag",
    "qc_ol_flag",
    "qc_cnr_flag",
    "qc_rss_flag",
)

# What each flag means in terms of this pipeline, so a task page can say why a
# subject will not pool with the others rather than just naming a JSON key.
FLAG_MEANING = {
    "qc_params_flag": "eddy current / movement parameters",
    "qc_s2v_params_flag": "slice-to-volume (within-volume) motion correction",
    "qc_field_flag": "topup susceptibility field",
    "qc_ol_flag": "outlier detection and replacement (--repol)",
    "qc_cnr_flag": "SNR / CNR maps (--cnr_maps)",
    "qc_rss_flag": "eddy residuals (--residuals)",
}


def read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def as_bool(value: Any) -> bool:
    """QUAD writes its flags as JSON true/false; tolerate 0/1 and "true" too."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def as_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN carries no information


def as_number_list(value: Any) -> List[float]:
    if not isinstance(value, (list, tuple)):
        value = [value]
    out = []
    for item in value:
        number = as_number(item)
        if number is not None:
            out.append(number)
    return out


def round_bvals(values: Sequence[float]) -> List[int]:
    """Shell b-values, to the nearest 50, so 1495 and 1500 are one shell.

    QUAD copies eddy's unique b-values straight out of the bvals file, and a
    scanner that writes 1495 for one subject and 1500 for the next would
    otherwise split an otherwise identical cohort in two.
    """
    return sorted({int(round(v / 50.0)) * 50 for v in values})


def flags_of(qc: dict) -> Dict[str, bool]:
    return {name: as_bool(qc.get(name)) for name in SQUAD_FLAGS}


def protocol_of(qc: dict) -> Dict[str, Any]:
    unique = round_bvals(as_number_list(qc.get("data_unique_bvals")))
    return {
        "n_shells": qc.get("data_no_shells"),
        "unique_bvals": unique,
        "n_dw_volumes": qc.get("data_no_dw_vols"),
        "n_b0_volumes": qc.get("data_no_b0_vols"),
        "n_pe_directions": qc.get("data_no_PE_dirs"),
        "voxel_size_mm": [round(v, 4) for v in as_number_list(qc.get("data_vox_size"))],
    }


def signature_of(flags: Dict[str, bool], protocol: Dict[str, Any]) -> str:
    """The cohort key: what must match for SQUAD to pool these subjects.

    Flags because SQUAD compares them itself and raises otherwise. Shell count,
    shell b-values and phase-encode directions because SQUAD takes the protocol
    from whichever subject happens to be listed first and stacks the per-shell
    CNR and outlier arrays on top of each other -- differing shells there are
    not an error, they are a silently mislabelled group report.

    Volume counts are deliberately *not* in the key. A subject with a dropped
    volume still pools correctly; excluding it would throw away data over
    something SQUAD handles.
    """
    enabled = ",".join(name[3:-5] for name in SQUAD_FLAGS if flags[name]) or "none"
    return "|".join([
        "flags=" + enabled,
        "shells=%s" % protocol["n_shells"],
        "bvals=" + ",".join(str(b) for b in protocol["unique_bvals"]),
        "pedirs=%s" % protocol["n_pe_directions"],
    ])


def metrics_of(qc: dict) -> Dict[str, Any]:
    """The handful of headline numbers worth having without opening the PDF."""
    return {
        "motion_abs_mm": as_number(qc.get("qc_mot_abs")),
        "motion_rel_mm": as_number(qc.get("qc_mot_rel")),
        "outliers_pct": as_number(qc.get("qc_outliers_tot")),
        "voxel_displacement_std": as_number(qc.get("qc_vox_displ_std")),
        "cnr_avg": as_number_list(qc.get("qc_cnr_avg")),
        "cnr_std": as_number_list(qc.get("qc_cnr_std")),
    }


def summarise(qc: dict, labels: Dict[str, str]) -> Dict[str, Any]:
    flags = flags_of(qc)
    protocol = protocol_of(qc)
    signature = signature_of(flags, protocol)
    summary: Dict[str, Any] = dict(labels)
    summary.update({
        "squad_ready": bool(qc),
        "eddy_flags": flags,
        "flag_meanings": {name: FLAG_MEANING[name] for name in SQUAD_FLAGS},
        "protocol": protocol,
        "signature": signature,
        "signature_hash": hashlib.sha1(signature.encode()).hexdigest()[:8],
        "metrics": metrics_of(qc),
    })
    if not qc:
        summary["note"] = ("no qc.json was produced -- eddy_quad did not run, or "
                           "failed. This subject cannot take part in a group "
                           "SQUAD analysis.")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qc-json", required=True, help="qc.json written by eddy_quad")
    ap.add_argument("--subject", default="")
    ap.add_argument("--session", default="")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    qc = read_json(args.qc_json)
    if not qc and os.path.exists(args.qc_json):
        print("eddyqc_summary: %s is not readable JSON" % args.qc_json, file=sys.stderr)

    summary = summarise(qc, {"subject": args.subject or "subject",
                             "session": args.session,
                             "run_id": args.run_id})
    with open(args.out, "w") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    print("eddyqc_summary: wrote %s (signature %s: %s)"
          % (args.out, summary["signature_hash"], summary["signature"]),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
