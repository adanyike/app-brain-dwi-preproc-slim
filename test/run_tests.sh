#!/bin/bash
# Test suite for the app.
#
#   bash test/run_tests.sh              unit tests, static checks and a dry run
#                                       of the whole pipeline against the stub
#                                       toolchain -- no FSL/MRtrix3/ANTs needed
#   bash test/run_tests.sh --no-dryrun  unit tests and static checks only
#   bash test/run_tests.sh --smoke      also run the pipeline for real on
#                                       synthetic data (needs the container)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$(cd "$HERE/.." && pwd)"
cd "$APP" || exit 1

FAILURES=0
run() {  # run <label> <command...>
    printf '\n=== %s ===\n' "$1"
    shift
    if "$@"; then
        printf '  ok\n'
    else
        printf '  FAILED\n'
        FAILURES=$((FAILURES + 1))
    fi
}

# ------------------------------------------------------------ static checks ----
run "bash syntax" bash -c 'for f in main run.sh src/*.sh test/*.sh; do bash -n "$f" || exit 1; done'
run "python syntax" bash -c 'for f in python/*.py test/*.py; do python3 -m py_compile "$f" || exit 1; done'
run "config.json.example is valid JSON" jq empty config.json.example
run "package.json is valid JSON" jq empty package.json
run "atlas label metadata is valid JSON" jq empty templates/JHU-ICBM-labels.json
run "entrypoints are executable" bash -c '[ -x main ] && [ -x run.sh ]'
run "templates are present" bash -c '
    for f in JHU-ICBM-FA-1mm.nii.gz JHU-ICBM-labels.json philips_84_slices_slspec.txt; do
        [ -s "templates/$f" ] || { echo "missing templates/$f"; exit 1; }
    done'

if command -v shellcheck >/dev/null 2>&1; then
    SHELLCHECK_TARGETS=(main run.sh src/*.sh)
    # The slim variant adds container build helpers.
    [ -d docker ] && SHELLCHECK_TARGETS+=(docker/*.sh)
    run "shellcheck" shellcheck -S warning -x "${SHELLCHECK_TARGETS[@]}"
else
    printf '\n=== shellcheck ===\n  skipped (not installed)\n'
fi

# -------------------------------------------------------------- unit tests ----
run "bash 3.2 portability" bash test/test_portability.sh
run "config helpers"  bash test/test_config.sh
run "main dispatch"   bash test/test_main.sh
run "Dockerfile lint" bash test/test_dockerfile.sh
# Only the slim variant carries this; it asserts the two apps have not drifted.
if [ -f test/test_parity.sh ]; then
    run "parity with the full app" bash test/test_parity.sh
fi
run "sidecar"         python3 test/test_sidecar.py -q
run "make_slspec"     python3 test/test_make_slspec.py -q
run "prepare_inputs"  python3 test/test_prepare_inputs.py -q
run "shells"          python3 test/test_shells.py -q
run "labels"          python3 test/test_labels.py -q
run "make_product"    python3 test/test_make_product.py -q
if python3 -c 'import numpy, nibabel' 2>/dev/null; then
    run "roi_stats"   python3 test/test_roi_stats.py -q
else
    printf '\n=== roi_stats ===\n  skipped (numpy/nibabel not installed)\n'
fi

# ------------------------------------------ dry run against stub binaries ----
# Exercises every stage and every branch without FSL/MRtrix3/ANTs installed.
# Skip with --no-dryrun when iterating on the unit tests alone.
if [ "${1:-}" != "--no-dryrun" ] && python3 -c 'import numpy, nibabel' 2>/dev/null; then
    run "pipeline dry run (stub toolchain)" bash test/dryrun.sh
else
    printf '\n=== pipeline dry run ===\n  skipped\n'
fi

# ------------------------------------------------------------ smoke test ----
if [ "${1:-}" = "--smoke" ]; then
    SMOKE="$(mktemp -d)"
    printf '\n=== end-to-end smoke test in %s ===\n' "$SMOKE"
    python3 test/make_test_data.py --outdir "$SMOKE/input"
    jq -n --arg d "$SMOKE/input" '{
        dwi: ($d + "/dwi/dwi.nii.gz"),   bvals: ($d + "/dwi/dwi.bvals"),
        bvecs: ($d + "/dwi/dwi.bvecs"),  dwi_json: ($d + "/dwi/dwi.json"),
        rdwi: ($d + "/rdwi/dwi.nii.gz"), rbvals: ($d + "/rdwi/dwi.bvals"),
        rbvecs: ($d + "/rdwi/dwi.bvecs"),rdwi_json: ($d + "/rdwi/dwi.json"),
        subject: "sub-synthetic", dtifit_shell: "1500",
        eddy_niter: 2, eddy_fwhm: "10,0", eddy_s2v_niter: 2,
        eddy_qc: false, nthreads: 2
    }' > "$SMOKE/config.json"
    ( cd "$SMOKE" && APP_DIR="$APP" "$APP/run.sh" )
    status=$?
    if [ $status -eq 0 ] && [ -s "$SMOKE/output/tensor/fa.nii.gz" ] \
       && [ -s "$SMOKE/output/roistats/roi_stats.csv" ]; then
        printf '  ok -- outputs under %s/output\n' "$SMOKE"
    else
        printf '  FAILED (exit %d)\n' "$status"
        FAILURES=$((FAILURES + 1))
    fi
fi

printf '\n----------------------------------------\n'
if [ "$FAILURES" -eq 0 ]; then
    printf 'all checks passed\n'
else
    printf '%d check(s) failed\n' "$FAILURES"
fi
exit "$FAILURES"
