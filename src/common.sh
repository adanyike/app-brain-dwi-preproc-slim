#!/bin/bash
# Shared helpers for every stage of the app.  Sourced, never executed.

set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WORK_DIR="${WORK_DIR:-$PWD/work}"
OUT_DIR="${OUT_DIR:-$PWD/output}"
CONFIG="${CONFIG:-$PWD/config.json}"
TEMPLATE_DIR="${TEMPLATE_DIR:-$APP_DIR/templates}"
FSLDIR="${FSLDIR:-/opt/fsl}"
# The JHU ICBM-DTI-81 FA template and label image ship with FSL, so the app
# reads them from the installation rather than carrying its own copies.
JHU_DIR="${JHU_DIR:-$FSLDIR/data/atlases/JHU}"

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
warn() { printf '[%s] WARNING: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- config ----

# cfg <key> [default] -- read a scalar from config.json.  A missing key, an
# explicit JSON null, or an empty string all fall back to the default.
cfg() {
    local key="$1" default="${2-}" value
    value="$(jq -r --arg k "$key" 'if has($k) and (.[$k] != null) then .[$k] else "" end | tostring' "$CONFIG")"
    if [ -z "$value" ]; then printf '%s' "$default"; else printf '%s' "$value"; fi
}

# lower <string> -- lowercase, without ${x,,}, which is bash 4+ and so absent
# from the bash 3.2 that macOS still ships as /bin/bash.
lower() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

# cfg_bool <key> <default true|false> -- normalise to the strings true/false.
cfg_bool() {
    local value; value="$(cfg "$1" "$2")"
    case "$(lower "$value")" in
        1|true|yes|on)  printf 'true' ;;
        0|false|no|off|"") printf 'false' ;;
        *) die "config key '$1' should be a boolean, got '$value'" ;;
    esac
}

is_true() { [ "$1" = "true" ]; }

