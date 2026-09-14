#!/usr/bin/env python3
"""Stage N subjects' QUAD databases for one ``eddy_squad`` run.

This is the part of a group QC task that brainlife cannot do for you. A group
App is given an array of input datasets -- brainlife writes the selected
datasets into ``config.json`` as a JSON array, preserving the order across every
file mapping, and describes them in ``_inputs`` -- and ``eddy_squad`` wants
something quite different: a text file of directories, each holding a
``qc.json``, all of which must come from ``eddy`` runs that used the same
features.

So this script:

* resolves every input to a directory holding a ``qc.json``, whether the dataset
  mapped the file itself or the folder around it;
* names each subject from its own ``squad_ready.json``, else brainlife's
  ``_inputs[i].meta.subject``, else the directory it came in;
* buckets subjects by cohort signature (see eddyqc_summary.py), splits a bucket
  further where a subject falls outside SQUAD's *tolerance* on the two fields it
  compares with one, and picks one cohort -- so a study half-processed on GPU
  nodes yields a group report for the larger half plus an explicit list of who
  was left out, rather than SQUAD's ``ValueError: Eddy output inconsistency
  detected!``;
* optionally (``pool_across_acquisition``) merges cohorts that differ only in
  the topup acquisition parameters, and only within bounds, rewriting the staged
  copies to one reference value and recording every original;
* copies each ``qc.json`` into the work directory, because ``eddy_squad -u``
  writes ``qc_updated.pdf`` *into* the folders it was given and brainlife stages
  its inputs read-only;
* writes the grouping-variable file in SQUAD's own format, in list order, which
  is the only thing tying a value to a subject.

Everything it decided goes into ``cohorts.json`` for the task page and for the
shell driver, which reads ``list_file`` / ``variable_file`` back out of it.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eddyqc_summary import (  # noqa: E402  (path set above)
    DISCLOSED_FIELDS,
    EXACT_FIELDS,
    FIELD_MEANING,
    REPORTED_FIELDS,
    SQUAD_FLAGS,
    FLAG_MEANING,
    TOLERANT_FIELDS,
    acquisition_of,
    as_bool,
    as_number,
    canonical,
    flags_of,
    metrics_of,
    numbers_of,
    protocol_of,
    read_json,
    signature_of,
    tolerance_gaps,
)

# How far apart two sites' readout times may be before `pool_across_acquisition`
# refuses to call them the same acquisition. 0.05 s is wider than the difference
# between two implementations of the same protocol (fractions of a millisecond)
# and far narrower than the difference between two protocols.
DEFAULT_READOUT_TOLERANCE = 0.05

# config.json keys that may carry the QUAD folders, in the order they are
# consulted. `eddyqc` is what the group App maps; the others let the same code
# run from the command line and accept a dataset that mapped qc.json directly.
INPUT_KEYS = ("eddyqc", "qc_folders", "qc_json", "quad_folders")

# Where a qc.json hides inside a staged dataset, relative to what the config
# pointed at. "" means the path itself is the folder holding qc.json.
#
# More than one of these is the same app at different ages: `eddyqc` is the lean
# dataset published for group analysis, `qc/eddy_quad` is the whole task output
# directory of a run that predates it, `eddy_quad` is that run's archived qc
# dataset (brainlife stages a dataset at what was inside output/qc/), and `.qc`
# is eddy_quad's own default. A study processed before the group App existed
# should not have to be reprocessed to take part in it.
# The `output/…` pair is for a task directory named directly, which is what
# someone with older runs on disk reaches for.
QC_SUBDIRS = ("", "eddyqc", "qc", "qc/eddy_quad", "eddy_quad", ".qc",
              "output/eddyqc", "output/qc/eddy_quad")

# Directory names that say nothing about who a subject is, so a label taken from
# the path skips past them.
GENERIC_DIRS = {"", ".", "..", "eddyqc", "qc", "eddy_quad", ".qc",
                "output", "outputs", "work", "subjects"}


class StagingError(Exception):
    """A condition the task cannot proceed from, phrased for the task page."""


# ------------------------------------------------------------- input paths ----

def config_paths(config: dict) -> List[str]:
    """Every input path named in config.json, in the order brainlife listed it."""
    paths: List[str] = []
    for key in INPUT_KEYS:
        value = config.get(key)
        if value in (None, "", []):
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, str) and item.strip():
                paths.append(item.strip())
        if paths:
            break
    return paths


def expand_list_file(paths: Sequence[str]) -> List[str]:
    """A single .txt that is not a qc.json is read as a list of folders.

    That is exactly what eddy_squad itself takes, so someone running this app
    outside brainlife can hand over the list they already have.
    """
    if len(paths) != 1:
        return list(paths)
    only = paths[0]
    if os.path.basename(only) == "qc.json" or not only.endswith(".txt"):
        return list(paths)
    if not os.path.isfile(only):
        return list(paths)
    with open(only) as fh:
        return [line.strip() for line in fh if line.strip() and not line.startswith("#")]


def find_qc_json(path: str) -> str:
    """The qc.json a staged input refers to, or "" when there is none."""
    if os.path.isfile(path):
        return path if os.path.basename(path) == "qc.json" else ""
    if not os.path.isdir(path):
        return ""
    for subdir in QC_SUBDIRS:
        candidate = os.path.join(path, subdir, "qc.json") if subdir else os.path.join(path, "qc.json")
        if os.path.isfile(candidate):
            return candidate
    return ""


# ----------------------------------------------------------------- labels ----

def input_meta(config: dict) -> List[Dict[str, Any]]:
    """subject/session per entry of brainlife's `_inputs`, in order.

    Each entry also carries the ids brainlife names the staged directory after
    (`../<task id>/<subdir>`), so an entry can be tied to a path rather than to
    its position in the array.
    """
    out = []
    for entry in config.get("_inputs") or []:
        if not isinstance(entry, dict):
            out.append({"subject": "", "session": "", "tokens": []})
            continue
        meta = entry.get("meta") or {}
        tokens = [str(entry.get(key)) for key in ("task_id", "subdir", "dataset_id", "id")
                  if entry.get(key)]
        out.append({
            "subject": str(meta.get("subject") or "").strip(),
            "session": str(meta.get("session") or "").strip(),
            "tokens": [token for token in tokens if len(token) > 8],
        })
    return out


def meta_for(index: int, path: str, metas: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The `_inputs` entry describing this path.

    brainlife stages each dataset under a directory named after the task that
    produced it, so matching that id in the path ties an entry to its dataset
    without trusting the array order. Position is the fallback, which is what the
    documented ordering guarantee gives us.
    """
    absolute = os.path.abspath(path)
    for meta in metas:
        if any(token in absolute for token in meta.get("tokens", [])):
            return meta
    if index < len(metas):
        return metas[index]
    return {"subject": "", "session": ""}


