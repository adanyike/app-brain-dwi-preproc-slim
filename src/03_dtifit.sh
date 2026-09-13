#!/bin/bash
# Stage 3 -- B1 bias correction, final brain mask, tensor fit and derived maps.
#
# The tensor is fitted on a single diffusion-weighted shell together with the
# b=0 volumes, because dtifit's monoexponential model is not valid across a
# multi-shell acquisition.  Set dtifit_shell to "all" to fit every volume
# anyway.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
source "$WORK_DIR/state.sh"
timer_start

PROC="$WORK_DIR/proc"
DTI="$WORK_DIR/dti"
mkdir -p "$PROC" "$DTI"

BIAS_CORRECTED="$PROC/dwi_preprocessed.nii.gz"
BIAS_ALGO="$(cfg biascorrect ants)"

if [ "$BIAS_ALGO" = none ]; then
    log "bias correction disabled"
    cp "$EDDY_CORRECTED" "$BIAS_CORRECTED"
else
    log "B1 field bias correction (dwibiascorrect $BIAS_ALGO)"
    bias_correct "$EDDY_CORRECTED" "$BIAS_CORRECTED" \
                 "$ROTATED_BVECS" "$MERGED_BVALS" "$BRAIN_MASK" "$BIAS_ALGO"
fi

# ------------------------------------------- brain mask on corrected data ----
log "recomputing the brain mask on the corrected mean b=0"
dwiextract -force -nthreads "$NTHREADS" -bzero \
    -fslgrad "$ROTATED_BVECS" "$MERGED_BVALS" \
    "$BIAS_CORRECTED" "$PROC/b0.nii.gz"
fslmaths "$PROC/b0.nii.gz" -Tmean "$PROC/meanb0.nii.gz"

BET_F="$(cfg bet_final_f 0.3)"
bet "$PROC/meanb0.nii.gz" "$PROC/brain" -m -n -f "$BET_F"
# Fill interior holes left by bet, which otherwise punch through every ROI
# that overlaps them.
fslmaths "$PROC/brain_mask.nii.gz" -fillh "$PROC/brain_mask_filled.nii.gz"
mv "$PROC/brain_mask_filled.nii.gz" "$PROC/brain_mask.nii.gz"
FINAL_MASK="$PROC/brain_mask.nii.gz"

# The trade-off reverses here. This mask is published as neuro/mask and bounds
# dtifit and every ROI average, so an over-inclusive mask contaminates the
# numbers and sends downstream tractography into the skull: the union and the
# hole fill, never intensity growth, and a tighter cap than stage 1's.
MASK_QC_DIR="${MASK_QC_DIR:-$WORK_DIR/maskqc}"
check_brain_mask final "$PROC/meanb0.nii.gz" "$FINAL_MASK" "$BET_F" "$MASK_QC_DIR" \
    "$(cfg mask_repair_cap_final 0.10)" 0

# --------------------------------------------------------- shell selection ----
# dtifit's monoexponential model is not valid across a multi-shell acquisition,
# so by default one diffusion-weighted shell is fitted together with the b=0
# volumes.  Which shell that is comes out of the data: dtifit_shell may name a
# b-value, or ask for "lowest"/"highest", or "all" to fit everything.
FIT_DWI="$BIAS_CORRECTED"
FIT_BVECS="$ROTATED_BVECS"
FIT_BVALS="$MERGED_BVALS"

SHELL_REPORT="$WORK_DIR/prep/shells.json"
SHELL_ARG="$(python3 "$APP_DIR/python/shells.py" \
    --bvals "$MERGED_BVALS" \
    --select "$(cfg dtifit_shell 1500)" \
    --b0-threshold "$(cfg b0_threshold 50)" \
    --tolerance "$(cfg shell_tolerance 100)" \
    --out "$SHELL_REPORT")"
# The label is the b-value actually fitted, so provenance records b=1500 rather
# than the word "lowest".
DTIFIT_SHELL="$(jq -r '.resolved.label' "$SHELL_REPORT")"

