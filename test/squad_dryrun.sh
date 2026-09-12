#!/bin/bash
# Run the group (eddy_squad) mode end to end against the stub toolchain, and
# assert on what it produced.
#
# The per-subject dry run is dryrun.sh; this is its group-level counterpart. It
# covers the two things that actually go wrong in a study-wise QC task: the
# inputs not being the homogeneous cohort SQUAD demands, and the grouping
# variable not lining up with the subject list.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$(cd "$HERE/.." && pwd)"

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
BIN="$ROOT/bin"
bash "$HERE/stubs/install.sh" "$BIN" >/dev/null

PASS=0
FAIL=0
note() { printf '    %s\n' "$*"; }
check() {  # check <description> <condition-command...>
    if "${@:2}"; then PASS=$((PASS + 1)); note "ok   -- $1"
    else FAIL=$((FAIL + 1)); note "FAIL -- $1"; fi
}

# qc_dataset <dir> <subject> <s2v true|false> [field true|false] [readout]
#
# One per-subject eddy QC dataset, laid out the way stage 6 publishes it: the
# QUAD database, the single-subject report, and the signature that says which
# cohort the subject belongs to.
qc_dataset() {
    local dir="$1" subject="$2" s2v="$3" field="${4:-true}" readout="${5:-0.0959}"
    mkdir -p "$dir"
    python3 - "$dir" "$subject" "$s2v" "$field" "$readout" <<'EOPY'
import json, os, subprocess, sys
directory, subject, s2v, field, readout = sys.argv[1:6]
app = os.environ["APP"]
qc = {
    "data_file_eddy": "/work/%s/eddy_corrected" % subject,
    "data_no_dw_vols": 16, "data_no_b0_vols": 4, "data_no_PE_dirs": 2,
    "data_no_shells": 2, "data_unique_bvals": [1500, 3000],
    "data_vox_size": [2.0, 2.0, 2.0],
    "data_protocol": [[1500, 8], [3000, 8]],
    "data_unique_pes": [[0, 1, 0], [0, -1, 0]],
    "data_eddy_para": [[0, 1, 0, float(readout)], [0, -1, 0, float(readout)]],
    "qc_mot_abs": 0.3 + 0.1 * len(subject), "qc_mot_rel": 0.1,
    "qc_outliers_tot": 1.0 + len(subject), "qc_vox_displ_std": 0.7,
    "qc_params_flag": True, "qc_ol_flag": True, "qc_cnr_flag": True,
    "qc_rss_flag": False,
    "qc_s2v_params_flag": s2v == "true",
    "qc_field_flag": field == "true",
    "qc_cnr_avg": [12.0, 2.5, 1.8], "qc_cnr_std": [1.0, 0.3, 0.2],
    "qc_params_avg": [0.0] * 9,
}
if s2v == "true":
    qc["qc_s2v_params_avg_std"] = [0.0] * 6
with open(os.path.join(directory, "qc.json"), "w") as fh:
    json.dump(qc, fh, indent=4)
with open(os.path.join(directory, "qc.pdf"), "w") as fh:
    fh.write("%PDF-1.4 stub\n")
subprocess.check_call([sys.executable, os.path.join(app, "python", "eddyqc_summary.py"),
                       "--qc-json", os.path.join(directory, "qc.json"),
                       "--subject", subject,
                       "--out", os.path.join(directory, "squad_ready.json")],
                      stderr=subprocess.DEVNULL)
EOPY
}

export APP

# group_config <output path> <extra jq object> <dataset dir>...
group_config() {
    local out="$1" overrides="$2"; shift 2
    local paths="[]"
    for dir in "$@"; do
        paths="$(jq -n --argjson a "$paths" --arg p "$dir" '$a + [$p]')"
    done
    jq -n --argjson p "$paths" --argjson o "$overrides" \
        '{eddyqc: $p} * $o' > "$out"
}

