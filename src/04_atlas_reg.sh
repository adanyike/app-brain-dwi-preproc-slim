#!/bin/bash
# Stage 4 -- bring the JHU ICBM-DTI-81 white-matter atlas into each subject's
# native diffusion space.
#
# The JHU FA template is the *moving* image and the subject's own FA map is
# *fixed*, so the atlas labels only ever have to be pushed through one composite
# transform and the subject's data is never resampled.
#
# The template and the label image are FSL's own, read from
# $FSLDIR/data/atlases/JHU; template_fa and atlas override them.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
source "$WORK_DIR/state.sh"
timer_start

REG="$WORK_DIR/reg"
mkdir -p "$REG"

TEMPLATE_FA="$(cfg_path template_fa)"
[ -n "$TEMPLATE_FA" ] || TEMPLATE_FA="$JHU_DIR/JHU-ICBM-FA-1mm.nii.gz"
ATLAS="$(cfg_path atlas)"
[ -n "$ATLAS" ] || ATLAS="$JHU_DIR/JHU-ICBM-labels-1mm.nii.gz"
ATLAS_LABELS="$(cfg_path atlas_labels)"
[ -n "$ATLAS_LABELS" ] || ATLAS_LABELS="$TEMPLATE_DIR/JHU-ICBM-labels.json"

ATLAS_HINT="it ships with FSL under \$FSLDIR/data/atlases/JHU; set FSLDIR, or name the file with the 'template_fa' / 'atlas' config key"
[ -f "$TEMPLATE_FA" ] || die "template FA not found at $TEMPLATE_FA -- $ATLAS_HINT"
[ -f "$ATLAS" ]       || die "atlas not found at $ATLAS -- $ATLAS_HINT"
[ -f "$ATLAS_LABELS" ] || die "atlas label metadata not found at $ATLAS_LABELS"

PREFIX="$REG/template_to_native"
TRANSFORM_TYPE="$(cfg ants_transform s)"

log "registering the JHU FA template to the subject's FA (antsRegistrationSyN.sh -t $TRANSFORM_TYPE)"
antsRegistrationSyN.sh -d 3 \
    -f "$FA_MAP" \
    -m "$TEMPLATE_FA" \
    -o "$PREFIX" \
    -t "$TRANSFORM_TYPE" \
    -n "$NTHREADS"

AFFINE="${PREFIX}0GenericAffine.mat"
WARP="${PREFIX}1Warp.nii.gz"
[ -f "$AFFINE" ] || die "antsRegistrationSyN.sh produced no affine at $AFFINE"

ATLAS_NATIVE="$REG/atlas_in_native.nii.gz"
# MultiLabel is the interpolator ANTs recommends for label images.  Linear
# interpolation of a label image blurs small ROIs and has to be followed by
# rounding to recover integers; set atlas_interpolation to "Linear" if that is
# what a comparison needs.
INTERP="$(cfg atlas_interpolation MultiLabel)"

APPLY_ARGS=(-d 3 -i "$ATLAS" -r "$FA_MAP" -o "$ATLAS_NATIVE" -n "$INTERP")
[ -f "$WARP" ] && APPLY_ARGS+=(-t "$WARP") || true
APPLY_ARGS+=(-t "$AFFINE")

log "warping the atlas into native space (interpolation: $INTERP)"
antsApplyTransforms "${APPLY_ARGS[@]}"

if [ "$INTERP" = Linear ] || [ "$INTERP" = BSpline ]; then
    log "rounding interpolated label values back to integers"
    mrcalc -force -nthreads "$NTHREADS" "$ATLAS_NATIVE" -round "$REG/atlas_rounded.nii.gz"
    mv "$REG/atlas_rounded.nii.gz" "$ATLAS_NATIVE"
fi

# Optional per-ROI binary masks, one Roi_<n>.nii.gz per label.  Off by default:
# the statistics are computed straight from the label image, so 50 extra NIfTIs
# are usually just clutter.
if is_true "$(cfg_bool write_roi_masks false)"; then
    ROI_DIR="$REG/roi"
    mkdir -p "$ROI_DIR"
    N_LABELS="$(jq -r '.n_labels' "$ATLAS_LABELS" 2>/dev/null || echo 50)"
    log "writing $N_LABELS binary ROI masks"
    for k in $(seq 1 "$N_LABELS"); do
        fslmaths "$ATLAS_NATIVE" -thr "$k" -uthr "$k" -bin "$ROI_DIR/Roi_${k}.nii.gz"
    done
fi

cat >> "$WORK_DIR/state.sh" <<EOSTATE
ATLAS_NATIVE="$ATLAS_NATIVE"
ATLAS_LABELS="$ATLAS_LABELS"
REG_PREFIX="$PREFIX"
REG_AFFINE="$AFFINE"
REG_WARP="$WARP"
REG_WARPED_TEMPLATE="${PREFIX}Warped.nii.gz"
EOSTATE

timer_report "stage 4 (atlas registration)"
