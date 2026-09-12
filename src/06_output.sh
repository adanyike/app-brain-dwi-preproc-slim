#!/bin/bash
# Stage 6 -- lay the results out as brainlife datatypes and write product.json.
#
#   output/dwi/            neuro/dwi     preprocessed DWI + rotated gradients
#   output/mask/           neuro/mask    final brain mask
#   output/tensor/         neuro/tensor  tensor + FA/MD/AD/RD/CL/CP/CS
#   output/roistats/       raw           per-ROI tables
#   output/reg/            raw           template-to-native transforms
#   output/qc/             raw           eddy_quad report and eddy logs
#   output/eddyqc/         raw           just qc.json, qc.pdf and the cohort
#                                        signature -- the lean dataset a group
#                                        SQUAD run consumes, N subjects at once

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
source "$WORK_DIR/state.sh"
timer_start

# Stages 4 and 5 are optional (atlas_registration=false) and QC can be turned
# off, so default every variable they would have set.
TOPUP_CONFIG="${TOPUP_CONFIG:-}"
ATLAS_NATIVE="${ATLAS_NATIVE:-}"
ATLAS_LABELS="${ATLAS_LABELS:-}"
REG_PREFIX="${REG_PREFIX:-}"
REG_AFFINE="${REG_AFFINE:-}"
REG_WARP="${REG_WARP:-}"
REG_WARPED_TEMPLATE="${REG_WARPED_TEMPLATE:-}"
ROI_STATS_DIR="${ROI_STATS_DIR:-}"
SHELL_REPORT="${SHELL_REPORT:-}"
EDDY_QC_DIR="${EDDY_QC_DIR:-}"
SLSPEC="${SLSPEC:-}"
SUBJECT="${SUBJECT:-}"
SESSION="${SESSION:-}"
RUN_ID="${RUN_ID:-}"
[ -n "$SUBJECT" ] || resolve_labels

mkdir -p "$OUT_DIR"/{dwi,mask,tensor,roistats,reg,qc,eddyqc}

# --------------------------------------------------------------- neuro/dwi ----
cp "$BIAS_CORRECTED"  "$OUT_DIR/dwi/dwi.nii.gz"
cp "$ROTATED_BVECS"   "$OUT_DIR/dwi/dwi.bvecs"
cp "$MERGED_BVALS"    "$OUT_DIR/dwi/dwi.bvals"

DWI_JSON="$(cfg_path dwi_json)"
if [ -n "$DWI_JSON" ]; then
    jq --argjson n "$NVOL_TOTAL" \
       '. + {ProcessingPipeline: "brainlife app-brain-dwi-preproc",
             NumberOfVolumes: $n}' "$DWI_JSON" > "$OUT_DIR/dwi/dwi.json"
else
    jq -n --argjson n "$NVOL_TOTAL" \
       '{ProcessingPipeline: "brainlife app-brain-dwi-preproc", NumberOfVolumes: $n}' \
       > "$OUT_DIR/dwi/dwi.json"
fi

# -------------------------------------------------------------- neuro/mask ----
cp "$FINAL_MASK" "$OUT_DIR/mask/mask.nii.gz"

# ------------------------------------------------------------ neuro/tensor ----
copy_map() {   # copy_map <dtifit suffix> <brainlife name>
    local src="${DTI_BASE}_$1.nii.gz"
    if [ -f "$src" ]; then cp "$src" "$OUT_DIR/tensor/$2.nii.gz"
    else warn "expected map $src was not produced"; fi
}
copy_map tensor tensor
copy_map FA fa
copy_map MD md
copy_map AD ad
copy_map RD rd
copy_map CL cl
copy_map CP cp
copy_map CS cs
copy_map FA_color fa_color
copy_map V1 v1
copy_map S0 s0

