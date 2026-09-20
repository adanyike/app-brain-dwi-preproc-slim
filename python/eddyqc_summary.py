#!/usr/bin/env python3
"""Summarise QUAD's ``qc.json`` into the small file a group SQUAD run needs.

``eddy_squad`` reads nothing but ``qc.json`` from each subject folder, and it
refuses the lot outright -- ``ValueError: Eddy output inconsistency detected!``
-- when the subjects were not all processed with the same ``eddy`` features.
That is easy to trip on brainlife, where every subject is an independently
launched task: slice-to-volume correction depends on a GPU being visible, the
susceptibility field on the subject having a reverse phase-encoded series, and
outlier replacement / CNR maps on config keys that drift between submissions.

So each subject publishes a signature alongside its database: the six feature
flags SQUAD compares, plus the acquisition fields it compares *exactly*. The
group app buckets subjects by that signature, checks the two fields SQUAD
compares within a tolerance, and reports what it had to leave out -- instead of
dying on subject 37.

The signature mirrors SQUAD's comparison rather than exceeding it. A key that is
looser fails the whole study where SQUAD refuses; a key that is stricter splits
a study SQUAD would have pooled, which costs just as much and announces itself
as nothing at all.

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

# How squad_db.py compares the eddy *input* data across subjects before it will
# build a group database. It does not compare every field, and it does not
# compare them all the same way -- so neither do we. A key stricter than SQUAD's
# splits cohorts SQUAD would have pooled, which is how a two-site study ends up
# as two group reports of two subjects each.
#
# Exact (``!=``): the fields a difference in which is a different acquisition,
# and SQUAD says so.
EXACT_FIELDS = (
    "data_no_shells",
    "data_no_PE_dirs",
    "data_no_b0_vols",
    "data_no_dw_vols",
    "data_eddy_para",
)

# Tolerant (``np.allclose``, after a length check): SQUAD allows the scanner's
# own jitter here, and the tolerances are its own. b-values are compared with
# atol=20, which is why a b=1495 shell pools with a b=1500 one; voxel sizes with
# rtol=1e-2, which covers the last digit of a header. The second number in each
# pair is numpy's own default for the tolerance SQUAD leaves unset, since
# ``np.allclose`` applies both: |a - b| <= atol + rtol * |b|.
TOLERANT_FIELDS = {
    "data_unique_bvals": (20.0, 1e-5),   # (atol, rtol)
    "data_vox_size": (1e-8, 1e-2),
}

# Not compared by SQUAD at all. It labels the group report from the first
# subject in the list, so a difference here is a mislabelled report rather than
# a refusal -- worth saying on the task page, never a reason to split a cohort.
DISCLOSED_FIELDS = (
    "data_protocol",
    "data_unique_pes",
)

# Every field worth describing a subject's acquisition with, in the order a
# reader wants them. The signature is built from the exact ones alone.
REPORTED_FIELDS = EXACT_FIELDS + tuple(TOLERANT_FIELDS) + DISCLOSED_FIELDS

# SQUAD names these in its own error message; naming them the same way lets a
# reader connect a cohort split to the refusal it prevented.
FIELD_MEANING = {
    "data_no_shells": "number of shells",
    "data_unique_bvals": "shell b-values",
    "data_no_PE_dirs": "number of phase-encode directions",
    "data_unique_pes": "phase-encode directions",
    "data_eddy_para": "topup acquisition parameters",
    "data_protocol": "acquisition protocol (volumes per shell)",
    "data_vox_size": "voxel size",
    "data_no_dw_vols": "number of diffusion-weighted volumes",
    "data_no_b0_vols": "number of b=0 volumes",
}

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


def canonical(value: Any) -> str:
    """A value rendered so that two equal values always render identically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def acquisition_of(qc: dict, fields: Sequence[str] = REPORTED_FIELDS) -> Dict[str, Any]:
    """The acquisition, as QUAD described it.

    Values are taken raw: how each one is *compared* is the caller's business --
    exactly (``signature_of``), within SQUAD's tolerance (``tolerance_gaps``), or
    not at all beyond being disclosed.
    """
    return {name: qc.get(name) for name in fields}


def numbers_of(value: Any) -> List[float] | None:
    """``value`` as a list of floats, or None if any part of it is not a number.

    Distinct from ``as_number_list``, which drops what it cannot parse: a
    tolerance comparison has to know that it is not looking at numbers, rather
    than silently compare the remains.
    """
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    out = []
    for item in items:
        number = as_number(item)
        if number is None:
            return None
        out.append(number)
    return out


