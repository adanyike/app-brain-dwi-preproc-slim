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

# ---------------------------------------------------------- which App is it ----
# One repository, two brainlife Apps: the per-subject pipeline and the group
# eddy_squad run.  The driver is chosen from the inputs in config.json.
run_main_in() {  # run_main_in <dir> <env assignments...>
    local dir="$1"; shift
    ( cd "$dir" && env "$@" bash "$APP/main" 2>&1 )
}

mkdir -p "$TMP/subject" "$TMP/group"
printf '{"dwi": "dwi.nii.gz"}\n' > "$TMP/subject/config.json"
printf '{"eddyqc": ["a", "b"]}\n' > "$TMP/group/config.json"

out="$(run_main_in "$TMP/subject" SKIP_CONTAINER=1)"
check "a per-subject config runs the pipeline" "driver: run.sh" "$out"

out="$(run_main_in "$TMP/group" SKIP_CONTAINER=1)"
check "a group config runs the SQUAD driver" "driver: run_squad.sh" "$out"

# An explicit mode wins over the inputs, so a group task whose input key was
# renamed on brainlife can still be routed.
printf '{"mode": "group", "qc_folders": ["a", "b"]}\n' > "$TMP/group/config.json"
out="$(run_main_in "$TMP/group" SKIP_CONTAINER=1)"
check "an explicit group mode is honoured" "driver: run_squad.sh" "$out"

printf '{"mode": "nonsense", "dwi": "dwi.nii.gz"}\n' > "$TMP/subject/config.json"
out="$(run_main_in "$TMP/subject" SKIP_CONTAINER=1)"
check "an unrecognised mode is refused" "unrecognised mode" "$out"

# ------------------------------------------------------- where results go ----
# brainlife collects each output from <task dir>/<output ID>/, so a brainlife
# task (recognised by the keys brainlife writes) writes there; a local run keeps
# ./output.
mkdir -p "$TMP/bl/tensor" "$TMP/bl/squad"
touch "$TMP/bl/tensor/stale.nii.gz" "$TMP/bl/squad/keep"
printf '{"_app": "x", "_inputs": [], "dwi": "../stage/ds/dwi.nii.gz"}\n' > "$TMP/bl/config.json"
out="$(run_main_in "$TMP/bl" SKIP_CONTAINER=1)"
check "a brainlife task writes to the task directory" "outputs: $TMP/bl"$'\n' "$out"$'\n'
check "the previous attempt's outputs are cleared" "cleared" \
      "$([ ! -e "$TMP/bl/tensor" ] && echo cleared)"
check "the other App's output directory is left alone" "kept" \
      "$([ -e "$TMP/bl/squad/keep" ] && echo kept)"
check "no ./output on brainlife" "absent" "$([ ! -e "$TMP/bl/output" ] && echo absent)"

printf '{"dwi": "dwi.nii.gz"}\n' > "$TMP/subject/config.json"
out="$(run_main_in "$TMP/subject" SKIP_CONTAINER=1)"
check "a local run writes to ./output" "outputs: $TMP/subject/output" "$out"

out="$(run_main_in "$TMP/subject" SKIP_CONTAINER=1 "OUT_DIR=$TMP/elsewhere")"
check "OUT_DIR in the environment wins" "outputs: $TMP/elsewhere" "$out"

# A directory the run would clear is refused when config.json reads from it,
# so input data in ./dwi survives a config that looks like brainlife's.
mkdir -p "$TMP/guard/dwi"
touch "$TMP/guard/dwi/dwi.nii.gz"
printf '{"_inputs": [], "dwi": "dwi/dwi.nii.gz"}\n' > "$TMP/guard/config.json"
out="$(run_main_in "$TMP/guard" SKIP_CONTAINER=1)"
check "clearing an input directory is refused" "reads an input from it" "$out"
check "and the input survives" "kept" "$([ -f "$TMP/guard/dwi/dwi.nii.gz" ] && echo kept)"

mkdir -p "$TMP/blgroup/squad" "$TMP/blgroup/tensor"
printf '{"_tid": 2, "eddyqc": ["a", "b"]}\n' > "$TMP/blgroup/config.json"
out="$(run_main_in "$TMP/blgroup" SKIP_CONTAINER=1)"
check "a brainlife group task writes to the task directory" "outputs: $TMP/blgroup"$'\n' "$out"$'\n'
check "and clears only squad/" "squad-cleared tensor-kept" \
      "$([ ! -e "$TMP/blgroup/squad" ] && printf squad-cleared) $([ -d "$TMP/blgroup/tensor" ] && printf tensor-kept)"

# The routing must not depend on jq: the container carries it, a cluster head
# node need not.  Run main with a PATH that has everything it uses but jq.
NOJQ="$TMP/bin-nojq"
mkdir -p "$NOJQ"
MISSING=""
for tool in bash grep rm mkdir sed cat nproc id env dirname; do
    path="$(command -v "$tool" 2>/dev/null)" && ln -sf "$path" "$NOJQ/$tool" || MISSING="$MISSING $tool"
done
if [ -z "$MISSING" ]; then
    printf '{"eddyqc": ["a", "b"]}\n' > "$TMP/group/config.json"
    out="$(run_main_in "$TMP/group" SKIP_CONTAINER=1 "PATH=$NOJQ")"
    check "the group driver is found without jq" "driver: run_squad.sh" "$out"
    printf '{"dwi": "dwi.nii.gz"}\n' > "$TMP/subject/config.json"
    out="$(run_main_in "$TMP/subject" SKIP_CONTAINER=1 "PATH=$NOJQ")"
    check "and so is the per-subject driver" "driver: run.sh" "$out"
else
    printf '  (skipping the no-jq dispatch check: missing%s)\n' "$MISSING"
fi

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
