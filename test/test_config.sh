#!/bin/bash
# Tests for the config helpers in src/common.sh.  No neuroimaging tools needed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$HERE/.." && pwd)"
export APP_DIR

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export CONFIG="$TMP/config.json"
export WORK_DIR="$TMP/work"
export OUT_DIR="$TMP/output"

cat > "$CONFIG" <<'EOJSON'
{
  "dwi": "REPLACED_AT_RUNTIME",
  "empty_string": "",
  "explicit_null": null,
  "number": 0.4,
  "int": 6,
  "yes": true,
  "no": false,
  "metrics_array": ["FA", "MD", "AD"],
  "metrics_string": "FA,MD",
  "metrics_spaced": "FA MD RD",
  "flag_one": 1,
  "flag_zero": 0,
  "flag_word": "yes",
  "bad_bool": "maybe",
  "pe_auto": "auto",
  "pe_auto_caps": "AUTO",
  "pe_manual": "j-",
  "readout_manual": 0.0342
}
EOJSON
touch "$TMP/dwi.nii.gz"
tmp_json="$(jq --arg p "$TMP/dwi.nii.gz" '.dwi = $p' "$CONFIG")"
printf '%s' "$tmp_json" > "$CONFIG"

# common.sh sets -e; run each check in a subshell so a deliberate failure in one
# assertion cannot take the whole file down.
source "$APP_DIR/src/common.sh"
set +e