# --------------------------------------------------------------- ROI tables ----
if [ -n "$ROI_STATS_DIR" ] && [ -d "$ROI_STATS_DIR" ]; then
    cp "$ROI_STATS_DIR"/*.csv "$ROI_STATS_DIR"/roi_stats.json "$OUT_DIR/roistats/" 2>/dev/null || \
        warn "no ROI statistics were produced"
    [ -n "$ATLAS_LABELS" ] && cp "$ATLAS_LABELS" "$OUT_DIR/roistats/atlas_labels.json" || true
else
    log "atlas registration was skipped -- no ROI tables to publish"
    rmdir "$OUT_DIR/roistats" "$OUT_DIR/reg" 2>/dev/null || true
fi

# --------------------------------------------------------------- transforms ----
[ -n "$ATLAS_NATIVE" ] && [ -f "$ATLAS_NATIVE" ] && \
    cp "$ATLAS_NATIVE" "$OUT_DIR/reg/atlas_in_native.nii.gz" || true
[ -n "$REG_AFFINE" ] && [ -f "$REG_AFFINE" ]           && cp "$REG_AFFINE"           "$OUT_DIR/reg/template_to_native_affine.mat" || true
[ -n "$REG_WARP" ] && [ -f "$REG_WARP" ] && cp "$REG_WARP"             "$OUT_DIR/reg/template_to_native_warp.nii.gz" || true
[ -n "$REG_PREFIX" ] && [ -f "${REG_PREFIX}1InverseWarp.nii.gz" ] && \
    cp "${REG_PREFIX}1InverseWarp.nii.gz" "$OUT_DIR/reg/native_to_template_inversewarp.nii.gz" || true
[ -n "$REG_WARPED_TEMPLATE" ] && [ -f "$REG_WARPED_TEMPLATE" ] && cp "$REG_WARPED_TEMPLATE"  "$OUT_DIR/reg/template_fa_in_native.nii.gz" || true

# ----------------------------------------------------------------------- QC ----
[ -n "$EDDY_QC_DIR" ] && [ -d "$EDDY_QC_DIR" ] && cp -r "$EDDY_QC_DIR" "$OUT_DIR/qc/eddy_quad" || true
cp "$WORK_DIR/prep/prep.json"      "$OUT_DIR/qc/prep.json"      2>/dev/null || true
cp "$WORK_DIR/prep/shells.json"    "$OUT_DIR/qc/shells.json"    2>/dev/null || true
cp "$WORK_DIR/prep/acqparams.txt"  "$OUT_DIR/qc/acqparams.txt"  2>/dev/null || true
cp "$WORK_DIR/prep/index.txt"      "$OUT_DIR/qc/index.txt"      2>/dev/null || true
[ -n "$SLSPEC" ] && cp "$SLSPEC" "$OUT_DIR/qc/slspec.txt" || true
cp "$MEAN_B0"                      "$OUT_DIR/qc/meanb0.nii.gz"  2>/dev/null || true
for suffix in eddy_movement_rms eddy_restricted_movement_rms eddy_outlier_report \
              eddy_outlier_map eddy_parameters eddy_post_eddy_shell_alignment_parameters; do
    [ -f "${EDDY_OUT}.${suffix}" ] && cp "${EDDY_OUT}.${suffix}" "$OUT_DIR/qc/" || true
done

# ------------------------------------------------------- group-QC dataset ----
# eddy_squad reads nothing but qc.json from each subject, so the dataset a group
# run consumes carries only that, the single-subject report, and the signature
# saying which cohort this subject belongs to. Keeping it separate from the qc/
# bundle above matters: a group task stages every subject it was given, and the
# qc/ bundle carries a mean b=0 volume and the outlier maps -- hundreds of
# megabytes to read a 2 KB database, multiplied by the size of the study.
QC_JSON="${EDDY_QC_DIR:+$EDDY_QC_DIR/qc.json}"
if [ -n "$QC_JSON" ] && [ -f "$QC_JSON" ]; then
    cp "$QC_JSON" "$OUT_DIR/eddyqc/qc.json"
    [ -f "$EDDY_QC_DIR/qc.pdf" ] && cp "$EDDY_QC_DIR/qc.pdf" "$OUT_DIR/eddyqc/qc.pdf" || true
    python3 "$APP_DIR/python/eddyqc_summary.py" \
        --qc-json "$OUT_DIR/eddyqc/qc.json" \
        --subject "$SUBJECT" --session "$SESSION" --run-id "$RUN_ID" \
        --out "$OUT_DIR/eddyqc/squad_ready.json" \
        || warn "could not summarise the eddy QC database for group analysis"
    SQUAD_READY="$OUT_DIR/eddyqc/squad_ready.json"
else
    log "no eddy_quad database -- publishing no group-QC dataset"
    rmdir "$OUT_DIR/eddyqc" 2>/dev/null || true
    SQUAD_READY=""
fi

# ------------------------------------------------------------- product.json ----
PRODUCT_ARGS=(--prep "$WORK_DIR/prep/prep.json")
[ -n "$ROI_STATS_DIR" ] && PRODUCT_ARGS+=(--roi-stats "$ROI_STATS_DIR/roi_stats.json") || true
[ -n "$SHELL_REPORT" ] && [ -f "$SHELL_REPORT" ] && PRODUCT_ARGS+=(--shells "$SHELL_REPORT") || true
[ -n "$SQUAD_READY" ] && [ -f "$SQUAD_READY" ] && PRODUCT_ARGS+=(--eddy-qc "$SQUAD_READY") || true
python3 "$APP_DIR/python/make_product.py" \
    "${PRODUCT_ARGS[@]}" \
    --eddy-movement-rms "${EDDY_OUT}.eddy_movement_rms" \
    --eddy-binary "$EDDY_BIN" \
    --slice-to-volume "$EDDY_S2V" \
    --topup-applied "$TOPUP_APPLIED" \
    --topup-config "${TOPUP_CONFIG:-}" \
    --shell "$DTIFIT_SHELL" \
    --out "$PWD/product.json"

log "outputs written to $OUT_DIR"
find "$OUT_DIR" -maxdepth 2 -type f | sort | sed 's|^|  |' >&2

timer_report "stage 6 (output)"