if [ "$SHELL_ARG" != all ]; then
    log "extracting shells $SHELL_ARG for the tensor fit"
    EXTRACT="$WORK_DIR/extracted"
    mkdir -p "$EXTRACT"
    dwiextract -force -nthreads "$NTHREADS" \
        -fslgrad "$ROTATED_BVECS" "$MERGED_BVALS" \
        -shells "$SHELL_ARG" \
        -export_grad_fsl "$EXTRACT/shell.bvecs" "$EXTRACT/shell.bvals" \
        "$BIAS_CORRECTED" "$EXTRACT/shell.nii.gz"
    FIT_DWI="$EXTRACT/shell.nii.gz"
    FIT_BVECS="$EXTRACT/shell.bvecs"
    FIT_BVALS="$EXTRACT/shell.bvals"
else
    log "fitting the tensor to every volume"
fi

# ---------------------------------------------------------------- dtifit ----
DTI_BASE="$DTI/dti"
DTIFIT_ARGS=(--data="$FIT_DWI" --mask="$FINAL_MASK"
             --bvecs="$FIT_BVECS" --bvals="$FIT_BVALS"
             --out="$DTI_BASE" --save_tensor)
is_true "$(cfg_bool dtifit_wls false)" && DTIFIT_ARGS+=(--wls) || true
is_true "$(cfg_bool dtifit_sse false)" && DTIFIT_ARGS+=(--sse) || true

log "fitting the diffusion tensor (dtifit)"
dtifit "${DTIFIT_ARGS[@]}"

log "deriving RD, AD and the direction-encoded colour map"
# RD is the mean of the two minor eigenvalues; AD is the major one.
mrcalc -force -nthreads "$NTHREADS" \
    "${DTI_BASE}_L2.nii.gz" "${DTI_BASE}_L3.nii.gz" -add 2 -div "${DTI_BASE}_RD.nii.gz"
cp "${DTI_BASE}_L1.nii.gz" "${DTI_BASE}_AD.nii.gz"
fslmaths "${DTI_BASE}_FA.nii.gz" -mul "${DTI_BASE}_V1.nii.gz" "${DTI_BASE}_FA_color.nii.gz"

# Westin shape measures, part of brainlife's neuro/tensor datatype.
# cl = (L1-L2)/L1   linear     cp = (L2-L3)/L1   planar     cs = L3/L1  spherical
# L1 is zero everywhere outside the fit mask, so divide by a guarded copy of it
# (zeros swapped for ones) to keep the quotients finite; the numerators are zero
# there too, so every shape measure stays 0 outside the brain.
log "deriving the Westin linear/planar/spherical shape measures"
SAFE_L1="$DTI/l1_nonzero.nii.gz"
mrcalc -force -nthreads "$NTHREADS" "${DTI_BASE}_L1.nii.gz" 0 -gt \
    "${DTI_BASE}_L1.nii.gz" 1 -if "$SAFE_L1"
mrcalc -force -nthreads "$NTHREADS" "${DTI_BASE}_L1.nii.gz" "${DTI_BASE}_L2.nii.gz" -subtract \
    "$SAFE_L1" -divide "${DTI_BASE}_CL.nii.gz"
mrcalc -force -nthreads "$NTHREADS" "${DTI_BASE}_L2.nii.gz" "${DTI_BASE}_L3.nii.gz" -subtract \
    "$SAFE_L1" -divide "${DTI_BASE}_CP.nii.gz"
mrcalc -force -nthreads "$NTHREADS" "${DTI_BASE}_L3.nii.gz" \
    "$SAFE_L1" -divide "${DTI_BASE}_CS.nii.gz"

cat >> "$WORK_DIR/state.sh" <<EOSTATE
BIAS_CORRECTED="$BIAS_CORRECTED"
FINAL_MASK="$FINAL_MASK"
MEAN_B0="$PROC/meanb0.nii.gz"
DTI_BASE="$DTI_BASE"
FA_MAP="${DTI_BASE}_FA.nii.gz"
MD_MAP="${DTI_BASE}_MD.nii.gz"
AD_MAP="${DTI_BASE}_AD.nii.gz"
RD_MAP="${DTI_BASE}_RD.nii.gz"
FIT_BVECS="$FIT_BVECS"
FIT_BVALS="$FIT_BVALS"
DTIFIT_SHELL="$DTIFIT_SHELL"
SHELL_REPORT="$SHELL_REPORT"
EOSTATE

timer_report "stage 3 (tensor fit)"