# ---------------------------------------------------------------------------
printf '\n--- 1-homogeneous-study ---\n'
SCEN="$ROOT/1-homogeneous"
mkdir -p "$SCEN"
for index in 1 2 3 4; do
    qc_dataset "$SCEN/input/sub-0$index" "sub-0$index" true
done
group_config "$SCEN/config.json" '{}' "$SCEN"/input/sub-0*
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN/log.txt" 2>&1
STATUS=$?
check "group run exits 0" test "$STATUS" -eq 0
[ "$STATUS" -eq 0 ] || tail -20 "$SCEN/log.txt"
check "the study-wise database is published" test -s "$SCEN/output/squad/group_db.json"
check "the study-wise report is published"   test -s "$SCEN/output/squad/group_qc.pdf"
check "the cohort report is published"       test -s "$SCEN/output/squad/cohorts.json"
check "the subject list is published"        test -s "$SCEN/output/squad/subject_list.txt"
check "all four subjects pooled" bash -c '
    [ "$(jq -r ".chosen.n_subjects" "'"$SCEN"'/output/squad/cohorts.json")" = "4" ]'
check "nothing was excluded" bash -c '
    [ "$(jq -r ".excluded | length" "'"$SCEN"'/output/squad/cohorts.json")" = "0" ]'
check "eddy_squad saw the same four subjects" bash -c '
    [ "$(jq -r ".data_no_subjects" "'"$SCEN"'/output/squad/group_db.json")" = "4" ]'
check "product.json is valid" jq empty "$SCEN/product.json"
check "the task page names the cohort" bash -c '
    jq -r ".brainlife[].msg // empty" "'"$SCEN"'/product.json" | grep -q "Pooled 4 subject"'
