#!/bin/bash
# Stage 0 -- assemble the merged series and every bookkeeping file eddy needs.
#
#   * concatenate the forward and reverse phase-encoded series
#   * drop one slice when the slice count is odd (topup needs an even count at
#     the coarsest subsampling level)
#   * derive acqparams / index / merged gradient table from the sidecars
#   * derive the slspec (or the multiband factor) for slice-to-volume correction
#   * denoise (MP-PCA) and remove Gibbs ringing

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
timer_start

RAW="$WORK_DIR/raw"
PREP="$WORK_DIR/prep"
mkdir -p "$RAW" "$PREP"

DWI="$(require_path dwi)"
BVALS="$(require_path bvals)"
BVECS="$(require_path bvecs)"
DWI_JSON="$(cfg_path dwi_json)"

RDWI="$(cfg_path rdwi)"
RBVALS="$(cfg_path rbvals)"
RBVECS="$(cfg_path rbvecs)"
RDWI_JSON="$(cfg_path rdwi_json)"

log "forward series: $DWI"
NVOL_FWD="$(nvols "$DWI")"
NSLICE="$(nslices "$DWI")"
log "  $NVOL_FWD volumes, $NSLICE slices"

MERGED="$RAW/dwi_merged.nii.gz"
NVOL_REV=0

# The reverse series is not optional.  Without the opposing pair there is no
# field to estimate, and the output would be geometrically distorted while being
# named, shaped and summarised exactly like a corrected run -- indistinguishable
# downstream, and easy to pool across a cohort by accident.
[ -n "$RDWI" ] || die "no reverse phase-encoded series supplied. This app corrects susceptibility distortion from an opposing phase-encode pair and cannot run without one: map 'rdwi', 'rbvals' and 'rbvecs' (and ideally 'rdwi_json')."
[ -n "$RBVALS" ] && [ -n "$RBVECS" ] || \
    die "'rdwi' was given but 'rbvals'/'rbvecs' were not; all three are needed"

NVOL_REV="$(nvols "$RDWI")"
log "reverse series: $RDWI ($NVOL_REV volumes)"

for dim in dim1 dim2 dim3; do
    a="$(fslval "$DWI" $dim | tr -d '[:space:]')"
    b="$(fslval "$RDWI" $dim | tr -d '[:space:]')"
    [ "$a" = "$b" ] || die "forward and reverse series disagree on $dim ($a vs $b); they must share a voxel grid"
done

fslmerge -t "$MERGED" "$DWI" "$RDWI"

NVOL_TOTAL=$(( NVOL_FWD + NVOL_REV ))
log "merged series: $NVOL_TOTAL volumes"

# --------------------------------------------------- odd slice handling ----
# topup's default subsampling schedule halves the matrix, so an odd slice count
# makes it fail.  The original pipeline cropped one slice off and told eddy
# about the resulting slice-order shift via --mb/--mb_offs.
DROP_ODD_SLICE="$(cfg_bool remove_odd_slice true)"
REMOVE_BOTTOM="$(cfg_bool remove_bottom_slice true)"
SLICE_DROPPED=none

if [ $(( NSLICE % 2 )) -ne 0 ] && is_true "$DROP_ODD_SLICE"; then
    CROPPED="$RAW/dwi_merged_cropped.nii.gz"
    if is_true "$REMOVE_BOTTOM"; then
        log "odd slice count ($NSLICE) -- dropping the bottom slice"
        fslroi "$MERGED" "$CROPPED" 0 -1 0 -1 1 -1 0 -1
        SLICE_DROPPED=bottom
    else
        log "odd slice count ($NSLICE) -- dropping the top slice"
        fslroi "$MERGED" "$CROPPED" 0 -1 0 -1 0 $(( NSLICE - 1 )) 0 -1
        SLICE_DROPPED=top
    fi
    mv "$CROPPED" "$MERGED"
elif [ $(( NSLICE % 2 )) -ne 0 ]; then
    warn "slice count is odd ($NSLICE) and remove_odd_slice is false -- topup may fail unless topup_config handles it"
fi

# ------------------------------------- acqparams / index / gradient table ----
PREP_ARGS=(
    --bvals "$BVALS" --bvecs "$BVECS" --nvols "$NVOL_FWD"
    --pe-dir "$(cfg_manual pe_dir)"
    --b0-threshold "$(cfg b0_threshold 50)"
    --b0-per-pedir "$(cfg b0_per_pedir 2)"
    --reduce-b0-above "$(cfg reduce_b0_above 8)"
    --outdir "$PREP"
)
[ -n "$DWI_JSON" ] && PREP_ARGS+=(--json "$DWI_JSON") || true
READOUT="$(cfg_manual readout_time)"
[ -n "$READOUT" ] && PREP_ARGS+=(--readout-time "$READOUT") || true

