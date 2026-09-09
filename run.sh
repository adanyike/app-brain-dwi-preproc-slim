#!/bin/bash
# In-container driver.  Runs every stage of the pipeline in order against the
# config.json in the current working directory.
#
#   ./run.sh                 run everything
#   ./run.sh --from 2        resume from stage 2 (reuses work/state.sh)
#   ./run.sh --only 4 5      run just those stages
#
# Stages: 0 prepare | 1 topup | 2 eddy | 3 tensor fit | 4 atlas registration
#         5 ROI statistics | 6 outputs

set -euo pipefail

export APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
export WORK_DIR="${WORK_DIR:-$PWD/work}"
export OUT_DIR="${OUT_DIR:-$PWD/output}"
export CONFIG="${CONFIG:-$PWD/config.json}"
export TEMPLATE_DIR="${TEMPLATE_DIR:-$APP_DIR/templates}"

source "$APP_DIR/src/common.sh"

[ -f "$CONFIG" ] || die "no config.json in $PWD"

ALL_STAGES=(0 1 2 3 4 5 6)
# Indexed, not associative: `declare -A` is bash 4+, and macOS ships bash 3.2.
# The stage numbers are 0..6, so the index is the key.
STAGE_SCRIPT=(
    "00_prepare.sh" "01_topup.sh"     "02_eddy.sh"
    "03_dtifit.sh"  "04_atlas_reg.sh" "05_roi_stats.sh"
    "06_output.sh"
)

FROM=0
TO=6
ONLY=()
while [ $# -gt 0 ]; do
    case "$1" in
        --from) FROM="$2"; shift 2 ;;
        --to)   TO="$2";   shift 2 ;;
        --only) shift; while [ $# -gt 0 ] && [[ "$1" =~ ^[0-9]+$ ]]; do ONLY+=("$1"); shift; done ;;
        -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "unrecognised argument: $1" ;;
    esac
done

mkdir -p "$WORK_DIR" "$OUT_DIR"
setup_threads

check_tools jq python3 fslval fslroi fslmerge fslmaths bet topup dtifit \
            dwidenoise mrdegibbs dwiextract mrcalc dwibiascorrect \
            antsRegistrationSyN.sh antsApplyTransforms
python3 -c 'import numpy, nibabel' 2>/dev/null || \
    die "python3 needs numpy and nibabel; run the app through its container"

# Skipping the atlas stages is legitimate when only preprocessing is wanted.
RUN_ATLAS="$(cfg_bool atlas_registration true)"

SELECTED=()
if [ ${#ONLY[@]} -gt 0 ]; then
    SELECTED=("${ONLY[@]}")
else
    for stage in "${ALL_STAGES[@]}"; do
        [ "$stage" -ge "$FROM" ] && [ "$stage" -le "$TO" ] && SELECTED+=("$stage") || true
    done
fi

log "pipeline start -- stages: ${SELECTED[*]}"
PIPELINE_T0="$(date +%s)"

for stage in "${SELECTED[@]}"; do
    script="${STAGE_SCRIPT[$stage]:-}"
    [ -n "$script" ] || die "no such stage: $stage"
    if [ "$RUN_ATLAS" != true ] && { [ "$stage" = 4 ] || [ "$stage" = 5 ]; }; then
        log "stage $stage skipped (atlas_registration is false)"
        continue
    fi
    log "===== stage $stage: $script ====="
    bash "$APP_DIR/src/$script"
done

TOTAL=$(( $(date +%s) - PIPELINE_T0 ))
log "pipeline finished in $((TOTAL / 3600))h $(((TOTAL % 3600) / 60))m $((TOTAL % 60))s"