check "a figure per QC index" bash -c '
    [ "$(jq -r "[.brainlife[] | select(.type==\"plotly\")] | length" \
         "'"$SCEN"'/product.json")" = "4" ]'
check "subjects are named on the figures" bash -c '
    jq -r ".brainlife[] | select(.type==\"plotly\") | .data[0].x[]" \
        "'"$SCEN"'/product.json" | grep -q "sub-01"'
check "no single-subject report updated by default" bash -c '
    [ ! -d "'"$SCEN"'/output/squad/updated" ]'

# ---------------------------------------------------------------------------
# The failure this whole mechanism exists to prevent: some subjects processed on
# a GPU node and some not, which real eddy_squad refuses outright.
printf '\n--- 2-mixed-gpu-and-cpu ---\n'
SCEN="$ROOT/2-mixed"
mkdir -p "$SCEN"
for index in 1 2 3; do
    qc_dataset "$SCEN/input/sub-0$index" "sub-0$index" true
done
for index in 4 5; do
    qc_dataset "$SCEN/input/sub-0$index" "sub-0$index" false
done
group_config "$SCEN/config.json" '{}' "$SCEN"/input/sub-0*
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN/log.txt" 2>&1
STATUS=$?
check "the task still succeeds" test "$STATUS" -eq 0
[ "$STATUS" -eq 0 ] || tail -20 "$SCEN/log.txt"
check "the larger cohort was pooled" bash -c '
    [ "$(jq -r ".chosen.n_subjects" "'"$SCEN"'/output/squad/cohorts.json")" = "3" ]'
check "the other cohort is named, not silently dropped" bash -c '
    [ "$(jq -r "[.excluded[].subjects[]] | join(\",\")" \
         "'"$SCEN"'/output/squad/cohorts.json")" = "sub-04,sub-05" ]'
check "the reason is in the pipeline's own terms" bash -c '
    jq -r ".excluded[0].reason" "'"$SCEN"'/output/squad/cohorts.json" \
    | grep -q "slice-to-volume"'
check "the task page warns about the exclusion" bash -c '
    jq -r ".brainlife[] | select(.type==\"warning\") | .msg" "'"$SCEN"'/product.json" \
    | grep -q "Excluded 2 subject"'
check "both cohorts are recorded for a second run" bash -c '
    [ "$(jq -r ".provenance.cohorts | length" "'"$SCEN"'/product.json")" = "2" ]'
# And the excluded cohort can be analysed on its own, which is the way out.
MINORITY="$(jq -r '.cohorts[] | select(.n_subjects == 2) | .signature_hash' \
            "$SCEN/output/squad/cohorts.json")"
SCEN2="$ROOT/2b-minority-cohort"
mkdir -p "$SCEN2"
jq --arg c "$MINORITY" '. + {cohort: $c}' "$SCEN/config.json" > "$SCEN2/config.json"
( cd "$SCEN2" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN2/log.txt" 2>&1
check "the excluded cohort can be reported on its own" test $? -eq 0
check "it pooled exactly that cohort" bash -c '
    [ "$(jq -r ".chosen.subjects | join(\",\")" "'"$SCEN2"'/output/squad/cohorts.json")" \
      = "sub-04,sub-05" ]'

# --- require_homogeneous refuses rather than choosing for you ---
SCEN3="$ROOT/2c-require-homogeneous"
mkdir -p "$SCEN3"
jq '. + {require_homogeneous: true}' "$SCEN/config.json" > "$SCEN3/config.json"
( cd "$SCEN3" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN3/log.txt" 2>&1
check "require_homogeneous fails the task" test $? -ne 0
check "the failure explains itself" grep -q "incompatible cohorts" "$SCEN3/log.txt"
check "the reason is published for the task page" bash -c '
    jq -r ".error" "'"$SCEN3"'/output/squad/cohorts.json" | grep -q "incompatible cohorts"'
check "product.json still renders the error" bash -c '
    jq -r ".brainlife[] | select(.type==\"error\") | .msg" "'"$SCEN3"'/product.json" \
    | grep -q "incompatible"'

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# The same trap from the other direction: identical eddy options, but a
# different readout time in the acquisition parameters. Newer FSL compares the
# eddy *input* data too and refuses the study over it, so the cohort key has to
# be at least as strict -- and exact, since SQUAD does not round.
printf '\n--- 2d-different-acquisition-parameters ---\n'
SCEN="$ROOT/2d-acqparams"
mkdir -p "$SCEN"
for index in 1 2 3; do
    qc_dataset "$SCEN/input/sub-0$index" "sub-0$index" true true 0.0959
done
for index in 4 5; do
    qc_dataset "$SCEN/input/sub-0$index" "sub-0$index" true true 0.1043
done
group_config "$SCEN/config.json" '{}' "$SCEN"/input/sub-0*
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN/log.txt" 2>&1
STATUS=$?
check "a readout-time difference does not fail the task" test "$STATUS" -eq 0
[ "$STATUS" -eq 0 ] || tail -20 "$SCEN/log.txt"
check "the protocols were split into cohorts" bash -c '
    [ "$(jq -r ".chosen.n_subjects" "'"$SCEN"'/output/squad/cohorts.json")" = "3" ]'
check "the reason names the acquisition parameters" bash -c '
    jq -r ".excluded[0].reason" "'"$SCEN"'/output/squad/cohorts.json" \
    | grep -q "topup acquisition parameters"'
check "and shows both values" bash -c '
    jq -r ".excluded[0].reason" "'"$SCEN"'/output/squad/cohorts.json" | grep -q "0.1043"'
check "eddy_squad never saw the mixture" bash -c '
    ! grep -qi "inconsistency detected" "'"$SCEN"'/log.txt"'

# A shell b-value that differs by 5 is a different cohort too: SQUAD compares
# these exactly, so rounding them together here would fail the whole study.
printf '\n--- 2e-near-identical-bvalues ---\n'
SCEN="$ROOT/2e-bvals"
mkdir -p "$SCEN"
for index in 1 2 3; do
    qc_dataset "$SCEN/input/sub-0$index" "sub-0$index" true
done
for index in 4; do
    qc_dataset "$SCEN/input/sub-0$index" "sub-0$index" true
    python3 - "$SCEN/input/sub-04/qc.json" <<'EOPY'
import json, sys
path = sys.argv[1]
qc = json.load(open(path))
qc["data_unique_bvals"] = [1495, 3000]
json.dump(qc, open(path, "w"), indent=4)
EOPY
done
group_config "$SCEN/config.json" '{}' "$SCEN"/input/sub-0*
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN/log.txt" 2>&1
check "the task succeeds" test $? -eq 0
check "b=1495 is its own cohort" bash -c '
    [ "$(jq -r ".excluded[0].subjects | join(\",\")" \
         "'"$SCEN"'/output/squad/cohorts.json")" = "sub-04" ]'
check "the reason names the b-values" bash -c '
    jq -r ".excluded[0].reason" "'"$SCEN"'/output/squad/cohorts.json" | grep -q "b-values"'

# ...and the study can be pooled anyway, by narrowing what the cohort key
# compares -- for a difference this FSL turns out to tolerate.
SCEN2="$ROOT/2f-narrowed-signature"
mkdir -p "$SCEN2"
jq '. + {signature_fields: "data_no_shells data_no_PE_dirs"}' "$SCEN/config.json" \
    > "$SCEN2/config.json"
( cd "$SCEN2" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN2/log.txt" 2>&1
check "narrowing signature_fields pools them again" bash -c '
    [ "$(jq -r ".chosen.n_subjects" "'"$SCEN2"'/output/squad/cohorts.json" 2>/dev/null)" = "4" ]'
# The stub refuses it, exactly as the real tool would -- and the diagnostic runs.
check "the refusal is diagnosed, not just reported" \
    grep -q "differs across subjects" "$SCEN2/log.txt"
check "the diagnosis names the field" grep -q "data_unique_bvals" "$SCEN2/log.txt"
check "and says how to split them" grep -q "signature_fields" "$SCEN2/log.txt"

printf '\n--- 3-grouping-variable-and-update ---\n'
SCEN="$ROOT/3-grouping"
mkdir -p "$SCEN"
for index in 1 2 3 4; do
    qc_dataset "$SCEN/input/sub-0$index" "sub-0$index" true
done
# Deliberately in a different order from the inputs: SQUAD matches values to
# subjects by position, so the app has to reorder them.
printf 'participant_id\tgroup\n' > "$SCEN/participants.tsv"
printf 'sub-03\t1\nsub-01\t0\nsub-04\t1\nsub-02\t0\n' >> "$SCEN/participants.tsv"
group_config "$SCEN/config.json" \
    "$(jq -n --arg v "$SCEN/participants.tsv" \
        '{grouping_variable: $v, variable_name: "group",
          update_single_subject_reports: true}')" \
    "$SCEN"/input/sub-0*
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN/log.txt" 2>&1
STATUS=$?
check "group run with a variable exits 0" test "$STATUS" -eq 0
[ "$STATUS" -eq 0 ] || tail -20 "$SCEN/log.txt"
check "the variable file is published" test -s "$SCEN/output/squad/grouping_variable.txt"
check "it is in eddy_squad's format" bash -c '
    [ "$(head -2 "'"$SCEN"'/output/squad/grouping_variable.txt" | tr "\n" " ")" = "group 0 " ]'
check "the values follow the subject list, not the table" bash -c '
    [ "$(tail -n +3 "'"$SCEN"'/output/squad/grouping_variable.txt" | tr -d "[:space:]")" \
      = "0011" ]'
check "updated single-subject reports are collected by subject" bash -c '
    [ "$(ls "'"$SCEN"'"/output/squad/updated/*_qc_updated.pdf 2>/dev/null | wc -l)" = "4" ]'
check "each is named after its subject" test -s "$SCEN/output/squad/updated/sub-03_qc_updated.pdf"
check "the task page reports the grouping" bash -c '
    jq -r ".brainlife[].msg // empty" "'"$SCEN"'/product.json" \
    | grep -q "Grouped by .group. (categorical)"'
check "and the updated reports" bash -c '
    jq -r ".brainlife[].msg // empty" "'"$SCEN"'/product.json" \
    | grep -q "Updated 4 single-subject report"'

# --- a table missing a subject is refused before eddy_squad runs ---
SCEN2="$ROOT/3b-incomplete-variable"
mkdir -p "$SCEN2"
printf 'participant_id\tgroup\nsub-01\t0\nsub-02\t1\n' > "$SCEN2/participants.tsv"
jq --arg v "$SCEN2/participants.tsv" '.grouping_variable = $v' "$SCEN/config.json" \
    > "$SCEN2/config.json"
( cd "$SCEN2" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN2/log.txt" 2>&1
check "an incomplete variable table fails the task" test $? -ne 0
check "it names the subjects with no value" bash -c '
    jq -r ".error" "'"$SCEN2"'/output/squad/cohorts.json" | grep -q "sub-03"'
check "nothing was published as if it had worked" bash -c '
    [ ! -e "'"$SCEN2"'/output/squad/group_db.json" ]'

# ---------------------------------------------------------------------------
# --- a subject with no report of its own does not cost the group report ---
SCEN2="$ROOT/3c-missing-subject-report"
mkdir -p "$SCEN2"
cp -r "$SCEN/input" "$SCEN2/input"
rm -f "$SCEN2/input/sub-02/qc.pdf"
cp "$SCEN/participants.tsv" "$SCEN2/participants.tsv"
group_config "$SCEN2/config.json" \
    "$(jq -n --arg v "$SCEN2/participants.tsv" \
        '{grouping_variable: $v, update_single_subject_reports: true}')" \
    "$SCEN2"/input/sub-0*
( cd "$SCEN2" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN2/log.txt" 2>&1
check "the group report survives a subject with no report of its own" test $? -eq 0
check "the update was skipped rather than attempted" \
    grep -q "not updating the single-subject reports" "$SCEN2/log.txt"
check "the subject is named in the log" grep -q "sub-02" "$SCEN2/log.txt"
check "and on the task page" bash -c '
    jq -r ".brainlife[] | select(.type==\"warning\") | .msg" "'"$SCEN2"'/product.json" \
    | grep -q "published no single-subject report"'
check "all four subjects are still in the group database" bash -c '
    [ "$(jq -r ".data_no_subjects" "'"$SCEN2"'/output/squad/group_db.json")" = "4" ]'

# --- and neither does an FSL that cannot merge PDFs ---
# Only the update step needs a PDF library. Stand up an FSL python that
# satisfies every other import and fails to load squad_update, which is what a
# too-eager prune leaves behind.
SCEN3="$ROOT/3d-no-pypdf2"
mkdir -p "$SCEN3/fsl/bin" "$SCEN3/fakemods/eddy_qc/SQUAD"
for module in seaborn pandas matplotlib; do : > "$SCEN3/fakemods/$module.py"; done
: > "$SCEN3/fakemods/eddy_qc/__init__.py"
: > "$SCEN3/fakemods/eddy_qc/SQUAD/__init__.py"
# The real module's first unsatisfied import is its PDF library.
printf 'from PyPDF2 import PdfFileMerger\n' > "$SCEN3/fakemods/eddy_qc/SQUAD/squad_update.py"
cat > "$SCEN3/fsl/bin/python" <<EOSH
#!/bin/sh
PYTHONPATH="$SCEN3/fakemods:\${PYTHONPATH:-}" exec python3 "\$@"
EOSH
chmod +x "$SCEN3/fsl/bin/python"
cp -r "$SCEN/input" "$SCEN3/input"
cp "$SCEN/participants.tsv" "$SCEN3/participants.tsv"
group_config "$SCEN3/config.json" \
    "$(jq -n --arg v "$SCEN3/participants.tsv" \
        '{grouping_variable: $v, update_single_subject_reports: true}')" \
    "$SCEN3"/input/sub-0*
( cd "$SCEN3" && PATH="$BIN:$PATH" APP_DIR="$APP" FSLDIR="$SCEN3/fsl" \
    bash "$APP/run_squad.sh" ) > "$SCEN3/log.txt" 2>&1
check "an FSL without PyPDF2 still produces the group report" test $? -eq 0
check "the update was skipped" grep -q "not updating the single-subject reports" "$SCEN3/log.txt"
check "the missing module is named" grep -q "PyPDF2" "$SCEN3/log.txt"
check "and the group database is there" test -s "$SCEN3/output/squad/group_db.json"

printf '\n--- 4-unusable-inputs ---\n'
SCEN="$ROOT/4-unusable"
mkdir -p "$SCEN/input/empty"
for index in 1 2; do
    qc_dataset "$SCEN/input/sub-0$index" "sub-0$index" true
done
group_config "$SCEN/config.json" '{}' "$SCEN/input/sub-01" "$SCEN/input/sub-02" \
    "$SCEN/input/empty"
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN/log.txt" 2>&1
check "the usable subjects are still reported on" test $? -eq 0
check "the unusable input is named on the task page" bash -c '
    jq -r ".brainlife[] | select(.type==\"warning\") | .msg" "'"$SCEN"'/product.json" \
    | grep -q "was not usable"'

printf '\n--- 5-one-subject ---\n'
SCEN="$ROOT/5-single"
mkdir -p "$SCEN"
qc_dataset "$SCEN/input/sub-01" "sub-01" true
group_config "$SCEN/config.json" '{}' "$SCEN/input/sub-01"
( cd "$SCEN" && PATH="$BIN:$PATH" APP_DIR="$APP" bash "$APP/run_squad.sh" ) \
    > "$SCEN/log.txt" 2>&1
check "a one-subject study is refused" test $? -ne 0
check "the refusal says how many it needs" grep -q "at least 2" "$SCEN/log.txt"

# ---------------------------------------------------------------------------
# ./main has to route a group config to run_squad.sh without being told.
printf '\n--- 6-main-dispatch ---\n'
SCEN="$ROOT/6-dispatch"
mkdir -p "$SCEN"
for index in 1 2; do
    qc_dataset "$SCEN/input/sub-0$index" "sub-0$index" true
done
group_config "$SCEN/config.json" '{}' "$SCEN"/input/sub-0*
( cd "$SCEN" && PATH="$BIN:$PATH" SKIP_CONTAINER=1 bash "$APP/main" ) \
    > "$SCEN/log.txt" 2>&1
STATUS=$?
check "main runs the group driver" test "$STATUS" -eq 0
[ "$STATUS" -eq 0 ] || tail -20 "$SCEN/log.txt"
check "it said which driver it chose" grep -q "driver: run_squad.sh" "$SCEN/log.txt"
check "and checked the group output, not the tensor" bash -c '
    ! grep -q "output/tensor/fa.nii.gz was not produced" "'"$SCEN"'/log.txt"'
check "the group outputs are there" test -s "$SCEN/output/squad/group_db.json"

# ---------------------------------------------------------------------------
# A negative control for the checks above: eddy_squad parses with argparse and
# --update is nargs="?", so a subject list placed after it is swallowed as its
# value. If the stub did not model that, the argument order the group stage
# depends on would be untested.
printf '\n--- 7-argument-order-is-load-bearing ---\n'
( cd "$ROOT" && PATH="$BIN:$PATH" eddy_squad --update "$ROOT/6-dispatch/work/squad/list.txt" \
    --output-dir "$ROOT/7-order/squad" ) > "$ROOT/order.txt" 2>&1
check "the list after --update is lost, as argparse would lose it" test $? -ne 0
check "and the stub says why" grep -q "no subject list given" "$ROOT/order.txt"
( cd "$ROOT" && PATH="$BIN:$PATH" eddy_squad "$ROOT/6-dispatch/work/squad/list.txt" \
    --output-dir "$ROOT/7-order/squad" --update ) > "$ROOT/order-ok.txt" 2>&1
check "the order the group stage uses works" test $? -eq 0

printf '\n----------------------------------------\n'
printf 'squad_dryrun.sh: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
