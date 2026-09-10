#!/bin/bash
# Stage 2 -- eddy current, motion and (where possible) slice-to-volume
# correction, applying the topup field at the same time.
#
# Slice-to-volume correction (--mporder) is CUDA-only.  Without a CUDA build of
# eddy the stage degrades to volume-to-volume correction and says so loudly
# rather than failing.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
source "$WORK_DIR/state.sh"
timer_start

EDDY_DIR="$WORK_DIR/eddy"
mkdir -p "$EDDY_DIR"
EDDY_OUT="$EDDY_DIR/eddy_corrected"

EDDY_BIN="$(find_eddy)"
log "eddy binary: $EDDY_BIN"

USE_S2V=false
if is_cuda_eddy "$EDDY_BIN"; then
    if gpu_present; then
        USE_S2V=true
    elif is_true "$(cfg_bool require_gpu false)"; then
        die "a CUDA eddy binary was selected but no GPU is visible. Pass --nv to singularity / --gpus to docker, or set require_gpu=false to fall back to CPU eddy without slice-to-volume correction."
    elif [ -n "$(cfg eddy_binary "${EDDY_BINARY:-}")" ]; then
        # The user named this binary explicitly, so honour it rather than
        # quietly substituting a different one -- but say what is coming.
        warn "eddy_binary names a CUDA build but no GPU is visible; running it anyway as requested, which will probably fail"
        USE_S2V=true
    elif CPU_EDDY="$(find_cpu_eddy)"; then
        warn "no GPU is visible -- falling back to $CPU_EDDY. Slice-to-volume correction will be SKIPPED and this stage will be considerably slower. Set require_gpu=true to fail here instead."
        EDDY_BIN="$CPU_EDDY"
    else
        die "no GPU is visible and this image contains no CPU eddy build to fall back to. Pass --nv to singularity / --gpus to docker, or use an image providing eddy_openmp or eddy_cpu."
    fi
else
    warn "no CUDA eddy build available -- slice-to-volume correction is disabled and this stage will be considerably slower"
fi
log "eddy binary in use: $EDDY_BIN (slice-to-volume: $USE_S2V)"

EDDY_ARGS=(
    --imain="$DWI_PREPARED"
    --mask="$BRAIN_MASK"
    --index="$INDEX_FILE"
    --acqp="$ACQPARAMS"
    --bvecs="$MERGED_BVECS"
    --bvals="$MERGED_BVALS"
    --out="$EDDY_OUT"
)

[ "$TOPUP_APPLIED" = true ] && EDDY_ARGS+=(--topup="$TOPUP_BASE") || true

EDDY_NITER="$(cfg eddy_niter 6)"
EDDY_ARGS+=(--niter="$EDDY_NITER")

if is_true "$(cfg_bool eddy_repol true)"; then
    EDDY_ARGS+=(--repol --ol_type="$(cfg eddy_ol_type both)")
fi

EDDY_FWHM="$(cfg eddy_fwhm '10,6,0,0,0,0')"
if [ -n "$EDDY_FWHM" ]; then
    fwhm_n="$(awk -F, '{print NF}' <<< "$EDDY_FWHM")"
    [ "$fwhm_n" = "$EDDY_NITER" ] || \
        die "eddy_fwhm has $fwhm_n entries but eddy_niter is $EDDY_NITER; they must match"
    EDDY_ARGS+=(--fwhm="$EDDY_FWHM")
fi

if is_true "$USE_S2V"; then
    MPORDER="$(cfg eddy_mporder 6)"
    if [ -n "$SLSPEC" ]; then
        log "slice-to-volume correction with an explicit slspec"
        EDDY_ARGS+=(--mporder="$MPORDER"
                    --s2v_niter="$(cfg eddy_s2v_niter 6)"
                    --slspec="$SLSPEC")
    elif [ -n "$MB_FACTOR" ]; then
        # A slice was cropped, so the slspec rows no longer line up; describe the
        # same interleave with --mb and the offset of the dropped slice.
        if [ "$SLICE_DROPPED" = bottom ]; then MB_OFFS=-1; else MB_OFFS=1; fi
        log "slice-to-volume correction with --mb $MB_FACTOR --mb_offs $MB_OFFS"
        EDDY_ARGS+=(--mporder="$MPORDER"
                    --s2v_niter="$(cfg eddy_s2v_niter 6)"
                    --mb="$MB_FACTOR" --mb_offs="$MB_OFFS")
    else
        warn "no slice timing information -- running volume-to-volume correction only"
        USE_S2V=false
    fi
fi

is_true "$(cfg_bool eddy_data_is_shelled true)" && EDDY_ARGS+=(--data_is_shelled) || true
is_true "$(cfg_bool eddy_cnr_maps true)"        && EDDY_ARGS+=(--cnr_maps) || true
is_true "$(cfg_bool eddy_residuals false)"      && EDDY_ARGS+=(--residuals) || true

SLM="$(cfg eddy_slm none)"
[ "$SLM" != none ] && [ -n "$SLM" ] && EDDY_ARGS+=(--slm="$SLM") || true

EDDY_EXTRA="$(cfg eddy_extra '')"

log "running: $EDDY_BIN ${EDDY_ARGS[*]} $EDDY_EXTRA"
# EDDY_EXTRA is a deliberate argument list, so word splitting is wanted here.
# shellcheck disable=SC2086
"$EDDY_BIN" "${EDDY_ARGS[@]}" $EDDY_EXTRA --verbose

[ -f "${EDDY_OUT}.nii.gz" ] || die "eddy produced no output at ${EDDY_OUT}.nii.gz"

ROTATED_BVECS="${EDDY_OUT}.eddy_rotated_bvecs"
[ -f "$ROTATED_BVECS" ] || die "eddy produced no rotated bvecs"

# ------------------------------------------------------------------- QC ----
QC_DIR="$WORK_DIR/qc/eddy_quad"
if is_true "$(cfg_bool eddy_qc true)" && have eddy_quad; then
    log "running eddy_quad"
    QUAD_ARGS=("$EDDY_OUT"
               --eddyIdx "$INDEX_FILE"
               --eddyParams "$ACQPARAMS"
               --mask "$BRAIN_MASK"
               --bvals "$MERGED_BVALS"
               --bvecs "$MERGED_BVECS"
               --output-dir "$QC_DIR")
    [ "$TOPUP_APPLIED" = true ] && [ -f "${TOPUP_BASE}_fout.nii.gz" ] && \
        QUAD_ARGS+=(--field "${TOPUP_BASE}_fout.nii.gz") || true
    is_true "$USE_S2V" && [ -n "$SLSPEC" ] && QUAD_ARGS+=(--slspec "$SLSPEC") || true
    rm -rf "$QC_DIR"
    eddy_quad "${QUAD_ARGS[@]}" || warn "eddy_quad failed; continuing without its report"
else
    log "eddy_quad skipped"
fi

cat >> "$WORK_DIR/state.sh" <<EOSTATE
EDDY_OUT="$EDDY_OUT"
EDDY_CORRECTED="${EDDY_OUT}.nii.gz"
ROTATED_BVECS="$ROTATED_BVECS"
EDDY_BIN="$EDDY_BIN"
EDDY_S2V=$USE_S2V
EDDY_QC_DIR="$QC_DIR"
EOSTATE

timer_report "stage 2 (eddy)"
