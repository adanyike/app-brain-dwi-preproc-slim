#!/bin/bash
# Run the whole pipeline against the stub toolchain in test/stubs, across the
# branches that are easy to get wrong, and assert on what each one produced.
#
# This proves the plumbing -- argument construction, file hand-off between
# stages, the state file, the output layout.  It proves nothing about the
# science; only a run against real FSL/MRtrix3/ANTs does that.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$(cd "$HERE/.." && pwd)"

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
BIN="$ROOT/bin"
bash "$HERE/stubs/install.sh" "$BIN" >/dev/null
WITH_FAKE_GPU=1 bash "$HERE/stubs/install.sh" "$ROOT/bin-gpu" >/dev/null
cp "$BIN"/* "$ROOT/bin-gpu/" 2>/dev/null
WITH_FAKE_GPU=1 bash "$HERE/stubs/install.sh" "$ROOT/bin-gpu" >/dev/null

PASS=0
FAIL=0
note() { printf '    %s\n' "$*"; }
check() {  # check <description> <condition-command...>
    if "${@:2}"; then PASS=$((PASS + 1)); note "ok   -- $1"
    else FAIL=$((FAIL + 1)); note "FAIL -- $1"; fi
}

# base_config <input dir> [extra jq object] -- the config every scenario that
# runs to completion starts from.  Stage 4 normally reads FSL's JHU data, and
# the stub toolchain has no FSL, so the dry run points it at the synthetic
# template and label image make_test_data.py writes alongside the series.
base_config() {
    jq -n --arg d "$1" --argjson o "${2:-{\}}" '{
        dwi:($d+"/dwi/dwi.nii.gz"), bvals:($d+"/dwi/dwi.bvals"),
        bvecs:($d+"/dwi/dwi.bvecs"), dwi_json:($d+"/dwi/dwi.json"),
        rdwi:($d+"/rdwi/dwi.nii.gz"), rbvals:($d+"/rdwi/dwi.bvals"),
        rbvecs:($d+"/rdwi/dwi.bvecs"), rdwi_json:($d+"/rdwi/dwi.json"),
        template_fa:($d+"/atlas/template_fa.nii.gz"),
        atlas:($d+"/atlas/atlas_labels.nii.gz"),
        subject:"sub-dry", nthreads:2, eddy_niter:2, eddy_fwhm:"10,0"
    } * $o'
}

# scenario <name> <bindir> <extra jq object> [make_test_data args...]
scenario() {
    local name="$1" bindir="$2" overrides="$3"; shift 3
    SCEN="$ROOT/$name"
    mkdir -p "$SCEN"
    python3 "$HERE/make_test_data.py" --outdir "$SCEN/input" "$@" >/dev/null
    base_config "$SCEN/input" "$overrides" > "$SCEN/config.json"
    printf '\n--- %s ---\n' "$name"
    ( cd "$SCEN" && PATH="$bindir:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) \
        > "$SCEN/log.txt" 2>&1
    SCEN_STATUS=$?
    check "pipeline exits 0" test "$SCEN_STATUS" -eq 0
    [ "$SCEN_STATUS" -eq 0 ] || tail -20 "$SCEN/log.txt"
}

eddy_cmd() { cat "$SCEN"/work/eddy/eddy_corrected.eddy_command_txt 2>/dev/null; }
product()  { jq -r "$1" "$SCEN/product.json" 2>/dev/null; }

# ---------------------------------------------------------------------------
scenario "1-cpu-appa" "$BIN" '{"eddy_binary":"eddy_openmp"}'
check "core outputs exist" bash -c '
    for f in dwi/dwi.nii.gz dwi/dwi.bvals dwi/dwi.bvecs mask/mask.nii.gz \
             tensor/fa.nii.gz tensor/md.nii.gz tensor/ad.nii.gz tensor/rd.nii.gz \
             tensor/tensor.nii.gz roistats/roi_stats.csv reg/atlas_in_native.nii.gz; do
        [ -s "'"$SCEN"'/output/$f" ] || { echo "missing $f"; exit 1; }
    done'
check "gradients match the merged volume count" bash -c '
    n=$(wc -w < "'"$SCEN"'/output/dwi/dwi.bvals")
    [ "$n" = "20" ]'
check "topup ran"                grep -q -- "--topup=" <<< "$(eddy_cmd)"
check "no slice-to-volume on CPU" bash -c '! grep -q -- "--mporder" <<< "$(cat "'"$SCEN"'"/work/eddy/eddy_corrected.eddy_command_txt)"'
# The slspec is still needed without --mporder: group-wise outlier detection
# uses it, and eddy refuses --ol_type=both without the multiband structure.
check "slspec passed even without s2v" grep -q -- "--slspec=" <<< "$(eddy_cmd)"
check "group-wise outlier detection kept" grep -q -- "--ol_type=both" <<< "$(eddy_cmd)"
check "50 ROIs x 4 metrics recorded" bash -c '
    [ "$(tail -n +2 "'"$SCEN"'/output/roistats/roi_stats.csv" | wc -l)" = "200" ]'
check "topup config picked for a /4 matrix" \
    grep -q "topup config: b02b0_4.cnf (32x32x12 divides by 4)" "$SCEN/log.txt"
check "resolved config recorded in provenance" bash -c '
    [ "$(jq -r .provenance.topup_config "'"$SCEN"'/product.json")" = "b02b0_4.cnf" ]'
# eddy renormalises rotated directions, so an unweighted volume comes back NaN
# and MRtrix refuses the table.  The published gradients must be finite.
check "published bvecs are finite" bash -c '
    awk "{for (i = 1; i <= NF; i++) if (\$i != \$i || \$i ~ /[nN]a[nN]|[iI]nf/) exit 1}" \
        "'"$SCEN"'/output/dwi/dwi.bvecs"'
check "the repair is reported" grep -q "restored 0 0 0 for" "$SCEN/log.txt"
check "product.json is valid"    jq empty "$SCEN/product.json"

# ---------------------------------------------------------------------------
scenario "2-cuda-s2v" "$ROOT/bin-gpu" '{"eddy_binary":"eddy_cuda10.2"}'
check "slice-to-volume enabled" grep -q -- "--mporder=6" <<< "$(eddy_cmd)"
check "slspec passed"           grep -q -- "--slspec=" <<< "$(eddy_cmd)"
check "s2v iterations passed"   grep -q -- "--s2v_niter=6" <<< "$(eddy_cmd)"
check "outlier replacement on"  grep -q -- "--repol" <<< "$(eddy_cmd)"
check "group-wise outliers on GPU" grep -q -- "--ol_type=both" <<< "$(eddy_cmd)"
check "product reports s2v"     bash -c '[ "$(jq -r .provenance.slice_to_volume_correction "'"$SCEN"'/product.json")" = "true" ]'
check "slspec published to qc"  test -s "$SCEN/output/qc/slspec.txt"

# ---------------------------------------------------------------------------
# An odd slice count is no longer special: nothing is cropped, so the measured
# slspec still describes the volume and slice-to-volume correction is unaffected.
scenario "3-odd-slices" "$ROOT/bin-gpu" '{"eddy_binary":"eddy_cuda10.2"}' --slices 15 --multiband 3
check "every slice kept"        bash -c '
    python3 -c "
import sys, nibabel as nib
sys.exit(0 if nib.load(sys.argv[1]).shape[2] == 15 else 1)" \
    "'"$SCEN"'/output/dwi/dwi.nii.gz"'
check "nothing was cropped"     bash -c '! grep -qi "dropping the .* slice" "'"$SCEN"'/log.txt"'
check "slspec passed, not --mb" bash -c '
    cmd="$(cat "'"$SCEN"'"/work/eddy/eddy_corrected.eddy_command_txt)"
    grep -q -- "--slspec=" <<< "$cmd" && ! grep -q -- "--mb=" <<< "$cmd"'
check "slspec covers all 15 slices" bash -c '
    [ "$(tr -s "[:space:]" "\n" < "'"$SCEN"'/output/qc/slspec.txt" | grep -c .)" = "15" ]'
check "five excitations of three" bash -c '
    [ "$(grep -c "[^[:space:]]" "'"$SCEN"'/output/qc/slspec.txt")" = "5" ]'
check "still slice-to-volume"   grep -q -- "--mporder=6" <<< "$(eddy_cmd)"
check "odd matrix selects the unsubsampled config" \
    grep -q "topup config: b02b0_1.cnf (32x32x15 divides by 1)" "$SCEN/log.txt"

# ---------------------------------------------------------------------------
# Even but not a multiple of 4, and an explicit override on the same data.
scenario "3b-even-slices" "$BIN" '{"eddy_binary":"eddy_openmp"}' --slices 10 --multiband 2
check "even matrix selects the /2 config" \
    grep -q "topup config: b02b0_2.cnf (32x32x10 divides by 2)" "$SCEN/log.txt"

scenario "3c-explicit-config" "$BIN" '{"eddy_binary":"eddy_openmp","topup_config":"b02b0.cnf"}' --slices 10 --multiband 2
check "an explicit config wins" \
    grep -q "topup config: b02b0.cnf (from config.json)" "$SCEN/log.txt"
check "the explicit config reached topup" bash -c '
    [ "$(jq -r .provenance.topup_config "'"$SCEN"'/product.json")" = "b02b0.cnf" ]'

# ---------------------------------------------------------------------------
printf '\n--- 4-no-reverse-pe ---\n'
SCEN="$ROOT/4-no-reverse-pe"
mkdir -p "$SCEN"
python3 "$HERE/make_test_data.py" --outdir "$SCEN/input" >/dev/null
jq -n --arg d "$SCEN/input" '{
    dwi:($d+"/dwi/dwi.nii.gz"), bvals:($d+"/dwi/dwi.bvals"),
    bvecs:($d+"/dwi/dwi.bvecs"), dwi_json:($d+"/dwi/dwi.json"),
    subject:"sub-dry", nthreads:2, eddy_niter:2, eddy_fwhm:"10,0",
    eddy_binary:"eddy_openmp"
}' > "$SCEN/config.json"

( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) \
    > "$SCEN/log.txt" 2>&1
check "refused"                 test $? -ne 0
check "the refusal says why"    grep -q "opposing phase-encode pair" "$SCEN/log.txt"
check "the refusal names the keys to map" bash -c '
    grep -q "rbvals" "'"$SCEN"'/log.txt"'
check "it stops in stage 0"     bash -c '! grep -q "stage 1:" "'"$SCEN"'/log.txt"'
check "nothing was published"   bash -c '[ ! -e "'"$SCEN"'/output/dwi/dwi.nii.gz" ]'

# --- rdwi without its gradients is refused too ---
SCEN2="$ROOT/4b-partial-reverse"
mkdir -p "$SCEN2"
jq --arg d "$SCEN/input" '.rdwi = ($d + "/rdwi/dwi.nii.gz")' "$SCEN/config.json" \
    > "$SCEN2/config.json"
( cd "$SCEN2" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) \
    > "$SCEN2/log.txt" 2>&1
check "rdwi without rbvals/rbvecs is refused" test $? -ne 0
check "the refusal says all three are needed" \
    grep -q "all three are needed" "$SCEN2/log.txt"

# ---------------------------------------------------------------------------
scenario "5-no-atlas" "$BIN" '{"eddy_binary":"eddy_openmp","atlas_registration":false}'
check "stages 4 and 5 skipped"  grep -q "atlas_registration is false" "$SCEN/log.txt"
check "preprocessing outputs still present" test -s "$SCEN/output/tensor/fa.nii.gz"
check "no ROI tables"           bash -c '[ ! -e "'"$SCEN"'/output/roistats/roi_stats.csv" ]'
check "product.json still written" jq empty "$SCEN/product.json"

# ---------------------------------------------------------------------------
scenario "6-linear-atlas-interp" "$BIN" '{"eddy_binary":"eddy_openmp","atlas_interpolation":"Linear","write_roi_masks":true,"roi_metrics":["FA"]}'
check "linear interpolation logged" grep -q "interpolation: Linear" "$SCEN/log.txt"
check "labels rounded back to integers" grep -q "rounding interpolated label values" "$SCEN/log.txt"
check "50 ROI masks written"    bash -c '[ "$(ls "'"$SCEN"'"/work/reg/roi/Roi_*.nii.gz | wc -l)" = "50" ]'
check "single metric recorded"  bash -c '
    [ "$(tail -n +2 "'"$SCEN"'/output/roistats/roi_stats.csv" | wc -l)" = "50" ]'

# --------------------------------------------------- multi-shell selection ----
# The acquisition this app targets: b=0 / 1500 / 3000, where dtifit must be
# given one diffusion-weighted shell rather than the lot.
SHELL_DATA_ARGS=(--shells "1500,3000" --directions 8)

scenario "7-shell-lowest" "$BIN" '{"eddy_binary":"eddy_openmp","dtifit_shell":"lowest"}' "${SHELL_DATA_ARGS[@]}"
check "three shells detected" bash -c '
    [ "$(jq -r "[.shells[].b] | join(\",\")" "'"$SCEN"'/output/qc/shells.json")" = "5,1500,3000" ]'
check "lowest resolves to b=1500" bash -c '
    [ "$(jq -r .resolved.shell "'"$SCEN"'/output/qc/shells.json")" = "1500" ]'
check "baseline kept in the extraction" bash -c '
    [ "$(jq -r .resolved.extract_arg "'"$SCEN"'/output/qc/shells.json")" = "5,1500" ]'
check "b=3000 volumes dropped before the fit" bash -c '
    awk "{for(i=1;i<=NF;i++) if (\$i+0 > 2000) exit 1}" "'"$SCEN"'/work/extracted/shell.bvals"'
check "provenance records the b-value, not the word" bash -c '
    [ "$(jq -r .provenance.tensor_shell "'"$SCEN"'/product.json")" = "1500" ]'
check "roi table records the resolved shell" bash -c '
    [ "$(jq -r .shell "'"$SCEN"'/output/roistats/roi_stats.json")" = "1500" ]'

scenario "8-shell-highest" "$BIN" '{"eddy_binary":"eddy_openmp","dtifit_shell":"highest"}' "${SHELL_DATA_ARGS[@]}"
check "highest resolves to b=3000" bash -c '
    [ "$(jq -r .resolved.shell "'"$SCEN"'/output/qc/shells.json")" = "3000" ]'
check "b=1500 volumes dropped before the fit" bash -c '
    awk "{for(i=1;i<=NF;i++) if (\$i+0 > 500 && \$i+0 < 2000) exit 1}" "'"$SCEN"'/work/extracted/shell.bvals"'

scenario "9-shell-explicit" "$BIN" '{"eddy_binary":"eddy_openmp","dtifit_shell":1500}' "${SHELL_DATA_ARGS[@]}"
check "an explicit b-value still works" bash -c '
    [ "$(jq -r .resolved.shell "'"$SCEN"'/output/qc/shells.json")" = "1500" ]'
check "resolution reason is recorded" bash -c '
    jq -r .resolved.reason "'"$SCEN"'/output/qc/shells.json" | grep -q "requested b=1500"'

scenario "10-shell-all" "$BIN" '{"eddy_binary":"eddy_openmp","dtifit_shell":"all"}' "${SHELL_DATA_ARGS[@]}"
check "all fits every volume" bash -c '
    [ "$(jq -r .resolved.mode "'"$SCEN"'/output/qc/shells.json")" = "all" ]'
check "no shell extraction happened" bash -c '[ ! -d "'"$SCEN"'/work/extracted" ]'
check "product says every volume was fitted" bash -c '
    jq -r ".brainlife[] | select(.type==\"info\") | .msg" "'"$SCEN"'/product.json" \
    | grep -q "Tensor fitted to every volume"'

printf '\n--- a shell the data does not contain is refused ---\n'
SCEN="$ROOT/11-shell-absent"
mkdir -p "$SCEN"
python3 "$HERE/make_test_data.py" --outdir "$SCEN/input" "${SHELL_DATA_ARGS[@]}" >/dev/null
base_config "$SCEN/input" '{"eddy_binary":"eddy_openmp","dtifit_shell":"800"}' \
    > "$SCEN/config.json"
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) > "$SCEN/log.txt" 2>&1
check "pipeline fails rather than fitting the wrong shell" test $? -ne 0
check "the error names the shells that are present" \
    grep -q "Shells present" "$SCEN/log.txt"

# ------------------------------------------------- acqparams derivation ----
# A DICOM parameter dump instead of a BIDS sidecar: nested fields, repeated per
# volume, no PhaseEncodingDirection and no TotalReadoutTime.  Everything the
# acqparams file needs has to come out of the Siemens CSA fields, with nothing
# entered by hand.
printf '\n--- acqparams derived from a DICOM dump ---\n'
SCEN="$ROOT/12-csa-dump"
mkdir -p "$SCEN"
python3 "$HERE/make_test_data.py" --outdir "$SCEN/input" >/dev/null
for dir in dwi rdwi; do
    [ "$dir" = dwi ] && positive=1 || positive=0
    python3 - "$SCEN/input/$dir/dwi.json" "$positive" <<'EOPY'
import json, sys
path, positive = sys.argv[1], int(sys.argv[2])
timing = json.load(open(path))["SliceTiming"]
json.dump({"global": {"const": {
              "Manufacturer": "Siemens",
              "InPlanePhaseEncodingDirection": "COL",
              "CsaImage.PhaseEncodingDirectionPositive": positive,
              "CsaImage.BandwidthPerPixelPhaseEncode": 29.24}},
           "time": {"samples": {
              # milliseconds, repeated once per volume, with the decimal
              # round-trip jitter a real dump carries
              "CsaImage.MosaicRefAcqTimes":
                  [[t * 1000.0 for t in timing],
                   [t * 1000.0 * (1 + 1e-11) for t in timing]] * 4}}},
          open(path, "w"))
EOPY
done
base_config "$SCEN/input" '{"eddy_binary":"eddy_openmp"}' > "$SCEN/config.json"
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) \
    > "$SCEN/log.txt" 2>&1
check "pipeline exits 0 with no hand-entered acqparams values" test $? -eq 0
check "the pair came out opposite" bash -c '
    awk "{print \$1, \$2, \$3}" "'"$SCEN"'/output/qc/acqparams.txt" | sort -u \
    | tr "\n" "|" | grep -q "^0 -1 0|0 1 0|$"'
check "readout came from the CSA bandwidth" bash -c '
    [ "$(jq -r ".phase_encoding[0].readout_time_source" \
         "'"$SCEN"'/output/qc/prep.json")" = "BandwidthPerPixelPhaseEncode" ]'
check "direction came from the CSA sign flag" bash -c '
    [ "$(jq -r ".phase_encoding[0].pe_source" \
         "'"$SCEN"'/output/qc/prep.json")" = "CsaImage.PhaseEncodingDirectionPositive" ]'
check "slice-to-volume correction survived the nested timings" bash -c '
    [ -s "'"$SCEN"'/output/qc/slspec.txt" ]'

# An explicit value overrides the derivation, which is the Philips escape hatch.
printf '\n--- an explicit readout_time overrides the sidecar ---\n'
SCEN2="$ROOT/13-manual-readout"
cp -r "$SCEN" "$SCEN2"
rm -rf "$SCEN2/work" "$SCEN2/output"
jq '.readout_time = 0.05 | .rreadout_time = 0.05 | .pe_dir = "j-" | .rpe_dir = "j"' \
    "$SCEN/config.json" > "$SCEN2/config.json"
( cd "$SCEN2" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) \
    > "$SCEN2/log.txt" 2>&1
check "pipeline exits 0" test $? -eq 0
check "the manual values are what reached acqparams" bash -c '
    head -1 "'"$SCEN2"'/output/qc/acqparams.txt" | grep -q "^0 -1 0 0.05$"'
check "provenance says config.json" bash -c '
    [ "$(jq -r ".phase_encoding[0].pe_source" \
         "'"$SCEN2"'/output/qc/prep.json")" = "config.json" ]'

# ------------------------------------------------- CUDA eddy without a GPU ----
# $BIN has eddy_cuda10.2 but no nvidia-smi, so a GPU is not visible.
scenario "12-no-gpu-fallback" "$BIN" '{"require_gpu":false}'
check "falls back to a CPU eddy" bash -c '
    grep -qE "^eddy_(openmp|cpu|eddy)?" <<< "$(head -c 20 "'"$SCEN"'"/work/eddy/eddy_corrected.eddy_command_txt)"'
check "the CUDA binary was not used" bash -c '
    ! grep -q "eddy_cuda" "'"$SCEN"'"/work/eddy/eddy_corrected.eddy_command_txt'
check "slice-to-volume was skipped" bash -c '
    ! grep -q -- "--mporder" "'"$SCEN"'"/work/eddy/eddy_corrected.eddy_command_txt'
check "the fallback is announced" grep -q "falling back to" "$SCEN/log.txt"
check "product records no slice-to-volume" bash -c '
    [ "$(jq -r .provenance.slice_to_volume_correction "'"$SCEN"'/product.json")" = "false" ]'

printf '\n--- CUDA eddy without a GPU, require_gpu=true ---\n'
SCEN="$ROOT/13-require-gpu"
mkdir -p "$SCEN"
python3 "$HERE/make_test_data.py" --outdir "$SCEN/input" >/dev/null
base_config "$SCEN/input" '{"require_gpu":true}' > "$SCEN/config.json"
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) > "$SCEN/log.txt" 2>&1
check "pipeline fails rather than running without a GPU" test $? -ne 0
check "the error explains how to proceed" grep -q "no GPU is visible" "$SCEN/log.txt"

# ------------------------------------------- no SliceTiming in the sidecar ----
# The escape hatch: a sidecar with no slice timing cannot yield a slspec, but an
# explicit one supplied in the config restores slice-to-volume correction.
printf '\n--- a sidecar without SliceTiming ---\n'
SCEN="$ROOT/14-no-slicetiming"
mkdir -p "$SCEN"
python3 "$HERE/make_test_data.py" --outdir "$SCEN/input" >/dev/null
for j in "$SCEN"/input/*/dwi.json; do
    jq 'del(.SliceTiming)' "$j" > "$j.tmp" && mv "$j.tmp" "$j"
