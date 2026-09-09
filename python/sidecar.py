#!/usr/bin/env python3
"""Find metadata fields in sidecars that are not flat BIDS JSON.

``dcm2niix`` writes a flat BIDS sidecar, so ``meta["SliceTiming"]`` is all the
lookup a well behaved dataset needs.  A raw DICOM parameter dump (dcmstack and
similar tools) describes the same series differently:

* the fields sit inside wrapper objects -- ``global.const``, ``time.samples``
  and so on -- rather than at the top level, and
* a field that dcm2niix reports once is repeated once per volume, so the value
  is a list of per-volume rows instead of the single row callers expect.

Both shapes still describe one acquisition, so fields are looked up by name at
any depth (shallowest match wins, which keeps a real BIDS sidecar behaving
exactly as before), and a per-volume repetition whose rows all agree collapses
back to one row.  Rows that genuinely disagree are an error: this module cannot
know which volume the caller meant.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, List, Tuple

# Per-volume rows are compared with a little slack.  A DICOM dump stores slice
# timings that have been through a decimal round-trip, so the same excitation
# comes back as 697.49999999 in one volume and 697.50000001 in the next; that
# is the same number, not a varying acquisition.
#
# The relative tolerance covers every nonzero value.  The absolute one only
# decides what counts as zero, and these fields are times -- in seconds for
# SliceTiming and TotalReadoutTime, in milliseconds for the mosaic times.  A
# microsecond is far below any real slice spacing or readout in either unit, so
# anything smaller is zero for our purposes, and a first excitation stored as
# 0.0 in one volume and 1e-8 in the next does not fail the comparison.
REL_TOLERANCE = 1e-6
ABS_TOLERANCE = 1e-6


class SidecarError(RuntimeError):
    """Raised when a field is present but not in a shape callers can use."""


def _walk(meta: Any, names: Tuple[str, ...]) -> Tuple[Any, str]:
    """Breadth-first search for the first of ``names`` present in ``meta``.

    Returns ``(value, path)``, or ``(None, "")`` when no name is found.  The
    search is breadth-first so a top-level BIDS key always beats a nested one
    of the same name; ``path`` is the dotted route to the match, for logging.
    """
    queue: deque[Tuple[Any, str]] = deque([(meta, "")])
    while queue:
        node, prefix = queue.popleft()
        if not isinstance(node, dict):
            continue
        for name in names:
            if node.get(name) is not None:
                return node[name], prefix + name
        for key, value in node.items():
            if isinstance(value, dict):
                queue.append((value, "%s%s." % (prefix, key)))
    return None, ""


def _rows_agree(a: List[float], b: List[float]) -> bool:
    if len(a) != len(b):
        return False
    return all(math.isclose(x, y, rel_tol=REL_TOLERANCE, abs_tol=ABS_TOLERANCE)
               for x, y in zip(a, b))


def find_list(meta: dict, *names: str) -> Tuple[List[Any], str]:
    """Find a field whose value is a list, e.g. ``SliceTiming``.

    A list of per-volume rows collapses to the first row when every row agrees.
    Returns ``(value, path)``, or ``(None, "")`` when the field is absent.
    """
    value, path = _walk(meta, names)
    if value is None:
        return None, ""
    if not isinstance(value, list) or not value:
        raise SidecarError("%s is %s, not a non-empty list"
                           % (path, type(value).__name__))

    if all(isinstance(row, list) for row in value):
        # One row per volume.  They describe the same acquisition, so they have
        # to agree before any single row can stand in for the series.
        rows = [[float(x) for x in row] for row in value]
        for i, row in enumerate(rows[1:], start=1):
            if not _rows_agree(rows[0], row):
                raise SidecarError(
                    "%s repeats per volume but volume %d differs from volume 0; "
                    "the series has no single value" % (path, i)
                )
        return rows[0], path
    return value, path


def find_scalar(meta: dict, *names: str) -> Tuple[Any, str]:
    """Find a field whose value is a scalar, e.g. ``PhaseEncodingDirection``.

    A per-volume list collapses to its first entry when every entry agrees.
    Returns ``(value, path)``, or ``(None, "")`` when the field is absent.
    """
    value, path = _walk(meta, names)
    if value is None:
        return None, ""
    if not isinstance(value, list):
        return value, path
    if not value:
        raise SidecarError("%s is an empty list" % path)

    first = value[0]
    for i, entry in enumerate(value[1:], start=1):
        same = (math.isclose(float(first), float(entry), rel_tol=REL_TOLERANCE,
                             abs_tol=ABS_TOLERANCE)
                if isinstance(first, (int, float)) and isinstance(entry, (int, float))
                else first == entry)
        if not same:
            raise SidecarError(
                "%s repeats per volume but volume %d (%r) differs from volume 0 "
                "(%r); the series has no single value" % (path, i, entry, first)
            )
    return first, path
