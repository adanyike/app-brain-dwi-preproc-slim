#!/bin/bash
# Stage 5 -- average each diffusion metric inside every atlas ROI.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
source "$WORK_DIR/state.sh"
timer_start

STATS_DIR="$WORK_DIR/roistats"
mkdir -p "$STATS_DIR"

# An explicit config key wins; otherwise take it from brainlife's input
# metadata. Without this, every task labels its rows "subject", and a batch
# concatenates into one indistinguishable block.
SUBJECT="$(cfg subject "")"
[ -z "$SUBJECT" ] && SUBJECT="$(cfg_input_meta subject)"
[ -z "$SUBJECT" ] && SUBJECT="subject"

SESSION="$(cfg session "")"
[ -z "$SESSION" ] && SESSION="$(cfg_input_meta session)"

# Optional: pull a session off the end of the subject label (sub01-MR03 ->
# sub01 + MR03). Off by default -- an explicit session, or brainlife's input
# metadata, is always preferred to guessing from a string.
if [ -z "$SESSION" ] && is_true "$(cfg_bool split_subject_session false)"; then
    SPLIT="$(python3 "$APP_DIR/python/labels.py" --subject "$SUBJECT" --split \
             --session-prefixes "$(cfg session_prefixes 'ses,MR,visit,tp,V')")"
    SUBJECT="${SPLIT%%$'\t'*}"
    SESSION="${SPLIT#*$'\t'}"
fi

RUN_ID="$(run_identifier)"
log "labelling results: subject=$SUBJECT session=${SESSION:-<none>} run_id=$RUN_ID"

METRIC_ARGS=()
for metric in $(cfg_list roi_metrics "FA MD AD RD"); do
    case "$metric" in
        FA) path="$FA_MAP" ;;
        MD) path="$MD_MAP" ;;
        AD) path="$AD_MAP" ;;
        RD) path="$RD_MAP" ;;
        *)  warn "unknown metric '$metric' in roi_metrics -- ignoring"; continue ;;
    esac
    METRIC_ARGS+=(--metric "${metric}=${path}")
done
[ ${#METRIC_ARGS[@]} -gt 0 ] || die "roi_metrics selected no usable metric"

STATS_ARGS=(--atlas "$ATLAS_NATIVE" --labels "$ATLAS_LABELS"
            "${METRIC_ARGS[@]}"
            --subject "$SUBJECT" --session "$SESSION" --run-id "$RUN_ID"
            --shell "$DTIFIT_SHELL" --outdir "$STATS_DIR")

is_true "$(cfg_bool roi_restrict_to_mask true)" && STATS_ARGS+=(--brain-mask "$FINAL_MASK") || true
is_true "$(cfg_bool roi_exclude_zeros false)"   && STATS_ARGS+=(--exclude-zeros) || true

python3 "$APP_DIR/python/roi_stats.py" "${STATS_ARGS[@]}"

cat >> "$WORK_DIR/state.sh" <<EOSTATE
ROI_STATS_DIR="$STATS_DIR"
SUBJECT="$SUBJECT"
SESSION="$SESSION"
RUN_ID="$RUN_ID"
EOSTATE

timer_report "stage 5 (ROI statistics)"
