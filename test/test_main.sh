#!/bin/bash
# Tests for the container dispatch in ./main -- the parts that decide what gets
# mounted and which runtime is used.  No neuroimaging toolchain needed: each
# case is expected to fail before any real work starts, and the assertion is on
# which error comes back.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$(cd "$HERE/.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
check() {  # check <description> <expected substring> <actual output>
    if grep -qF -- "$2" <<< "$3"; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s\n  expected to contain: %s\n  got:\n%s\n' "$1" "$2" "$3" >&2
    fi
}

run_main() {  # run_main <env assignments...>
    ( cd "$TMP" && env "$@" bash "$APP/main" 2>&1 )
}

# A non-existent bind path is caught up front, rather than silently producing an
# empty directory inside the container where the data should be.
out="$(run_main SKIP_CONTAINER=1 EXTRA_BIND=/no/such/path)"
check "missing EXTRA_BIND path is rejected" \
      "EXTRA_BIND path does not exist: /no/such/path" "$out"

# Valid bind paths get past the mount setup; the run then stops on the missing
# config.json, which is how we know the binds were accepted.
out="$(run_main SKIP_CONTAINER=1 EXTRA_BIND=/tmp,/usr)"
check "comma-separated EXTRA_BIND is accepted" "no config.json" "$out"

out="$(run_main SKIP_CONTAINER=1 "EXTRA_BIND=/tmp /usr")"
check "space-separated EXTRA_BIND is accepted" "no config.json" "$out"

out="$(run_main SKIP_CONTAINER=1)"
check "no EXTRA_BIND is fine" "no config.json" "$out"

# A .sif can only be run by singularity; if it is missing, say so plainly
# instead of handing the filename to docker.
if ! command -v singularity >/dev/null 2>&1 && command -v docker >/dev/null 2>&1; then
    out="$(run_main APP_IMAGE=/some/image.sif)"
    check "a .sif without singularity gives a clear error" \
          "but singularity is not installed" "$out"
    check "the error says how to fix it" \
          "set APP_IMAGE to a docker:// image" "$out"
else
    printf '  (skipping the .sif guard: needs docker present and singularity absent)\n'
fi

printf '%s: %d passed, %d failed\n' "$(basename "$0")" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
