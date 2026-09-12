#!/bin/bash
# In-container driver for the group (study-wise) QC mode.
#
# The per-subject pipeline is run.sh; this is its group-level counterpart, and
# the two are separate Apps on brainlife that happen to share this repository
# and its container. ./main decides which one a task wants (see the dispatch
# there) from the inputs in config.json.
#
#   ./run_squad.sh       run eddy_squad over every eddy QC dataset given
#
# Input: config.json mapping 'eddyqc' to the per-subject eddy QC datasets, which
# brainlife writes as a JSON array when the App's input allows multiple
# datasets, with '_inputs' describing them in the same order.

set -euo pipefail

export APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
export WORK_DIR="${WORK_DIR:-$PWD/work}"
export OUT_DIR="${OUT_DIR:-$PWD/output}"
export CONFIG="${CONFIG:-$PWD/config.json}"

source "$APP_DIR/src/common.sh"

[ -f "$CONFIG" ] || die "no config.json in $PWD"

mkdir -p "$WORK_DIR" "$OUT_DIR"
setup_threads

check_tools jq python3 eddy_squad

# eddy_squad draws its study-wise plots with seaborn, which eddy_quad never
# imports -- so an image trimmed against the per-subject pipeline can carry a
# working eddy_quad and an eddy_squad that dies at the plotting step, after the
# staging work is done. Check it up front, under FSL's own interpreter.
FSL_PYTHON="${FSLDIR:-/opt/fsl}/bin/python"
if [ -x "$FSL_PYTHON" ]; then
    MISSING_MODS="$("$FSL_PYTHON" - <<'EOPY' 2>/dev/null
missing = []
for module in ("seaborn", "pandas", "matplotlib", "numpy"):
    try:
        __import__(module)
    except Exception:
        missing.append(module)
print(",".join(missing))
EOPY
)"
    [ -z "$MISSING_MODS" ] || \
        die "eddy_squad needs these python modules and FSL's interpreter cannot import them: $MISSING_MODS. Install them into $FSL_PYTHON's environment, or use an image whose FSL is intact."
fi

log "group QC start"
T0="$(date +%s)"

bash "$APP_DIR/src/group_squad.sh"

TOTAL=$(( $(date +%s) - T0 ))
log "group QC finished in $((TOTAL / 60))m $((TOTAL % 60))s"