done
base_config "$SCEN/input" '{"eddy_binary":"eddy_cuda10.2"}' > "$SCEN/config.json"
( cd "$SCEN" && PATH="$ROOT/bin-gpu:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) \
    > "$SCEN/log.txt" 2>&1
check "pipeline exits 0" test $? -eq 0
check "the missing timing is reported" grep -q "could not derive a slspec" "$SCEN/log.txt"
check "eddy still ran (so the negatives below mean something)" \
    test -s "$SCEN/work/eddy/eddy_corrected.eddy_command_txt"
check "no slspec passed" bash -c '
    ! grep -q -- "--slspec" "'"$SCEN"'/work/eddy/eddy_corrected.eddy_command_txt"'
check "slice-to-volume disabled" bash -c '
    ! grep -q -- "--mporder" "'"$SCEN"'/work/eddy/eddy_corrected.eddy_command_txt"'
check "outlier detection degrades to slice-wise" bash -c '
    grep -q -- "--ol_type=sw" "'"$SCEN"'/work/eddy/eddy_corrected.eddy_command_txt"'
check "the degradation is announced" grep -q "falls back to --ol_type=sw" "$SCEN/log.txt"
check "product records no slice-to-volume" bash -c '
    [ "$(jq -r .provenance.slice_to_volume_correction "'"$SCEN"'/product.json")" = "false" ]'

printf '\n--- an explicit slspec restores slice-to-volume correction ---\n'
SCEN2="$ROOT/15-supplied-slspec"
cp -r "$SCEN" "$SCEN2"
rm -rf "$SCEN2/work" "$SCEN2/output"
# 12 slices, multiband 2: six excitations of two slices each, matching the
# interleaved order make_test_data.py writes into SliceTiming.
printf '%s\n' "0 6" "2 8" "4 10" "1 7" "3 9" "5 11" > "$SCEN2/slspec.txt"
jq --arg s "$SCEN2/slspec.txt" '.slspec = $s' "$SCEN/config.json" > "$SCEN2/config.json"
( cd "$SCEN2" && PATH="$ROOT/bin-gpu:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) \
    > "$SCEN2/log.txt" 2>&1
check "pipeline exits 0" test $? -eq 0
check "the supplied file is used" grep -q "using the supplied slspec file" "$SCEN2/log.txt"
check "slspec passed to eddy" bash -c '
    grep -q -- "--slspec=" "'"$SCEN2"'/work/eddy/eddy_corrected.eddy_command_txt"'
check "slice-to-volume enabled" bash -c '
    grep -q -- "--mporder=6" "'"$SCEN2"'/work/eddy/eddy_corrected.eddy_command_txt"'
check "what was published is what was supplied" \
    cmp -s "$SCEN2/slspec.txt" "$SCEN2/output/qc/slspec.txt"

# --- a slspec from a different protocol is refused, not handed to eddy ---
printf '\n--- a slspec from another protocol is refused ---\n'
SCEN3="$ROOT/15b-wrong-slspec"
cp -r "$SCEN" "$SCEN3"
rm -rf "$SCEN3/work" "$SCEN3/output"
# The shipped 84-slice example against 12-slice data: the realistic misuse.
cp "$APP/templates/philips_84_slices_slspec.txt" "$SCEN3/slspec.txt"
jq --arg s "$SCEN3/slspec.txt" '.slspec = $s' "$SCEN/config.json" > "$SCEN3/config.json"
( cd "$SCEN3" && PATH="$ROOT/bin-gpu:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) \
    > "$SCEN3/log.txt" 2>&1
check "a mismatched slspec is refused" test $? -ne 0
check "the refusal says what is wrong" \
    grep -q "does not describe this acquisition" "$SCEN3/log.txt"
check "it stops before eddy ran" bash -c '
    [ ! -e "'"$SCEN3"'/work/eddy/eddy_corrected.eddy_command_txt" ]'

# ------------------------------------------------ declared slice order ----
# The Philips case: the sidecar carries no timings, so the excitation order is
# declared from the protocol instead.  make_test_data.py writes a step-2
# interleave, so "interleaved" is the truthful declaration for this data.
printf '\n--- a declared slice order, with no SliceTiming to derive from ---\n'
SCEN="$ROOT/16-declared-order"
mkdir -p "$SCEN"
python3 "$HERE/make_test_data.py" --outdir "$SCEN/input" >/dev/null
for j in "$SCEN"/input/*/dwi.json; do
    jq 'del(.SliceTiming)' "$j" > "$j.tmp" && mv "$j.tmp" "$j"
done
base_config "$SCEN/input" '{"eddy_binary":"eddy_cuda10.2","slice_order":"interleaved","multiband":2}' \
    > "$SCEN/config.json"
( cd "$SCEN" && PATH="$ROOT/bin-gpu:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) \
    > "$SCEN/log.txt" 2>&1
check "pipeline exits 0"        test $? -eq 0
check "the slspec was generated" grep -q "declared slice order 'interleaved'" "$SCEN/log.txt"
check "slice-to-volume is back on" grep -q -- "--mporder=6" <<< "$(eddy_cmd)"
check "slspec passed to eddy"   grep -q -- "--slspec=" <<< "$(eddy_cmd)"
check "the generated order is the interleave" bash -c '
    [ "$(head -1 "'"$SCEN"'/output/qc/slspec.txt" | tr -s " " | sed "s/^ *//")" = "0 6" ]'

# With timings present the declaration is checked against them, not trusted.
scenario "16b-declared-agrees" "$ROOT/bin-gpu" '{"eddy_binary":"eddy_cuda10.2","slice_order":"interleaved","multiband":2}'
check "the declaration is cross-checked" \
    grep -q "agrees with the sidecar" "$SCEN/log.txt"
check "slice-to-volume still enabled" grep -q -- "--mporder=6" <<< "$(eddy_cmd)"

printf '\n--- a declared slice order that contradicts the sidecar ---\n'
SCEN="$ROOT/16c-declared-conflicts"
mkdir -p "$SCEN"
python3 "$HERE/make_test_data.py" --outdir "$SCEN/input" >/dev/null
base_config "$SCEN/input" '{"eddy_binary":"eddy_cuda10.2","slice_order":"ascending","multiband":2}' \
    > "$SCEN/config.json"
( cd "$SCEN" && PATH="$ROOT/bin-gpu:$PATH" APP_DIR="$APP" bash "$APP/run.sh" ) \
    > "$SCEN/log.txt" 2>&1
check "the contradiction is fatal" test $? -ne 0
check "it names both orders"    grep -q "does not match the SliceTiming" "$SCEN/log.txt"
check "it stops before eddy ran" bash -c '
    [ ! -e "'"$SCEN"'/work/eddy/eddy_corrected.eddy_command_txt" ]'

# ---------------------------------------------------------------------------
printf '\n--- resume from a later stage ---\n'
SCEN="$ROOT/1-cpu-appa"
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run.sh" --from 4 ) \
    > "$SCEN/resume.txt" 2>&1
check "run.sh --from 4 succeeds" test $? -eq 0
check "earlier stages not re-run" bash -c '! grep -q "stage 0:" "'"$SCEN"'/resume.txt"'

printf '\n----------------------------------------\n'
printf 'dryrun.sh: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