def sanitise(label: str) -> str:
    """A label safe as a directory name; SQUAD prints these into its report."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._")
    return cleaned or "subject"


def label_for(index: int, path: str, qc_json: str, summary: dict,
              metas: Sequence[Dict[str, Any]], overrides: Sequence[str]) -> Tuple[str, str]:
    """(subject, session), from the most trustworthy source available."""
    if index < len(overrides) and overrides[index]:
        return overrides[index], ""
    # The label this pipeline itself recorded when it wrote the database: the
    # only source that cannot be knocked out of step by the staging order.
    if summary.get("subject") and summary["subject"] != "subject":
        return str(summary["subject"]), str(summary.get("session") or "")
    meta = meta_for(index, path, metas)
    if meta.get("subject"):
        return meta["subject"], meta.get("session", "")
    # Nothing recorded the subject, so fall back to the path. Walk up past the
    # directories that name a *kind* of output rather than a subject -- an older
    # task's qc.json sits at <task>/output/qc/eddy_quad/qc.json, four levels of
    # them -- and take the first name that could be an identifier.
    folder = os.path.abspath(os.path.dirname(qc_json) or path)
    for _ in range(5):
        candidate = os.path.basename(folder)
        if candidate.lower() not in GENERIC_DIRS:
            return candidate, ""
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return "sub-%02d" % (index + 1), ""


def unique_labels(labels: Sequence[str]) -> List[str]:
    """Disambiguate repeats -- two sessions of one subject, say -- in order."""
    seen: Dict[str, int] = {}
    out = []
    for label in labels:
        seen[label] = seen.get(label, 0) + 1
        out.append(label if seen[label] == 1 else "%s-%d" % (label, seen[label]))
    return out


# --------------------------------------------------------------- subjects ----

def signature_fields(config: dict) -> List[str]:
    """Which acquisition fields the cohort key compares *exactly*.

    The five SQUAD compares with ``!=`` by default -- no more and no less. A key
    looser than SQUAD's fails the whole study rather than one cohort; a key
    stricter than SQUAD's splits a cohort SQUAD would have pooled, and says
    nothing about having done so. The two fields SQUAD compares with a tolerance
    are applied separately (see ``bucket``), and the two it does not compare at
    all are only disclosed.

    Narrowing this is a deliberate choice -- if your FSL tolerates a difference,
    a subject with one dropped volume say, drop the field here and those subjects
    pool again. So is widening it: naming ``data_unique_bvals`` or
    ``data_vox_size`` here compares that field exactly instead of within SQUAD's
    tolerance, and any other ``data_*`` field can be added to split on something
    SQUAD does not check at all.
    """
    requested = config.get("signature_fields")
    if requested in (None, "", []):
        return list(EXACT_FIELDS)
    if isinstance(requested, str):
        requested = [name.strip() for name in requested.replace(",", " ").split()]
    return [name for name in requested if name]


def tolerant_fields(exact: Sequence[str]) -> List[str]:
    """The fields still compared within SQUAD's tolerance, given the exact set."""
    return [name for name in TOLERANT_FIELDS if name not in exact]