def close_enough(value: Any, reference: Any, atol: float, rtol: float) -> bool:
    """``np.allclose(value, reference)``, in pure Python, after a length check.

    SQUAD checks the lengths itself for the b-values and relies on numpy for the
    voxel size, where a length mismatch would broadcast or raise rather than
    compare -- so a differing length is a difference either way.

    ``reference`` is numpy's ``b``, the side ``rtol`` scales, and it is the
    first subject in the list: SQUAD compares every subject against that one.
    """
    a, b = numbers_of(value), numbers_of(reference)
    if a is None or b is None:
        return canonical(value) == canonical(reference)
    if len(a) != len(b):
        return False
    return all(abs(x - y) <= atol + rtol * abs(y) for x, y in zip(a, b))


def tolerance_gaps(acquisition: Dict[str, Any], reference: Dict[str, Any],
                   fields: Sequence[str] | None = None) -> List[str]:
    """The tolerant fields where these two subjects are too far apart for SQUAD.

    ``fields`` narrows the check to the tolerant fields still being compared
    that way -- one named in ``signature_fields`` is compared exactly instead,
    and checking it twice would only report the same difference twice.
    """
    names = list(TOLERANT_FIELDS) if fields is None else [
        name for name in fields if name in TOLERANT_FIELDS]
    return [name for name in names
            if not close_enough(acquisition.get(name), reference.get(name),
                                *TOLERANT_FIELDS[name])]


def flags_of(qc: dict) -> Dict[str, bool]:
    return {name: as_bool(qc.get(name)) for name in SQUAD_FLAGS}


def protocol_of(qc: dict) -> Dict[str, Any]:
    """The same acquisition, named for a human reading the task page."""
    return {
        "n_shells": qc.get("data_no_shells"),
        "unique_bvals": as_number_list(qc.get("data_unique_bvals")),
        "n_dw_volumes": qc.get("data_no_dw_vols"),
        "n_b0_volumes": qc.get("data_no_b0_vols"),
        "n_pe_directions": qc.get("data_no_PE_dirs"),
        "voxel_size_mm": as_number_list(qc.get("data_vox_size")),
        "acquisition_parameters": qc.get("data_eddy_para"),
    }


def signature_of(flags: Dict[str, bool], acquisition: Dict[str, Any],
                 fields: Sequence[str] = EXACT_FIELDS) -> str:
    """The cohort key: what must match exactly for SQUAD to pool these subjects.

    Both halves mirror a check SQUAD makes for itself. The flags, because it
    compares them and raises "Eddy output inconsistency detected!". The
    acquisition fields, because it compares those too and raises "Inconsistency
    detected in eddy input data in <description>!".

    Only the fields SQUAD compares *exactly* are in the key, because only those
    partition the subjects: the two it compares with a tolerance are not
    transitive and cannot be a dictionary key at all, so they are applied
    afterwards, against a reference subject, the way SQUAD applies them (see
    ``tolerance_gaps``). The two it does not compare are disclosed, not enforced.
    """
    enabled = ",".join(name[3:-5] for name in SQUAD_FLAGS if flags[name]) or "none"
    parts = ["flags=" + enabled]
    parts.extend("%s=%s" % (name, canonical(acquisition.get(name)))
                 for name in sorted(fields))
    return "|".join(parts)


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


def summarise(qc: dict, labels: Dict[str, str],
              fields: Sequence[str] = EXACT_FIELDS) -> Dict[str, Any]:
    """This subject's database, reduced to what a group run needs to decide.

    ``fields`` is what the signature compares exactly. Everything SQUAD looks at
    is reported either way, so a reader can see the b-values that were compared
    within a tolerance and the protocol that was not compared at all.
    """
    flags = flags_of(qc)
    protocol = protocol_of(qc)
    reported = list(dict.fromkeys(list(REPORTED_FIELDS) + list(fields)))
    acquisition = acquisition_of(qc, reported)
    signature = signature_of(flags, acquisition, fields)
    summary: Dict[str, Any] = dict(labels)
    summary.update({
        "squad_ready": bool(qc),
        "eddy_flags": flags,
        "flag_meanings": {name: FLAG_MEANING[name] for name in SQUAD_FLAGS},
        "protocol": protocol,
        "acquisition": acquisition,
        "field_meanings": {name: FIELD_MEANING.get(name, name) for name in reported},
        "compared": {
            "exact": list(fields),
            "tolerant": {name: {"atol": atol, "rtol": rtol}
                         for name, (atol, rtol) in sorted(TOLERANT_FIELDS.items())
                         if name not in fields},
            "disclosed": [name for name in DISCLOSED_FIELDS if name not in fields],
        },
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
