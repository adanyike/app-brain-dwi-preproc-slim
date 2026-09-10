#!/bin/bash
# Stage 1 -- estimate the susceptibility field with topup and build the brain
# mask that eddy will use.
#
# The b=0 volumes selected in stage 0 are pulled out one at a time and merged in
# the order recorded in topup_b0s.txt, so that row N of acqparams.txt describes
# volume N of --imain.  An explicit list rather than a shell glob: a glob sorts
# bzero_10 before bzero_2 and silently misorders the merge.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
source "$WORK_DIR/state.sh"
timer_start

TOPUP_DIR="$WORK_DIR/topup"
mkdir -p "$TOPUP_DIR"

B0_LIST="$WORK_DIR/prep/topup_b0s.txt"
B0_MERGED="$TOPUP_DIR/b0.nii.gz"
TOPUP_BASE="$TOPUP_DIR/topup"
MEAN_B0="$TOPUP_DIR/topup_meanb0.nii.gz"
BRAIN_MASK="$WORK_DIR/prep/topup_mask.nii.gz"

BET_F="$(cfg bet_topup_f 0.4)"

log "extracting b=0 volumes for topup"
b0_files=()
i=0
while read -r volume; do
    [ -z "$volume" ] && continue
    out="$TOPUP_DIR/$(printf 'bzero_%04d.nii.gz' "$i")"
    fslroi "$DWI_PREPARED" "$out" "$volume" 1
    b0_files+=("$out")
    i=$(( i + 1 ))
done < "$B0_LIST"
[ ${#b0_files[@]} -gt 0 ] || die "no b=0 volumes were extracted for topup"

fslmerge -t "$B0_MERGED" "${b0_files[@]}"
rm -f "${b0_files[@]}"
log "merged ${#b0_files[@]} b=0 volumes into $(basename "$B0_MERGED")"

acq_rows="$(grep -c '[^[:space:]]' "$ACQPARAMS")"
[ "$acq_rows" = "${#b0_files[@]}" ] || \
    die "acqparams.txt has $acq_rows rows but $B0_MERGED has ${#b0_files[@]} volumes"

if [ "$HAS_REVERSE_PE" = true ]; then
    TOPUP_CONFIG="$(cfg topup_config b02b0.cnf)"
    # Estimate movement for the first two levels only, use the
    # scaled-conjugate-gradient minimiser after that, and never subsample:
    # brain data at this resolution converges fine and subsampling costs
    # accuracy.
    TOPUP_EXTRA="$(cfg topup_extra '--estmov=1,1,0,0,0,0,0,0,0 --minmet=0,0,1,1,1,1,1,1,1 --subsamp=1,1,1,1,1,1,1,1,1')"

    log "running topup (config $TOPUP_CONFIG)"
    # TOPUP_EXTRA is a deliberate argument list, so word splitting is wanted here.
    # shellcheck disable=SC2086
    topup --imain="$B0_MERGED" \
          --datain="$ACQPARAMS" \
          --config="$TOPUP_CONFIG" \
          --out="$TOPUP_BASE" \
          --iout="${TOPUP_BASE}_iout" \
          --fout="${TOPUP_BASE}_fout" \
          $TOPUP_EXTRA \
          --verbose

    fslmaths "${TOPUP_BASE}_iout.nii.gz" -Tmean "$MEAN_B0"
    TOPUP_APPLIED=true
else
    # Unreachable: stage 0 refuses to run without a reverse series, and
    # prepare_inputs.py refuses a pair that reports the same phase-encoding
    # vector.  Assert it rather than quietly producing uncorrected output.
    die "internal error: reached stage 1 with a single phase-encoding direction, which stage 0 should have refused"
fi

log "brain extraction on the mean b=0 (bet -f $BET_F)"
bet "$MEAN_B0" "$WORK_DIR/prep/topup" -m -n -f "$BET_F"
fslmaths "$WORK_DIR/prep/topup_mask.nii.gz" -fillh "$WORK_DIR/prep/topup_mask_filled.nii.gz"
mv "$WORK_DIR/prep/topup_mask_filled.nii.gz" "$BRAIN_MASK"

cat >> "$WORK_DIR/state.sh" <<EOSTATE
TOPUP_BASE="$TOPUP_BASE"
TOPUP_APPLIED=$TOPUP_APPLIED
TOPUP_MEAN_B0="$MEAN_B0"
BRAIN_MASK="$BRAIN_MASK"
EOSTATE

timer_report "stage 1 (topup)"