def collect(config: dict, overrides: Sequence[str]) -> Tuple[List[dict], List[dict]]:
    """(usable subjects, unusable inputs) -- each as a plain dict for the report.

    Each subject carries its whole acquisition, not only the part the signature
    compares: the tolerance pass and the disclosure both need the fields the key
    leaves out.
    """
    fields = signature_fields(config)
    # Everything SQUAD looks at, plus anything the config asked to compare.
    reported = list(dict.fromkeys(list(REPORTED_FIELDS) + list(fields)))
    paths = expand_list_file(config_paths(config))
    if not paths:
        raise StagingError(
            "no QUAD folders were given. Map the group App's 'eddyqc' input to "
            "the per-subject eddy QC datasets (each holding a qc.json), or set "
            "'qc_folders' in config.json to a list of folders.")

    metas = input_meta(config)
    subjects: List[dict] = []
    rejected: List[dict] = []
    raw_labels: List[str] = []

    for index, path in enumerate(paths):
        qc_json = find_qc_json(path)
        if not qc_json:
            rejected.append({"input": path, "reason":
                             "no qc.json found at or under this path"})
            continue
        qc = read_json(qc_json)
        if not qc:
            rejected.append({"input": path, "reason":
                             "qc.json is empty or not readable JSON"})
            continue

        summary = read_json(os.path.join(os.path.dirname(qc_json), "squad_ready.json"))
        subject, session = label_for(index, path, qc_json, summary, metas, overrides)
        flags = flags_of(qc)
        protocol = protocol_of(qc)
        acquisition = acquisition_of(qc, reported)
        raw_labels.append(sanitise(subject if not session else "%s_%s" % (subject, session)))
        subjects.append({
            "input": path,
            "qc_json": qc_json,
            "subject": subject,
            "session": session,
            "signature": signature_of(flags, acquisition, fields),
            "eddy_flags": flags,
            "protocol": protocol,
            "acquisition": acquisition,
            "metrics": metrics_of(qc),
        })

    for subject, label in zip(subjects, unique_labels(raw_labels)):
        subject["label"] = label
    return subjects, rejected


def split_by_tolerance(members: Sequence[dict], fields: Sequence[str]) -> List[List[dict]]:
    """Members grouped so that each group is within SQUAD's tolerance of its first.

    SQUAD compares every subject against ``ref_data`` -- the first folder in the
    list it was given -- so "close enough" is a relation to one subject, not a
    property of a pair. That makes it non-transitive: b=1490, b=1500 and b=1515
    are each within 20 of their neighbour and the ends are not within 20 of each
    other. Resolving it against a reference subject, and starting a new group
    from the first member that does not fit, is how SQUAD resolves it too.
    """
    parts: List[List[dict]] = []
    remaining = list(members)
    while remaining:
        reference = remaining[0]
        near, far = [reference], []
        for member in remaining[1:]:
            gaps = tolerance_gaps(member["acquisition"], reference["acquisition"], fields)
            (far if gaps else near).append(member)
        parts.append(near)
        remaining = far
    return parts


def tolerance_key(reference: dict, fields: Sequence[str]) -> str:
    """What tells one tolerance group from another: its reference's own values."""
    return "|".join("%s~%s" % (name, canonical(reference["acquisition"].get(name)))
                    for name in sorted(fields))


def bucket(subjects: Sequence[dict], fields: Sequence[str] = ()) -> List[dict]:
    """Subjects grouped into cohorts SQUAD would accept, largest cohort first.

    Two passes, because SQUAD makes two kinds of comparison. The signature is a
    dictionary key, since the fields in it are compared exactly. The tolerant
    fields (``fields``) cannot be: they are applied within each bucket, against
    a reference subject, and split it only where a subject is genuinely too far
    from that reference.
    """
    groups: Dict[str, List[dict]] = {}
    for subject in subjects:
        groups.setdefault(subject["signature"], []).append(subject)

    cohorts = []
    for signature, members in groups.items():
        parts = split_by_tolerance(members, fields) if fields else [list(members)]
        for part in parts:
            # Only a bucket that actually split needs its tolerant values in the
            # key. Leaving them out otherwise keeps a cohort's hash identical to
            # the one each of its subjects published for itself.
            key = signature if len(parts) == 1 else "%s|%s" % (
                signature, tolerance_key(part[0], fields))
            cohorts.append({"signature": key,
                            "n_subjects": len(part),
                            "subjects": [m["label"] for m in part],
                            "members": part})
    # Largest first, then alphabetically, so the choice does not depend on the
    # order brainlife happened to stage the datasets in.
    cohorts.sort(key=lambda c: (-c["n_subjects"], c["signature"]))
    return cohorts