PASS=0
FAIL=0
check() {  # check <description> <expected> <actual>
    if [ "$2" = "$3" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL: %s\n  expected: %s\n  actual:   %s\n' "$1" "$2" "$3" >&2
    fi
}

check "present string"          "$TMP/dwi.nii.gz" "$(cfg dwi)"
check "missing key uses default" "fallback"       "$(cfg not_a_key fallback)"
check "empty string uses default" "fallback"      "$(cfg empty_string fallback)"
check "explicit null uses default" "fallback"     "$(cfg explicit_null fallback)"
check "number"                  "0.4"             "$(cfg number)"
check "integer"                 "6"               "$(cfg int)"

check "bool true"               "true"            "$(cfg_bool yes false)"
check "bool false"              "false"           "$(cfg_bool no true)"
check "bool default when absent" "true"           "$(cfg_bool absent true)"
check "bool from 1"             "true"            "$(cfg_bool flag_one false)"
check "bool from 0"             "false"           "$(cfg_bool flag_zero true)"
check "bool from word"          "true"            "$(cfg_bool flag_word false)"

( cfg_bool bad_bool true ) >/dev/null 2>&1
check "non-boolean value is rejected" "1" "$([ $? -ne 0 ] && echo 1 || echo 0)"

check "list from JSON array"    "FA MD AD"        "$(cfg_list metrics_array 'X')"
check "list from comma string"  "FA MD"           "$(cfg_list metrics_string 'X')"
check "list from spaced string" "FA MD RD"        "$(cfg_list metrics_spaced 'X')"
check "list default"            "FA MD AD RD"     "$(cfg_list absent 'FA MD AD RD')"

# auto/manual keys: "auto" and "absent" both mean "derive it from the sidecar",
# which the callers signal by passing nothing through to the Python layer.
check "manual value passes through" "j-"    "$(cfg_manual pe_manual)"
check "manual number passes through" "0.0342" "$(cfg_manual readout_manual)"
check "auto yields nothing"         ""      "$(cfg_manual pe_auto)"
check "auto is case-insensitive"    ""      "$(cfg_manual pe_auto_caps)"
check "absent key defaults to auto" ""      "$(cfg_manual not_a_key)"

check "cfg_path resolves to absolute" "$TMP/dwi.nii.gz" "$(cfg_path dwi)"
check "cfg_path empty when unset"     ""                "$(cfg_path absent)"

( cfg_path explicit_missing_file ) >/dev/null 2>&1
printf '%s' "$(jq '. + {ghost: "/no/such/file.nii.gz"}' "$CONFIG")" > "$CONFIG"
( cfg_path ghost ) >/dev/null 2>&1
check "cfg_path rejects a missing file" "1" "$([ $? -ne 0 ] && echo 1 || echo 0)"

( require_path absent ) >/dev/null 2>&1
check "require_path rejects a missing key" "1" "$([ $? -ne 0 ] && echo 1 || echo 0)"

check "is_true true"  "0" "$(is_true true;  echo $?)"
check "is_true false" "1" "$(is_true false; echo $?)"

check "is_cuda_eddy cuda"  "0" "$(is_cuda_eddy eddy_cuda10.2; echo $?)"
check "is_cuda_eddy openmp" "1" "$(is_cuda_eddy eddy_openmp;  echo $?)"
check "is_cuda_eddy unsuffixed" "0" "$(is_cuda_eddy eddy_cuda; echo $?)"

# ---------------------------------------------------------------- find_eddy ----
# FSL keeps renaming this binary, so the search must discover what is installed
# rather than match a list of names we happened to know about.
FAKE_BIN="$TMP/fakebin"
mkdir -p "$FAKE_BIN"
make_fake() { for n in "$@"; do printf '#!/bin/sh\n' > "$FAKE_BIN/$n"; chmod +x "$FAKE_BIN/$n"; done; }
clear_fake() { rm -f "$FAKE_BIN"/*; }
# A fixed minimal PATH: coreutils stay reachable, but the only eddy binaries
# in scope are the fakes, so the result does not depend on the host.
with_fake_path() { ( PATH="$FAKE_BIN:/usr/bin:/bin"; find_eddy ); }

# FSL 6.0.7.18 and 6.0.7.19 ship this name; an enumerated list missed it and
# silently fell through to CPU eddy, losing slice-to-volume correction.
clear_fake; make_fake eddy_cuda11.0 eddy_openmp
check "picks eddy_cuda11.0 over CPU eddy" "eddy_cuda11.0" "$(with_fake_path)"

# FSL 6.0.7.20 onwards dropped the version suffix.
clear_fake; make_fake eddy_cuda eddy_openmp
check "picks the unsuffixed eddy_cuda" "eddy_cuda" "$(with_fake_path)"

# A name no release has used yet must still be found.
clear_fake; make_fake eddy_cuda13.7 eddy
check "picks an unknown future CUDA name" "eddy_cuda13.7" "$(with_fake_path)"

clear_fake; make_fake eddy_cuda10.2 eddy_cuda11.0 eddy_cuda9.1
check "picks the highest CUDA version" "eddy_cuda11.0" "$(with_fake_path)"

clear_fake; make_fake eddy_cuda eddy_cuda11.0
check "unsuffixed wins over a suffixed one" "eddy_cuda" "$(with_fake_path)"

clear_fake; make_fake eddy_openmp eddy
check "falls back to CPU eddy when no CUDA build exists" "eddy_openmp" "$(with_fake_path)"

clear_fake; make_fake eddy
check "falls back to plain eddy" "eddy" "$(with_fake_path)"

# A non-executable file must not be mistaken for a binary.
clear_fake; make_fake eddy_openmp; : > "$FAKE_BIN/eddy_cuda11.0"
check "ignores a non-executable eddy_cuda" "eddy_openmp" "$(with_fake_path)"

clear_fake
( PATH="$FAKE_BIN:/usr/bin:/bin"; find_eddy ) >/dev/null 2>&1
check "no eddy at all is an error" "1" "$([ $? -ne 0 ] && echo 1 || echo 0)"

# find_cpu_eddy backs the require_gpu=false fallback, so it must never hand back
# a CUDA build. Plain `eddy` is last: in recent FSL it dispatches to CUDA.
clear_fake; make_fake eddy_cuda11.0 eddy_openmp eddy_cpu eddy
check "cpu search skips CUDA builds" "eddy_openmp" \
      "$( PATH="$FAKE_BIN:/usr/bin:/bin"; find_cpu_eddy )"

clear_fake; make_fake eddy_cuda eddy_cpu eddy
check "cpu search prefers eddy_cpu over plain eddy" "eddy_cpu" \
      "$( PATH="$FAKE_BIN:/usr/bin:/bin"; find_cpu_eddy )"

clear_fake; make_fake eddy_cuda
( PATH="$FAKE_BIN:/usr/bin:/bin"; find_cpu_eddy ) >/dev/null 2>&1
check "cpu search reports failure when only CUDA exists" "1" \
      "$([ $? -ne 0 ] && echo 1 || echo 0)"

# ------------------------------------------------------- brainlife _inputs ----
# Without this fallback every task labels its rows "subject", and a batch
# concatenates into one indistinguishable block.
meta_cfg() { printf '%s' "$1" > "$TMP/meta.json"; CONFIG="$TMP/meta.json"; }

meta_cfg '{"_inputs":[{"id":"dwi","meta":{"subject":"sub-01","session":"ses-01"}}]}'
check "subject from _inputs metadata" "sub-01" "$(cfg_input_meta subject)"
check "session from _inputs metadata" "ses-01" "$(cfg_input_meta session)"

# Only _inputs[0].meta.subject is documented, so the lookup must not assume a
# position: metadata can sit on any entry.
meta_cfg '{"_inputs":[{"id":"dwi"},{"id":"rdwi","meta":{"subject":"sub-07"}}]}'
check "finds metadata on a later input" "sub-07" "$(cfg_input_meta subject)"

meta_cfg '{"_inputs":[{"id":"dwi","meta":{"subject":""}}]}'
check "empty metadata value is not used" "" "$(cfg_input_meta subject)"

meta_cfg '{"_inputs":[{"id":"dwi","meta":{"subject":"s1"}}]}'
check "absent field yields empty" "" "$(cfg_input_meta nosuchfield)"

meta_cfg '{"dwi":"x"}'
check "no _inputs at all yields empty" "" "$(cfg_input_meta subject)"

meta_cfg '{"_inputs":"not-an-array"}'
check "malformed _inputs does not crash" "" "$(cfg_input_meta subject)"

CONFIG="$TMP/config.json"

# ------------------------------------------------------------- run label ----
check "run_identifier honours TASK_ID" "abc123" "$(TASK_ID=abc123 run_identifier)"
check "run_identifier falls back to the run directory" "$(basename "$PWD")" \
      "$(TASK_ID='' run_identifier)"

printf '%s: %d passed, %d failed\n' "$(basename "$0")" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
