#!/bin/bash
# Stage 5 -- average each diffusion metric inside every atlas ROI.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
source "$WORK_DIR/state.sh"
timer_start

STATS_DIR="$WORK_DIR/roistats"
mkdir -p "$STATS_DIR"

# Stage 0 settles the labels and carries them in state.sh. Resolve them again
# when they are absent, so `run.sh --only 5` against a work directory written by
# an older version of the app still labels its rows.
[ -n "${SUBJECT:-}" ] || resolve_labels
SESSION="${SESSION:-}"
RUN_ID="${RUN_ID:-$(run_identifier)}"

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