if [ -n "$RDWI" ]; then
    PREP_ARGS+=(--rbvals "$RBVALS" --rbvecs "$RBVECS" --rnvols "$NVOL_REV"
                --rpe-dir "$(cfg_manual rpe_dir)")
    [ -n "$RDWI_JSON" ] && PREP_ARGS+=(--rjson "$RDWI_JSON") || true
    RREADOUT="$(cfg_manual rreadout_time)"
    [ -n "$RREADOUT" ] && PREP_ARGS+=(--rreadout-time "$RREADOUT") || true
fi

python3 "$APP_DIR/python/prepare_inputs.py" "${PREP_ARGS[@]}"

# Explicit acqparams/index inputs win over the derived ones -- an escape hatch
# for datasets whose sidecars are incomplete.
USER_ACQP="$(cfg_path acqp)"
USER_INDEX="$(cfg_path index)"
if [ -n "$USER_ACQP" ]; then
    log "using the supplied acqparams file instead of the derived one"
    cp "$USER_ACQP" "$PREP/acqparams.txt"
fi
if [ -n "$USER_INDEX" ]; then
    log "using the supplied index file instead of the derived one"
    cp "$USER_INDEX" "$PREP/index.txt"
fi

# ----------------------------------------------------------- slspec / mb ----
USER_SLSPEC="$(cfg_path slspec)"
SLSPEC=""
MB_FACTOR=""

if [ -n "$USER_SLSPEC" ]; then
    log "using the supplied slspec file"
    cp "$USER_SLSPEC" "$PREP/slspec.txt"
    SLSPEC="$PREP/slspec.txt"
    MB_FACTOR="$(awk 'NF{print NF; exit}' "$SLSPEC")"
elif [ -n "$DWI_JSON" ]; then
    if python3 "$APP_DIR/python/make_slspec.py" --json "$DWI_JSON" \
            --out "$PREP/slspec.txt" --mb-out "$PREP/mb.txt" --n-slices "$NSLICE"; then
        SLSPEC="$PREP/slspec.txt"
        MB_FACTOR="$(cat "$PREP/mb.txt")"
    else
        warn "could not derive a slspec from $DWI_JSON -- slice-to-volume correction will be disabled"
    fi
else
    warn "no dwi_json supplied -- slice-to-volume correction will be disabled"
fi

# A cropped volume no longer matches the slspec rows (one row would be short),
# so fall back to eddy's --mb/--mb_offs description of the same slice order.
if [ "$SLICE_DROPPED" != none ] && [ -n "$MB_FACTOR" ]; then
    log "a slice was cropped -- describing the slice order with --mb $MB_FACTOR instead of --slspec"
    SLSPEC=""
fi

# --------------------------------------------------- denoise and degibbs ----
CURRENT="$MERGED"

if is_true "$(cfg_bool denoise true)"; then
    log "MP-PCA denoising (dwidenoise)"
    dwidenoise -force -nthreads "$NTHREADS" \
        -noise "$PREP/noise.nii.gz" "$CURRENT" "$PREP/dwi_denoised.nii.gz"
    CURRENT="$PREP/dwi_denoised.nii.gz"
else
    log "denoising disabled"
fi

if is_true "$(cfg_bool degibbs true)"; then
    log "Gibbs ringing removal (mrdegibbs)"
    mrdegibbs -force -nthreads "$NTHREADS" "$CURRENT" "$PREP/dwi_denoised_unringed.nii.gz"
    CURRENT="$PREP/dwi_denoised_unringed.nii.gz"
else
    log "Gibbs ringing removal disabled"
fi

cp "$CURRENT" "$PREP/dwi_prepared.nii.gz"

# ------------------------------------------------------------ stage state ----
cat > "$WORK_DIR/state.sh" <<EOSTATE
DWI_PREPARED="$PREP/dwi_prepared.nii.gz"
ACQPARAMS="$PREP/acqparams.txt"
INDEX_FILE="$PREP/index.txt"
MERGED_BVALS="$PREP/merged.bvals"
MERGED_BVECS="$PREP/merged.bvecs"
SLSPEC="$SLSPEC"
MB_FACTOR="$MB_FACTOR"
SLICE_DROPPED="$SLICE_DROPPED"
NVOL_TOTAL=$NVOL_TOTAL
NVOL_FWD=$NVOL_FWD
NVOL_REV=$NVOL_REV
NSLICE_ORIGINAL=$NSLICE
HAS_REVERSE_PE=$([ -n "$RDWI" ] && echo true || echo false)
EOSTATE

log "stage 0 state written to $WORK_DIR/state.sh"
timer_report "stage 0 (prepare)"