# cfg_input_meta <field> -- read a field from brainlife's input metadata.
#
# brainlife adds an `_inputs` array to config.json describing the datasets the
# task was given; `_inputs[0].meta.subject` is the documented way to recover the
# subject at run time (see brainlife's docs/cli/download.md). Only that one
# access is documented, so this searches every entry for the first non-empty
# value rather than assuming a position or that `meta` exists at all.
cfg_input_meta() {
    jq -r --arg f "$1" '
        [ (._inputs // [])[]?
          | (.meta? // {})
          | .[$f]?
          | select(. != null and . != "")
          | tostring ]
        | first // empty
    ' "$CONFIG" 2>/dev/null || true
}

# run_identifier -- a best-effort label tying results back to the run.
#
# brainlife documents no environment variable carrying the task id, so this does
# not invent one: it honours TASK_ID when something in the environment sets it,
# and otherwise records the name of the working directory. brainlife runs each
# task in its own directory, so in practice that is the task id, but the docs do
# not promise it -- hence the neutral column name `run_id`.
run_identifier() {
    if [ -n "${TASK_ID:-}" ]; then printf '%s' "$TASK_ID"; return 0; fi
    printf '%s' "$(basename "$PWD")"
}

# resolve_labels -- settle the subject, session and run labels, once.
#
# An explicit config key wins; otherwise take it from brainlife's input
# metadata. Without this, every task labels its results "subject", and a batch
# concatenates into one indistinguishable block. Stage 0 records the answer in
# state.sh so that every later stage -- the ROI tables, the QC database a group
# SQUAD run reads -- agrees on who this is.
resolve_labels() {
    SUBJECT="$(cfg subject "")"
    [ -z "$SUBJECT" ] && SUBJECT="$(cfg_input_meta subject)"
    [ -z "$SUBJECT" ] && SUBJECT="subject"

    SESSION="$(cfg session "")"
    [ -z "$SESSION" ] && SESSION="$(cfg_input_meta session)"

    # Optional: pull a session off the end of the subject label (sub01-MR03 ->
    # sub01 + MR03). Off by default -- an explicit session, or brainlife's input
    # metadata, is always preferred to guessing from a string.
    if [ -z "$SESSION" ] && is_true "$(cfg_bool split_subject_session false)"; then
        local split
        split="$(python3 "$APP_DIR/python/labels.py" --subject "$SUBJECT" --split \
                 --session-prefixes "$(cfg session_prefixes 'ses,MR,visit,tp,V')")"
        SUBJECT="${split%%$'\t'*}"
        SESSION="${split#*$'\t'}"
    fi

    RUN_ID="$(run_identifier)"
    log "labelling results: subject=$SUBJECT session=${SESSION:-<none>} run_id=$RUN_ID"
}

# cfg_list <key> <default, space separated> -- read a config value that may be
# either a JSON array (["FA","MD"]) or a delimited string ("FA,MD"), and echo
# it as a whitespace-separated list.
cfg_list() {
    local key="$1" default="${2-}" value
    value="$(jq -r --arg k "$key" '
        if (has($k) and (.[$k] != null)) then
            (if (.[$k] | type) == "array" then .[$k][] | tostring else .[$k] | tostring end)
        else empty end' "$CONFIG" | tr ',;' '  ')"
    value="$(tr -s '[:space:]' ' ' <<< "$value" | sed 's/^ *//; s/ *$//')"
    if [ -z "$value" ]; then printf '%s' "$default"; else printf '%s' "$value"; fi
}

# cfg_manual <key> -- a config value that is either an explicit setting or the
# string "auto" (the default), meaning "derive it from the sidecar".  Echoes the
# value, or nothing at all when it is unset or "auto", so callers can treat an
# empty result as "let the deriving code decide".
cfg_manual() {
    local value; value="$(cfg "$1" auto)"
    [ "$(lower "$value")" = auto ] && return 0
    printf '%s' "$value"
}

# cfg_path <key> -- a config value that must name an existing file.  Empty when
# the key is unset, so callers can treat inputs as optional.
cfg_path() {
    local value; value="$(cfg "$1" "")"
    [ -z "$value" ] && return 0
    # An absolute path that is missing is more often unmounted than mistyped:
    # the app runs in a container that sees only the working directory, the app
    # directory, and whatever EXTRA_BIND named. Say so, rather than sending
    # someone to hunt for a typo in a path that is right.
    if [ ! -e "$value" ]; then
        case "$value" in
            /*) die "input '$1' points at '$value', which this process cannot see. If it exists on the host, the container was not given it: pass EXTRA_BIND=$(dirname "$value") to ./main, and check that your container runtime shares that path (Docker Desktop needs it under Settings -> Resources -> File sharing; a symlink pointing outside the bound directory dangles inside the container)." ;;
            *)  die "input '$1' points at '$value', which does not exist" ;;
        esac
    fi
    printf '%s' "$(cd "$(dirname "$value")" && pwd)/$(basename "$value")"
}

require_path() {
    local value; value="$(cfg_path "$1")"
    [ -n "$value" ] || die "required input '$1' is missing from config.json"
    printf '%s' "$value"
}

# ----------------------------------------------------------------- tools ----

NTHREADS="${NTHREADS:-}"
setup_threads() {
    NTHREADS="$(cfg nthreads "")"
    if [ -z "$NTHREADS" ] || [ "$NTHREADS" = "0" ]; then
        NTHREADS="${OMP_NUM_THREADS:-$(nproc 2>/dev/null || echo 4)}"
    fi
    export NTHREADS
    export OMP_NUM_THREADS="$NTHREADS"
    export ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS="$NTHREADS"
    export MRTRIX_NTHREADS="$NTHREADS"
    export ANTS_RANDOM_SEED="${ANTS_RANDOM_SEED:-1}"
    log "using $NTHREADS threads"
}

check_tools() {
    local missing=()
    for tool in "$@"; do have "$tool" || missing+=("$tool"); done
    [ ${#missing[@]} -eq 0 ] || die "missing required tool(s): ${missing[*]}. Run the app through its container, or put FSL/MRtrix3/ANTs on PATH."
}

# cuda_eddy_binaries -- every eddy_cuda* executable on PATH, one per line.
#
# FSL has changed this name more than once: releases up to 6.0.7.19 carry a
# suffix tracking the CUDA toolkit (eddy_cuda9.1, eddy_cuda10.2, eddy_cuda11.0),
# and 6.0.7.20 dropped the suffix for a plain eddy_cuda.  Enumerating the names
# we happen to know means a release that invents a new one is silently missed,
# the search falls through to CPU eddy, and slice-to-volume correction quietly
# stops happening.  So discover what is installed instead, the way MRtrix3's own
# fsl.eddy_binary() does.
cuda_eddy_binaries() {
    local dir entry
    local IFS=:
    for dir in $PATH; do
        [ -d "$dir" ] || continue
        for entry in "$dir"/eddy_cuda*; do
            [ -x "$entry" ] && [ -f "$entry" ] && printf '%s\n' "${entry##*/}"
        done
    done | sort -u
}

# find_eddy -- the eddy binary to run, preferring a CUDA build so that
# slice-to-volume (--mporder) correction is available.  Honours the
# 'eddy_binary' config key, then $EDDY_BINARY, then a search.
find_eddy() {
    local requested; requested="$(cfg eddy_binary "${EDDY_BINARY:-}")"
    if [ -n "$requested" ]; then
        have "$requested" || die "eddy_binary '$requested' is not on PATH"
        printf '%s' "$requested"; return 0
    fi

    local candidates; candidates="$(cuda_eddy_binaries)"
    if [ -n "$candidates" ]; then
        # The unsuffixed name is the modern one, so prefer it; otherwise take
        # the highest CUDA version present.
        if printf '%s\n' "$candidates" | grep -qx 'eddy_cuda'; then
            printf 'eddy_cuda'; return 0
        fi
        printf '%s' "$(printf '%s\n' "$candidates" | sort -V | tail -1)"
        return 0
    fi

    local cpu
    if cpu="$(find_cpu_eddy)"; then printf '%s' "$cpu"; return 0; fi
    die "no eddy binary found on PATH"
}

# find_cpu_eddy -- a non-CUDA eddy, or a non-zero return when there is none.
#
# Ordering matters: in recent FSL, plain `eddy` is a wrapper that dispatches to
# a CUDA build when it can, so it is tried last. eddy_openmp and eddy_cpu are
# unambiguously CPU builds.
find_cpu_eddy() {
    local candidate
    for candidate in eddy_openmp eddy_cpu eddy; do
        if have "$candidate"; then printf '%s' "$candidate"; return 0; fi
    done
    return 1
}

is_cuda_eddy() { case "$1" in *cuda*) return 0 ;; *) return 1 ;; esac; }

