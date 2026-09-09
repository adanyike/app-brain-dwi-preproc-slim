#!/bin/bash
# The slim app and the full app run the same pipeline; only the container build
# differs.  Everything under src/, python/, templates/ and the shared tests must
# therefore be byte-identical between them.
#
# Two copies of a pipeline drift silently: a fix lands in one and not the other,
# and the two apps quietly start producing different numbers while claiming to
# be the same analysis.  This makes that a test failure instead.
#
# Skipped when the sibling app is not present, so the slim app still works when
# cloned on its own.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$(cd "$HERE/.." && pwd)"
FULL="$(cd "$APP/../brainlife" 2>/dev/null && pwd || true)"

if [ -z "$FULL" ] || [ ! -d "$FULL/src" ]; then
    echo "test_parity.sh: sibling app not found at ../brainlife -- skipping"
    exit 0
fi

# Files that are meant to differ: the container recipe, its helper scripts, the
# app's identity, and the documentation describing each variant.
DIFFERS_BY_DESIGN='^(Dockerfile|\.dockerignore|package\.json|README\.md|CHANGELOG\.md|CLAUDE\.md|docker/|main$|test/test_parity\.sh$)'

PASS=0
FAIL=0
while read -r rel; do
    [[ "$rel" =~ $DIFFERS_BY_DESIGN ]] && continue
    if [ ! -e "$FULL/$rel" ]; then
        printf 'FAIL: %s exists here but not in the full app\n' "$rel" >&2
        FAIL=$((FAIL + 1))
    elif cmp -s "$APP/$rel" "$FULL/$rel"; then
        PASS=$((PASS + 1))
    else
        printf 'FAIL: %s differs from the full app\n' "$rel" >&2
        FAIL=$((FAIL + 1))
    fi
done < <(cd "$APP" && find src python templates test main run.sh config.json.example LICENSE \
              -type f \
              -not -path '*/__pycache__/*' -not -name '*.pyc' \
              2>/dev/null | sed 's|^\./||' | sort)

# `main` differs by design in exactly two places -- the container image it
# defaults to and the PBS job name.  Compare it with those lines removed, so a
# real change to the launcher still shows up as drift.
normalise_main() { grep -vE '^(APP_IMAGE=|#PBS -N )' "$1"; }
PASS=$((PASS + 1))
if [ -e "$FULL/main" ]; then
    if diff -q <(normalise_main "$APP/main") <(normalise_main "$FULL/main") >/dev/null; then
        :
    else
        printf 'FAIL: main differs beyond the image name and job name:\n' >&2
        diff <(normalise_main "$FULL/main") <(normalise_main "$APP/main") | head -20 >&2
        PASS=$((PASS - 1)); FAIL=$((FAIL + 1))
    fi
fi

printf 'test_parity.sh: %d shared files identical, %d differing\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
