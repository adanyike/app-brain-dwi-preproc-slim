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

FLIRTSCH="$FSLDIR/etc/flirtsch"

# topup_config_for <nifti> -- the topup config appropriate for this matrix size.
#
# topup requires the image size to be an integer multiple of every sub-sampling
# level in its config, and FSL ships one config per level: b02b0_4.cnf needs
# every dimension divisible by 4, b02b0_2.cnf (aliased as b02b0.cnf) by 2, and
# b02b0_1.cnf does not sub-sample and so accepts any size.  Sub-sampling only
# buys speed -- FSL states the results are very close to identical -- so take
# the fastest config the data actually allows rather than making the caller
# work it out, or defaulting to the slowest one for everybody.
# https://fsl.fmrib.ox.ac.uk/fsl/docs/diffusion/topup/users_guide/index.html
topup_config_for() {
    local image="$1" d1 d2 d3 entry level name
    d1="$(fslval "$image" dim1 | tr -d '[:space:]')"
    d2="$(fslval "$image" dim2 | tr -d '[:space:]')"
    d3="$(fslval "$image" dim3 | tr -d '[:space:]')"

    # Fastest first.  Both spellings of the level-2 config are offered because
    # releases before 6.0.5 shipped only b02b0.cnf; a size divisible by 4 is
    # also divisible by 2, so stepping down the list is always safe.
    for entry in 4:b02b0_4.cnf 2:b02b0_2.cnf 2:b02b0.cnf 1:b02b0_1.cnf; do
        level="${entry%%:*}"; name="${entry#*:}"
        [ $(( d1 % level )) -eq 0 ] && [ $(( d2 % level )) -eq 0 ] \
            && [ $(( d3 % level )) -eq 0 ] || continue
        # Only offer a file that is really there -- but when FSL's config
        # directory is not visible at all, trust the canonical name rather than
        # falling through to a config the data cannot use.
        [ ! -d "$FLIRTSCH" ] || [ -f "$FLIRTSCH/$name" ] || continue
        log "topup config: $name (${d1}x${d2}x${d3} divides by $level)"
        printf '%s' "$name"
        return 0
    done
    die "no topup config in $FLIRTSCH suits a ${d1}x${d2}x${d3} matrix; set 'topup_config' explicitly"
}

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
    TOPUP_CONFIG="$(cfg_manual topup_config)"
    if [ -z "$TOPUP_CONFIG" ]; then
        TOPUP_CONFIG="$(topup_config_for "$B0_MERGED")"
    else
        log "topup config: $TOPUP_CONFIG (from config.json)"
    fi
    # Estimate movement for the first two levels only, and use the
    # scaled-conjugate-gradient minimiser after that.  The entry counts here
    # must match the number of levels in topup_config; every b02b0* config
    # FSL ships uses the same nine.
    TOPUP_EXTRA="$(cfg topup_extra '--estmov=1,1,0,0,0,0,0,0,0 --minmet=0,0,1,1,1,1,1,1,1')"

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
TOPUP_CONFIG="$TOPUP_CONFIG"
TOPUP_MEAN_B0="$MEAN_B0"
BRAIN_MASK="$BRAIN_MASK"
EOSTATE

timer_report "stage 1 (topup)"