def abbreviate(value: Any, limit: int = 60) -> str:
    """A value short enough for a message, without hiding what it is."""
    text = canonical(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def differences_between(chosen: dict, other: dict, exact: Sequence[str],
                        tolerant: Sequence[str] = ()) -> List[str]:
    """Every reason SQUAD would not pool these two subjects, in plain terms.

    Compared from the values themselves rather than from the signature string: a
    reader needs to see that one group has a readout of 0.0959 and the other
    0.1043, not that two hashes differ.
    """
    reasons = []
    for name in SQUAD_FLAGS:
        here, there = bool(chosen["eddy_flags"].get(name)), bool(other["eddy_flags"].get(name))
        if here != there:
            reasons.append("%s %s" % (FLAG_MEANING[name],
                                      "missing here" if there else "present here"))
    for name in sorted(exact):
        value, theirs = chosen["acquisition"].get(name), other["acquisition"].get(name)
        if canonical(value) != canonical(theirs):
            reasons.append("%s differs (%s vs %s)"
                           % (FIELD_MEANING.get(name, name), abbreviate(value),
                              abbreviate(theirs)))
    for name in tolerance_gaps(other["acquisition"], chosen["acquisition"], tolerant):
        reasons.append("%s differs by more than SQUAD tolerates (%s vs %s)"
                       % (FIELD_MEANING.get(name, name),
                          abbreviate(chosen["acquisition"].get(name)),
                          abbreviate(other["acquisition"].get(name))))
    return reasons


def explain_difference(chosen: dict, other: dict, exact: Sequence[str] = EXACT_FIELDS,
                       tolerant: Sequence[str] = ()) -> str:
    """Why these two cohorts cannot be pooled, naming the field and its values."""
    return "; ".join(differences_between(chosen, other, exact, tolerant)) or "signature differs"


def disclosed_differences(members: Sequence[dict]) -> List[dict]:
    """Fields that differ within a cohort but that SQUAD never compares.

    Not a reason to split anything -- SQUAD pools these happily. But it labels
    the group report from the first subject in the list, so a protocol printed
    on the front page may describe only some of the subjects behind it. Worth
    saying; never worth enforcing.
    """
    out = []
    for name in DISCLOSED_FIELDS:
        values: Dict[str, List[str]] = {}
        for member in members:
            values.setdefault(canonical(member["acquisition"].get(name)), []).append(
                member["label"])
        if len(values) < 2:
            continue
        out.append({
            "field": name,
            "meaning": FIELD_MEANING.get(name, name),
            "values": [{"value": json.loads(value), "subjects": labels}
                       for value, labels in sorted(values.items(),
                                                   key=lambda kv: (-len(kv[1]), kv[0]))],
        })
    return out


# ------------------------------------------- pooling across the acquisition ----

def acqp_rows(value: Any) -> List[Tuple[Tuple[float, ...], float]] | None:
    """``data_eddy_para`` as [(phase-encode vector, readout time)], or None.

    QUAD writes the acqparams table **flat** -- one row of [x, y, z, readout]
    after another in a single list of 4N numbers, which is what a real qc.json
    carries. A nested list of rows is accepted too, since that is the shape the
    same table takes anywhere it has been reshaped before reaching us, and
    reading only one of the two would be reading the format we invented rather
    than the one eddy_quad writes.

    None means it is neither, in which case nothing here can say what differs
    between two of them, and harmonising is out.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return None

    if all(isinstance(row, (list, tuple)) for row in value):
        rows = []
        for row in value:
            numbers = numbers_of(row)
            if numbers is None or len(numbers) < 4:
                return None
            rows.append((tuple(numbers[:3]), numbers[3]))
        return rows

    numbers = numbers_of(value)
    if numbers is None or len(numbers) < 4 or len(numbers) % 4:
        return None
    return [(tuple(numbers[index:index + 3]), numbers[index + 3])
            for index in range(0, len(numbers), 4)]


def harmonisable(reference: Any, other: Any, tolerance: float) -> str:
    """Empty when ``other``'s acquisition parameters may be rewritten as
    ``reference``'s; otherwise the reason they may not.

    The bound is what makes this a description of one acquisition rather than a
    claim about two: the same phase-encode vectors, and readout times close
    enough that the difference is how the number was derived rather than what
    was acquired. Anything else is a different acquisition, and saying otherwise
    in the staged database would be a lie SQUAD cannot catch.
    """
    here, there = acqp_rows(reference), acqp_rows(other)
    if here is None or there is None:
        return ("the acquisition parameters are not a table of [x, y, z, readout] "
                "rows, so there is nothing to compare")
    if len(here) != len(there):
        return ("they have %d and %d acquisition parameter row(s): a different "
                "number of phase-encode series is a different acquisition"
                % (len(here), len(there)))
    if {vector for vector, _ in here} != {vector for vector, _ in there}:
        return ("the phase-encode vectors themselves differ (%s vs %s) -- that is "
                "a different acquisition, not the same one described differently"
                % (abbreviate([list(v) for v in sorted({v for v, _ in here})]),
                   abbreviate([list(v) for v in sorted({v for v, _ in there})])))
    gap = max(abs(x - y) for _, x in here for _, y in there)
    if gap > tolerance:
        return ("the readout times differ by %.6g s, more than "
                "pool_readout_tolerance (%.6g s)" % (gap, tolerance))
    return ""


def pool_across_acquisition(cohorts: List[dict], anchor: dict, tolerance: float,
                            exact: Sequence[str], tolerant: Sequence[str]
                            ) -> Tuple[List[dict], Dict[str, Any]]:
    """Merge into ``anchor`` the cohorts that differ from it only in
    ``data_eddy_para``, and only within ``tolerance``.

    Returns the remaining cohorts and what was harmonised. A cohort that differs
    in anything else is left alone -- it is a different acquisition and the
    ordinary exclusion explains it. One that differs only here but out of bounds
    is left alone too, carrying the reason it was refused, because the whole
    value of this option is that its limits are stated.
    """
    others = [name for name in exact if name != "data_eddy_para"]
    reference_member = anchor["members"][0]
    reference = reference_member["acquisition"].get("data_eddy_para")

    absorbed: List[dict] = []
    for cohort in cohorts:
        if cohort is anchor:
            continue
        member = cohort["members"][0]
        if differences_between(reference_member, member, others, tolerant):
            continue  # a different acquisition in more than its parameters
        # Every member, not only the one the other fields were read from: a
        # narrowed signature_fields can leave a cohort whose members do not all
        # share a data_eddy_para, and rewriting one that was never checked is
        # exactly the claim this function exists to refuse.
        refused = next((reason for reason in
                        (harmonisable(reference, m["acquisition"].get("data_eddy_para"),
                                      tolerance) for m in cohort["members"])
                        if reason), "")
        if refused:
            cohort["harmonisation_declined"] = refused
            continue
        absorbed.append(cohort)

    if not absorbed:
        return cohorts, {}

    per_subject: Dict[str, Any] = {}
    for cohort in absorbed:
        for member in cohort["members"]:
            per_subject[member["label"]] = member["acquisition"].get("data_eddy_para")
            # Applied by stage(), to our own copy of the database -- never to the
            # input, which is read-only and is the record of what was acquired.
            member["harmonise_to"] = reference

    anchor["members"] = list(anchor["members"]) + [
        member for cohort in absorbed for member in cohort["members"]]
    anchor["n_subjects"] = len(anchor["members"])
    anchor["subjects"] = [member["label"] for member in anchor["members"]]
    anchor["harmonised"] = {
        "field": "data_eddy_para",
        "meaning": FIELD_MEANING["data_eddy_para"],
        "reference": reference,
        "reference_subject": reference_member["label"],
        "readout_tolerance": tolerance,
        "per_subject": per_subject,
    }

    gone = {id(cohort) for cohort in absorbed}
    remaining = [cohort for cohort in cohorts if id(cohort) not in gone]
    remaining.sort(key=lambda c: (-c["n_subjects"], c["signature"]))
    return remaining, anchor["harmonised"]


def choose(cohorts: Sequence[dict], requested: str, require_homogeneous: bool) -> dict:
    if requested:
        for cohort in cohorts:
            if requested in (cohort["signature"], cohort["signature_hash"]):
                return cohort
        raise StagingError(
            "cohort '%s' matches none of the %d cohort(s) present: %s"
            % (requested, len(cohorts),
               ", ".join("%s (%d subjects)" % (c["signature_hash"], c["n_subjects"])
                         for c in cohorts)))
    if require_homogeneous and len(cohorts) > 1:
        raise StagingError(
            "the inputs split into %d incompatible cohorts and "
            "require_homogeneous is set: %s. eddy_squad can only pool subjects "
            "whose eddy runs used the same features."
            % (len(cohorts),
               "; ".join("%s: %d subject(s) [%s]"
                         % (c["signature_hash"], c["n_subjects"], c["signature"])
                         for c in cohorts)))
    return cohorts[0]


# ------------------------------------------------------ grouping variable ----

def read_variable_table(path: str) -> Tuple[str, Dict[str, str]]:
    """(column name, {subject: value}) from a participants-style CSV/TSV."""
    with open(path, newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect: Any = csv.Sniffer().sniff(sample, delimiters="\t,; ")
        except csv.Error:
            dialect = csv.excel_tab
        rows = [row for row in csv.reader(fh, dialect) if row and any(f.strip() for f in row)]

    if len(rows) < 2:
        raise StagingError("grouping variable table '%s' has no data rows" % path)
    header = [field.strip() for field in rows[0]]
    lowered = [field.lower() for field in header]
    subject_col = next((i for i, name in enumerate(lowered)
                        if name in ("subject", "participant_id", "subject_id", "sub", "id")), None)
    if subject_col is None:
        raise StagingError(
            "grouping variable table '%s' needs a subject column (one of "
            "subject, participant_id, subject_id, id); found: %s"
            % (path, ", ".join(header)))
    value_col = next((i for i in range(len(header)) if i != subject_col), None)
    if value_col is None:
        raise StagingError("grouping variable table '%s' has no value column" % path)

    values = {}
    for row in rows[1:]:
        if len(row) <= max(subject_col, value_col):
            continue
        subject = row[subject_col].strip()
        if subject:
            values[subject] = row[value_col].strip()
    return header[value_col], values


def looks_like_squad_file(path: str) -> bool:
    """SQUAD's own format: a name, then 0 or 1, then one value per subject."""
    with open(path) as fh:
        lines = [line.strip() for line in fh if line.strip()]
    return len(lines) > 2 and lines[1] in ("0", "1")


def encode_values(values: Sequence[str], continuous: bool, name: str
                  ) -> Tuple[List[str], Dict[str, str]]:
    """(values eddy_squad can read, {code: original name}).

    SQUAD reads the grouping file with ``np.genfromtxt(gVar, dtype=None,
    names=True)``, so every line of the column has to parse as one numeric
    type. A site name gets as far as ``ValueError: Cannot convert string
    'MCG'`` and takes the whole run down before a plot is drawn -- so names are
    encoded to integers here, and the mapping is published rather than left in
    the analyst's head.

    Values that are already numeric are passed through untouched: an existing
    file of 0s and 1s must reach SQUAD exactly as it was written, not renumbered
    behind the author's back.

    Classes are numbered in sorted order, so the same table yields the same
    codes on every run, whatever order brainlife staged the subjects in.
    """
    if all(as_number(value) is not None for value in values):
        return list(values), {}

    names = sorted({str(value) for value in values})
    if continuous:
        raise StagingError(
            "the grouping variable '%s' is marked continuous, but %d of its "
            "values are not numbers (%s). A regression needs numbers: unset "
            "variable_is_continuous to treat them as classes, or give the "
            "column numeric values."
            % (name or "?", sum(1 for v in values if as_number(v) is None),
               ", ".join(names[:6]) + ("..." if len(names) > 6 else "")))

    codes = {label: str(index) for index, label in enumerate(names)}
    return ([codes[str(value)] for value in values],
            {code: label for label, code in codes.items()})


def write_variable_file(path: str, name: str, continuous: bool,
                        members: Sequence[dict], out_path: str) -> dict:
    """Write SQUAD's grouping file for this cohort, in list order.

    SQUAD matches values to subjects by line position, so the order here has to
    be the order of the folder list -- nothing else connects the two.
    """
    if looks_like_squad_file(path):
        with open(path) as fh:
            lines = [line.strip() for line in fh if line.strip()]
        values = lines[2:]
        if len(values) != len(members):
            raise StagingError(
                "grouping variable file '%s' is in eddy_squad's own format and "
                "carries %d values, but this cohort has %d subject(s). Values "
                "are matched by position, so the counts must agree -- or supply "
                "a table with a subject column instead, which is matched by name."
                % (path, len(values), len(members)))
        encoded, encoding = encode_values(values, lines[1] == "1", lines[0])
        if encoding:
            with open(out_path, "w") as fh:
                fh.write("%s\n%s\n" % (lines[0], lines[1]))
                fh.write("".join("%s\n" % value for value in encoded))
        else:
            shutil.copyfile(path, out_path)
        return {"name": lines[0], "continuous": lines[1] == "1",
                "source": "eddy_squad format file, matched by position",
                "n_values": len(values), "missing": [], "encoding": encoding}

    column, table = read_variable_table(path)
    missing = [m["label"] for m in members
               if not (table.get(m["subject"]) or table.get(m["label"]))]
    if missing:
        raise StagingError(
            "the grouping variable table has no row for %d of this cohort's "
            "subjects (%s). eddy_squad needs one value per subject; add the "
            "missing rows, or drop those subjects from the input."
            % (len(missing), ", ".join(missing[:10]) + ("..." if len(missing) > 10 else "")))

    values = [table.get(m["subject"]) or table.get(m["label"]) for m in members]
    encoded, encoding = encode_values(values, continuous, name or column)
    with open(out_path, "w") as fh:
        fh.write("%s\n%s\n" % (name or column, "1" if continuous else "0"))
        fh.write("".join("%s\n" % value for value in encoded))
    return {"name": name or column, "continuous": continuous,
            "source": "table '%s', matched by subject name" % os.path.basename(path),
            "n_values": len(values), "missing": [], "encoding": encoding}


# ------------------------------------------------------------------ stage ----

def stage(cohort: dict, work_dir: str) -> Tuple[str, List[str]]:
    """Copy each qc.json into our own tree and write the folder list.

    Returns the list file and the subjects that arrived without a qc.pdf:
    SQUAD's update step opens ``<folder>/qc.pdf`` for every listed subject to
    append the study-wise pages to it, so one missing report would take the
    whole group run down with it.
    """
    subjects_dir = os.path.join(work_dir, "subjects")
    os.makedirs(subjects_dir, exist_ok=True)
    list_file = os.path.join(work_dir, "list.txt")
    missing_reports = []
    with open(list_file, "w") as fh:
        for member in cohort["members"]:
            folder = os.path.join(subjects_dir, member["label"])
            os.makedirs(folder, exist_ok=True)
            # Copy with qc_path repointed at the staged folder. SQUAD locates the
            # single-subject files from the folder list rather than this field,
            # but leaving it pointing into a read-only input dataset is an
            # invitation for some future version to try writing there.
            database = read_json(member["qc_json"])
            database["qc_path"] = folder
            # pool_across_acquisition: the cohort agreed to describe itself with
            # one set of topup parameters. This is where that is written, and it
            # is written here and nowhere else -- the copy this app made, not the
            # input dataset, which stays the record of what was acquired.
            if member.get("harmonise_to") is not None:
                database["data_eddy_para"] = member["harmonise_to"]
            with open(os.path.join(folder, "qc.json"), "w") as out:
                json.dump(database, out, indent=4, sort_keys=True)
            source_pdf = os.path.join(os.path.dirname(member["qc_json"]), "qc.pdf")
            if os.path.isfile(source_pdf):
                shutil.copyfile(source_pdf, os.path.join(folder, "qc.pdf"))
            else:
                missing_reports.append(member["label"])
            member["staged"] = folder
            fh.write("%s\n" % folder)
    return list_file, missing_reports


def diagnose(list_file: str) -> int:
    """Say which fields differ across an already-staged cohort, and how.

    For when eddy_squad refuses a cohort anyway: a future FSL may compare a
    field this app does not know to compare, and "Inconsistency detected in eddy
    input data in <something>!" is not enough to act on. Comparing every field
    ourselves turns it into the subject list and the two values.
    """
    with open(list_file) as fh:
        folders = [line.strip() for line in fh if line.strip()]

    databases = [(os.path.basename(folder), read_json(os.path.join(folder, "qc.json")))
                 for folder in folders]
    databases = [(label, qc) for label, qc in databases if qc]
    if len(databases) < 2:
        print("squad_inputs: fewer than two databases to compare", file=sys.stderr)
        return 1

    # Every field QUAD writes that describes the data rather than this subject's
    # own files -- a superset of what any version of SQUAD compares.
    names = sorted({name for _, qc in databases for name in qc
                    if name.startswith("data_") and not name.startswith("data_file_")})
    differing = 0
    for name in names:
        values: Dict[str, List[str]] = {}
        for label, qc in databases:
            values.setdefault(canonical(qc.get(name)), []).append(label)
        if len(values) < 2:
            continue
        differing += 1
        print("%s (%s) differs across subjects%s:"
              % (name, FIELD_MEANING.get(name, "no description"),
                 " -- but SQUAD compares this one within a tolerance, so it is "
                 "unlikely to be the refusal" if name in TOLERANT_FIELDS else ""),
              file=sys.stderr)
        for value, labels in sorted(values.items(), key=lambda kv: -len(kv[1])):
            print("    %-3d subject(s): %s   [%s]"
                  % (len(labels), abbreviate(json.loads(value), 80),
                     ", ".join(labels[:6]) + ("…" if len(labels) > 6 else "")),
                  file=sys.stderr)

    if not differing:
        print("squad_inputs: every eddy input field matches across these subjects; "
              "the refusal is about something this app does not compare",
              file=sys.stderr)
    else:
        print("squad_inputs: add the field(s) above to 'signature_fields' in "
              "config.json to split these subjects into cohorts instead",
              file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="the task's config.json")
    ap.add_argument("--work-dir", help="where to stage the cohort")
    ap.add_argument("--out", help="cohorts.json to write")
    ap.add_argument("--min-subjects", type=int, default=2)
    ap.add_argument("--diagnose", metavar="LIST_FILE",
                    help="compare a staged cohort's eddy input fields and exit")
    args = ap.parse_args(argv)

    if args.diagnose:
        return diagnose(args.diagnose)
    for required in ("config", "work_dir", "out"):
        if not getattr(args, required):
            ap.error("--%s is required" % required.replace("_", "-"))

    config = read_json(args.config)
    if not config:
        print("squad_inputs: %s is missing or not readable JSON" % args.config,
              file=sys.stderr)
        return 1

    os.makedirs(args.work_dir, exist_ok=True)
    report: Dict[str, Any] = {"cohorts": [], "excluded": [], "rejected": []}

    try:
        overrides = config.get("subject_labels") or []
        if isinstance(overrides, str):
            overrides = [label.strip() for label in overrides.split(",")]
        subjects, rejected = collect(config, overrides)
        report["rejected"] = rejected
        report["n_inputs"] = len(subjects) + len(rejected)

        if len(subjects) < args.min_subjects:
            raise StagingError(
                "only %d input(s) carried a usable qc.json, and a group report "
                "needs at least %d. %s"
                % (len(subjects), args.min_subjects,
                   "Rejected: " + "; ".join("%s (%s)" % (r["input"], r["reason"])
                                            for r in rejected)
                   if rejected else "Check that the inputs are eddy QC datasets."))

        exact = signature_fields(config)
        tolerant = tolerant_fields(exact)
        cohorts = bucket(subjects, tolerant)
        # Hash each cohort's signature the way a single subject hashes its own,
        # so the group page and the subject pages show the same short string.
        for cohort in cohorts:
            cohort["signature_hash"] = hashlib.sha1(
                cohort["signature"].encode()).hexdigest()[:8]

        requested = str(config.get("cohort") or "").strip()
        harmonised: Dict[str, Any] = {}
        if as_bool(config.get("pool_across_acquisition")):
            tolerance = as_number(config.get("pool_readout_tolerance"))
            if tolerance is None:
                tolerance = DEFAULT_READOUT_TOLERANCE
            # Merge into the cohort that would have been reported on anyway, so
            # the option changes who is pooled and not which acquisition the
            # group report describes.
            anchor = next((c for c in cohorts
                           if requested in (c["signature"], c["signature_hash"])),
                          cohorts[0]) if requested else cohorts[0]
            cohorts, harmonised = pool_across_acquisition(
                cohorts, anchor, tolerance, exact, tolerant)

        chosen = choose(cohorts, requested, bool(config.get("require_homogeneous")))

        # The count that matters is the cohort's, not the input list's. Subjects
        # that each land in a cohort of their own pass the check above and would
        # then hand eddy_squad a single folder -- a "study-wise" report of one
        # subject, which looks like a result and is not one.
        if chosen["n_subjects"] < args.min_subjects:
            report["cohorts"] = [{k: c[k] for k in ("signature", "signature_hash",
                                                    "n_subjects", "subjects")}
                                 for c in cohorts]
            raise StagingError(
                "the largest cohort has %d subject(s) and a group report needs "
                "at least %d: these subjects cannot be pooled with each other. "
                "%d cohort(s) present -- %s. They differ in: %s. Process the "
                "subjects you want to compare the same way, or set "
                "signature_fields to ignore a difference your FSL tolerates; "
                "min_subjects lets a smaller group through if you really want "
                "one."
                % (chosen["n_subjects"], args.min_subjects, len(cohorts),
                   "; ".join("%s (%s)" % (c["signature_hash"], ", ".join(c["subjects"]))
                             for c in cohorts),
                   explain_difference(chosen["members"][0],
                                      next(c["members"][0] for c in cohorts
                                           if c["signature"] != chosen["signature"]),
                                      exact, tolerant)
                   if len(cohorts) > 1 else "nothing -- there is only one cohort"))

        list_file, missing_reports = stage(chosen, args.work_dir)
        report["missing_reports"] = missing_reports

        variable: Dict[str, Any] = {}
        variable_path = str(config.get("grouping_variable") or "").strip()
        if variable_path:
            if not os.path.isfile(variable_path):
                raise StagingError("grouping_variable '%s' does not exist" % variable_path)
            variable = write_variable_file(
                variable_path,
                str(config.get("variable_name") or "").strip(),
                bool(config.get("variable_is_continuous")),
                chosen["members"],
                os.path.join(args.work_dir, "variable.txt"))
            variable["file"] = os.path.join(args.work_dir, "variable.txt")

        report.update({
            "list_file": list_file,
            "variable_file": variable.get("file", ""),
            "variable": variable,
            "chosen": {"signature": chosen["signature"],
                       "signature_hash": chosen["signature_hash"],
                       "n_subjects": chosen["n_subjects"],
                       "subjects": chosen["subjects"],
                       # What was rewritten to make this one cohort, if
                       # anything, and what every subject's own value was.
                       "harmonised": harmonised,
                       # Differences SQUAD does not compare, so they pooled --
                       # but the group report is labelled from one subject.
                       "disclosed_differences": disclosed_differences(chosen["members"]),
                       # "the largest cohort" is only meaningful when it *is*
                       # larger. On a tie the pick is deterministic but
                       # arbitrary, and a reader would take the report to
                       # describe the majority of the study.
                       "tied_with": [c["signature_hash"] for c in cohorts
                                     if c["signature"] != chosen["signature"]
                                     and c["n_subjects"] == chosen["n_subjects"]],
                       "protocol": chosen["members"][0]["protocol"],
                       "eddy_flags": chosen["members"][0]["eddy_flags"]},
            "cohorts": [{k: c[k] for k in ("signature", "signature_hash",
                                           "n_subjects", "subjects")}
                        for c in cohorts],
            "excluded": [
                {"signature": c["signature"], "signature_hash": c["signature_hash"],
                 "subjects": c["subjects"],
                 "reason": explain_difference(chosen["members"][0], c["members"][0],
                                              exact, tolerant)
                 + ("; not harmonised: " + c["harmonisation_declined"]
                    if c.get("harmonisation_declined") else "")}
                for c in cohorts if c["signature"] != chosen["signature"]],
            "subjects": [{k: m[k] for k in ("label", "subject", "session", "signature",
                                            "metrics", "input")}
                         for m in chosen["members"]],
            "flag_meanings": {name: FLAG_MEANING[name] for name in SQUAD_FLAGS},
            # How each field was compared, so a cohort split can be read against
            # the rule that produced it rather than taken on trust.
            "comparison": {
                "exact": exact,
                "tolerant": {name: {"atol": TOLERANT_FIELDS[name][0],
                                    "rtol": TOLERANT_FIELDS[name][1]}
                             for name in tolerant},
                "disclosed": [name for name in DISCLOSED_FIELDS if name not in exact],
            },
        })
    except StagingError as exc:
        report["error"] = str(exc)
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2)
        print("squad_inputs: %s" % exc, file=sys.stderr)
        return 1

    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2)

    print("squad_inputs: cohort %s -- %d subject(s) staged%s"
          % (chosen["signature_hash"], chosen["n_subjects"],
             ", %d excluded" % sum(len(c["subjects"]) for c in report["excluded"])
             if report["excluded"] else ""),
          file=sys.stderr)

    # Loud, because the staged databases no longer say what the inputs said.
    if harmonised:
        print("squad_inputs: WARNING: pool_across_acquisition rewrote %s for %d "
              "subject(s) to %s, the value from %s, so that they pool. Original "
              "values: %s. Distortion-derived indices (qc_vox_displ_std) depend "
              "on those parameters and are not comparable across the harmonised "
              "subjects; motion, outlier and CNR indices are unaffected."
              % (harmonised["field"], len(harmonised["per_subject"]),
                 canonical(harmonised["reference"]), harmonised["reference_subject"],
                 "; ".join("%s=%s" % (label, canonical(value))
                           for label, value in sorted(harmonised["per_subject"].items()))),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
