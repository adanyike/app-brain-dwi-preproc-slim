#!/bin/bash
# Group stage -- study-wise eddy QC with FSL's eddy_squad.
#
# Not part of the per-subject pipeline: this runs once for a whole study, from
# run_squad.sh, against the per-subject eddy QC datasets the pipeline published
# (output/eddyqc, each holding a qc.json).
#
#   output/squad/group_qc.pdf      the study-wise report
#   output/squad/group_db.json     the study-wise database
#   output/squad/cohorts.json      who pooled, who did not, and why
#   output/squad/updated/          single-subject reports with group context
#
# Two things about eddy_squad shape this stage. It reads nothing but qc.json from
# each folder, so staging is cheap; and with -u it writes qc_updated.pdf *into*
# the folders it was given, which on brainlife are read-only inputs -- hence the
# copy into work/ that python/squad_inputs.py performs.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
timer_start

SQUAD_WORK="$WORK_DIR/squad"
COHORTS="$SQUAD_WORK/cohorts.json"
mkdir -p "$SQUAD_WORK" "$OUT_DIR/squad"

have eddy_squad || die "eddy_squad is not on PATH. It ships with FSL's eddy QC tools (FSL >= 6.0.1); run this app through its container."

# ------------------------------------------------------------- the cohort ----
MIN_SUBJECTS="$(cfg min_subjects 2)"
if ! python3 "$APP_DIR/python/squad_inputs.py" \
        --config "$CONFIG" --work-dir "$SQUAD_WORK" \
        --out "$COHORTS" --min-subjects "$MIN_SUBJECTS"; then
    # cohorts.json carries the reason; publish it so the failure is visible on
    # the task page rather than only in the log.
    [ -f "$COHORTS" ] && cp "$COHORTS" "$OUT_DIR/squad/cohorts.json" || true
    python3 "$APP_DIR/python/make_group_product.py" \
        --cohorts "$COHORTS" --out "$PWD/product.json" 2>/dev/null || true
    die "the inputs could not be staged for eddy_squad (see output/squad/cohorts.json)"
fi

LIST_FILE="$(jq -r '.list_file // empty' "$COHORTS")"
VARIABLE_FILE="$(jq -r '.variable_file // empty' "$COHORTS")"
N_SUBJECTS="$(jq -r '.chosen.n_subjects // 0' "$COHORTS")"
[ -n "$LIST_FILE" ] && [ -s "$LIST_FILE" ] || die "no subject list was staged"
log "pooling $N_SUBJECTS subject(s) from $LIST_FILE"

# --------------------------------------------------------------- the run ----
# eddy_squad refuses to write into an existing directory, and a resumed task
# has one.
SQUAD_OUT="$SQUAD_WORK/squad"
rm -rf "$SQUAD_OUT"

# Order matters: eddy_squad parses with argparse and declares --update as
# nargs="?", so anything following it is taken as its value. The subject list
# goes first and --update last, or the list disappears into the update option.
SQUAD_ARGS=("$LIST_FILE" --output-dir "$SQUAD_OUT")
if [ -n "$VARIABLE_FILE" ] && [ -s "$VARIABLE_FILE" ]; then
    log "grouping variable: $(head -1 "$VARIABLE_FILE") ($(sed -n 2p "$VARIABLE_FILE" | grep -q 1 && echo continuous || echo categorical))"
    SQUAD_ARGS+=(--grouping "$VARIABLE_FILE")
fi

UPDATE_REPORTS="$(cfg_bool update_single_subject_reports false)"
if is_true "$UPDATE_REPORTS"; then
    # SQUAD's update step opens each listed subject's qc.pdf to append the
    # study-wise pages to it, so a single subject that published no report would
    # take the whole group run down. The group report is the deliverable; skip
    # the update rather than lose it.
    MISSING="$(jq -r '(.missing_reports // []) | join(", ")' "$COHORTS")"
    if [ -n "$MISSING" ]; then
        warn "not updating the single-subject reports: no qc.pdf for $MISSING. Re-run those subjects with eddy_qc enabled, or unset update_single_subject_reports."
        UPDATE_REPORTS=false
    else
        SQUAD_ARGS+=(--update)
    fi
fi

log "running: eddy_squad ${SQUAD_ARGS[*]}"
eddy_squad "${SQUAD_ARGS[@]}" || die "eddy_squad failed; see the log above"

[ -f "$SQUAD_OUT/group_db.json" ] || die "eddy_squad produced no group_db.json"

# -------------------------------------------------------------- outputs ----
cp "$SQUAD_OUT/group_db.json" "$OUT_DIR/squad/group_db.json"
[ -f "$SQUAD_OUT/group_qc.pdf" ] && cp "$SQUAD_OUT/group_qc.pdf" "$OUT_DIR/squad/group_qc.pdf" \
    || warn "eddy_squad wrote no group_qc.pdf"
cp "$COHORTS" "$OUT_DIR/squad/cohorts.json"
cp "$LIST_FILE" "$OUT_DIR/squad/subject_list.txt"
[ -n "$VARIABLE_FILE" ] && [ -s "$VARIABLE_FILE" ] && \
    cp "$VARIABLE_FILE" "$OUT_DIR/squad/grouping_variable.txt" || true

# With --update, each subject's report is rewritten inside its staged folder.
# Collect them under the subject label, because a directory of files all called
# qc_updated.pdf is no use to anyone downloading the dataset.
UPDATED=0
if is_true "$UPDATE_REPORTS"; then
    mkdir -p "$OUT_DIR/squad/updated"
    while IFS= read -r folder; do
        [ -n "$folder" ] || continue
        [ -f "$folder/qc_updated.pdf" ] || continue
        cp "$folder/qc_updated.pdf" "$OUT_DIR/squad/updated/$(basename "$folder")_qc_updated.pdf"
        UPDATED=$((UPDATED + 1))
    done < "$LIST_FILE"
    log "collected $UPDATED updated single-subject report(s)"
    [ "$UPDATED" -gt 0 ] || warn "--update was requested but eddy_squad updated no single-subject reports (the QUAD folders may not have carried their qc.pdf)"
fi

python3 "$APP_DIR/python/make_group_product.py" \
    --cohorts "$COHORTS" \
    --group-db "$OUT_DIR/squad/group_db.json" \
    --updated-reports "$UPDATED" \
    --out "$PWD/product.json"

log "outputs written to $OUT_DIR/squad"
find "$OUT_DIR/squad" -maxdepth 2 -type f | sort | sed 's|^|  |' >&2

timer_report "group stage (eddy_squad)"