# gpu_present -- true when a CUDA device is actually visible to this process.
gpu_present() {
    have nvidia-smi && nvidia-smi -L >/dev/null 2>&1
}

# bias_correct <in> <out> <bvecs> <bvals> <mask> <algorithm>
#
# MRtrix3 takes the algorithm as a positional argument: `dwibiascorrect ants ...`.
# The 3.0_RC3 spelling (`-ants`, a flag) is not supported: the container pins
# MRtrix3 3.0.8, and probing for the old syntax is unreliable because 3.0.8's
# no-argument usage error does not name the algorithms.
bias_correct() {
    local in="$1" out="$2" bvecs="$3" bvals="$4" mask="$5" algo="$6"
    dwibiascorrect "$algo" -force -nthreads "$NTHREADS" \
        -fslgrad "$bvecs" "$bvals" -mask "$mask" "$in" "$out"
}

# nvols <nifti> / nslices <nifti>
nvols()   { fslval "$1" dim4 | tr -d '[:space:]'; }
nslices() { fslval "$1" dim3 | tr -d '[:space:]'; }

timer_start() { STAGE_T0="$(date +%s)"; }
timer_report() {
    local elapsed=$(( $(date +%s) - STAGE_T0 ))
    log "$1 finished in $((elapsed / 60))m $((elapsed % 60))s"
}
