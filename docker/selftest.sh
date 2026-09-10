#!/bin/bash
# Prove the image is actually usable, not merely populated.
#
# Trimming an image breaks things quietly: a binary survives but its shared
# library was pruned, a wrapper script loses the helper it shells out to, a
# config file topup reads at run time is gone.  `command -v` sees none of that
# -- the file is still on PATH.  So every tool here is executed, and the two
# failure signatures that pruning actually produces are treated as fatal:
# a missing executable, and a dynamic linker error.
#
# Run at build time (the Dockerfile's last step) and any time afterwards:
#     docker run --rm <image> /opt/app/docker/selftest.sh
set -uo pipefail

FAILED=0
CHECKED=0

fail() { printf '  FAIL  %-28s %s\n' "$1" "$2" >&2; FAILED=$((FAILED + 1)); }
pass() { printf '  ok    %s\n' "$1"; }

# Execute a tool and judge the result.  Most neuroimaging programs exit
# non-zero when given no arguments and print usage; that is a pass.  What we
# refuse to accept is the program not being there, or not being able to load.
check_runs() {
    local tool="$1"; shift
    CHECKED=$((CHECKED + 1))

    if ! command -v "$tool" >/dev/null 2>&1; then
        fail "$tool" "not on PATH"
        return
    fi

    local output status
    output="$("$tool" "$@" 2>&1)"
    status=$?

    # Check the linker signature before the exit status: a missing shared
    # library also exits 127, and reporting that as "a helper is missing" sends
    # you hunting for the wrong thing.  Either way, quote what the tool actually
    # said rather than guessing at a cause.
    if grep -qiE 'error while loading shared libraries|cannot open shared object|symbol lookup error|undefined symbol' <<< "$output"; then
        fail "$tool" "$(grep -iEm1 'error while loading shared libraries|cannot open shared object|symbol lookup error|undefined symbol' <<< "$output")"
    elif [ "$status" -eq 127 ]; then
        fail "$tool" "exit 127: $(head -1 <<< "$output")"
    elif [ -z "$output" ] && [ "$status" -ne 0 ]; then
        fail "$tool" "exited $status with no output"
    else
        pass "$tool"
    fi
}

# The NVIDIA driver library is not in the image: CUDA base images deliberately
# omit libcuda.so.1 and the container runtime injects it at run time (--gpus /
# --nv).  So a CUDA binary cannot be executed during `docker build` -- it exits
# 127 with a loader error naming libcuda.  That is expected and says nothing
# about whether the image was trimmed correctly.
#
# Instead, ask the linker which of its libraries are missing and ignore the ones
# the driver supplies.  Anything else missing is a genuine packaging fault.
DRIVER_LIBS='^(libcuda\.so|libnvidia-.*\.so|libnvcuvid\.so)'

check_links() {  # check_links <tool>
    local tool="$1"
    CHECKED=$((CHECKED + 1))

    local path
    path="$(command -v "$tool" 2>/dev/null)" || { fail "$tool" "not on PATH"; return; }

    local unresolved
    unresolved="$(ldd "$path" 2>/dev/null | awk '/not found/ {print $1}' \
                  | grep -vE "$DRIVER_LIBS" || true)"

    if [ -n "$unresolved" ]; then
        fail "$tool" "missing libraries: $(tr '\n' ' ' <<< "$unresolved")"
        return
    fi

    # With a GPU present the driver library resolves, so the binary can also be
    # executed; without one, linkage is as far as we can check.
    if ldd "$path" 2>/dev/null | grep -q 'libcuda\.so.*=> /'; then
        pass "$tool (links clean; driver present)"
        check_runs "$tool"
    else
        pass "$tool (links clean; not executed -- no GPU driver at build time)"
    fi
}

check_file() {  # check_file <description> <path>
    CHECKED=$((CHECKED + 1))
    if [ -e "$2" ]; then pass "$1"; else fail "$1" "missing: $2"; fi
}

echo "FSL"
for tool in fslval fslroi fslmerge fslmaths bet topup dtifit; do
    check_runs "$tool"
done
# topup reads its configuration from $FSLDIR/etc/flirtsch at run time, so the
# binary being present is not enough.  b02b0_1.cnf is the app's default; the
# sub-sampling variants are there for anyone who sets topup_config.
for cnf in b02b0_1.cnf b02b0.cnf; do
    check_file "topup $cnf config" "${FSLDIR:-/opt/fsl}/etc/flirtsch/$cnf"
done

# Stage 4 registers the app's own JHU FA template to each subject and warps
# FSL's label image with the result, so one file comes from templates/ and the
# other has to survive the prune.
echo "JHU atlas"
FSL_ATLASES="${FSLDIR:-/opt/fsl}/data/atlases"
APP_DIR_SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_FA="$APP_DIR_SELF/templates/JHU-ICBM-FA-1mm.nii.gz"
FSL_LABELS="$FSL_ATLASES/JHU/JHU-ICBM-labels-1mm.nii.gz"
check_file "JHU FA template"  "$APP_FA"
check_file "JHU label image"  "$FSL_LABELS"
check_file "JHU label list"   "$FSL_ATLASES/JHU-labels.xml"

# The transform is estimated from the FA template and then applied to the label
# image, so the two have to sit on the same grid.  Nothing ties them together --
# one ships with this app, the other with whatever FSL release is installed --
# so check rather than assume.
CHECKED=$((CHECKED + 1))
grid_fa=""; grid_lab=""
for key in dim1 dim2 dim3 pixdim1 pixdim2 pixdim3 qform_xorient qform_yorient qform_zorient; do
    grid_fa="$grid_fa $(fslval "$APP_FA" "$key" 2>/dev/null | tr -d '[:space:]')"
    grid_lab="$grid_lab $(fslval "$FSL_LABELS" "$key" 2>/dev/null | tr -d '[:space:]')"
done
if [ -n "$(tr -d '[:space:]' <<< "$grid_fa")" ] && [ "$grid_fa" = "$grid_lab" ]; then
    pass "FA template and label image share a grid ($(tr -s ' ' <<< "$grid_fa" | cut -d' ' -f2-4 | tr ' ' 'x'))"
else
    fail "FA/label grid" "template is [$grid_fa ] but the label image is [$grid_lab ]"
fi

# The app's own label metadata drives every ROI table, so it has to agree with
# the label image it is read against.  FSL listed 48 regions before 6.0.5 and 50
# from 6.0.5 on; a mismatch here means every ROI past the divergence is reported
# under the wrong name.
CHECKED=$((CHECKED + 1))
APP_LABELS="$APP_DIR_SELF/templates/JHU-ICBM-labels.json"
if [ ! -f "$APP_LABELS" ] || [ ! -f "$FSL_ATLASES/JHU-labels.xml" ]; then
    fail "JHU label metadata" "cannot compare: $APP_LABELS or the FSL label list is missing"
else
    app_n="$(jq -r '.labels | length' "$APP_LABELS")"
    fsl_n="$(grep -c '<label ' "$FSL_ATLASES/JHU-labels.xml")"
    max_label="$(fslstats "$FSL_LABELS" -R \
                 | awk '{printf "%d", $2 + 0.5}')"
    if [ "$app_n" = "$fsl_n" ] && [ "$app_n" = "$max_label" ]; then
        pass "JHU label metadata ($app_n ROIs, matching FSL's label list and image)"
    else
        fail "JHU label metadata" \
             "templates/JHU-ICBM-labels.json has $app_n ROIs, JHU-labels.xml has $fsl_n, the label image goes up to $max_label"
    fi
fi

# eddy is named differently across FSL releases; at least one must work.
echo "eddy"
eddy_found=""
for candidate in $( { compgen -c eddy_cuda; echo eddy_openmp; echo eddy_cpu; echo eddy; } 2>/dev/null | sort -u); do
    if command -v "$candidate" >/dev/null 2>&1; then
        case "$candidate" in
            *cuda*) check_links "$candidate" ;;
            *)      check_runs  "$candidate" ;;
        esac
        eddy_found="$candidate"
    fi
done
CHECKED=$((CHECKED + 1))
if [ -n "$eddy_found" ]; then pass "an eddy binary exists"; else fail "eddy" "no eddy binary of any kind"; fi

echo "eddy QC (optional -- needs FSL's bundled python)"
if command -v eddy_quad >/dev/null 2>&1; then
    check_runs eddy_quad --help
    # --help does not import the plotting stack, so it passes happily on an
    # image whose QC would die at run time.  eddy_quad renders its report with
    # matplotlib; import what it needs, under FSL's own interpreter.
    CHECKED=$((CHECKED + 1))
    FSL_PYTHON="${FSLDIR:-/opt/fsl}/bin/python"
    if [ ! -x "$FSL_PYTHON" ]; then
        fail "eddy_quad dependencies" "FSL's python is missing at $FSL_PYTHON"
    else
        # Import for real rather than probing for a spec: a half-deleted
        # package still has a findable spec but blows up on import, which is
        # exactly what over-pruning leaves behind.
        missing_mods="$("$FSL_PYTHON" - <<'PYEOF' 2>/dev/null
missing = []
for module in ("matplotlib", "numpy", "nibabel"):
    try:
        __import__(module)
    except Exception:
        missing.append(module)
print(",".join(missing))
PYEOF
)"
        if [ -z "$missing_mods" ]; then
            pass "eddy_quad dependencies (matplotlib, numpy, nibabel)"
        else
            fail "eddy_quad dependencies" "FSL python cannot import: $missing_mods"
        fi
    fi
else
    echo "  skip  eddy_quad not installed; set eddy_qc=false in config.json"
fi

echo "MRtrix3"
for tool in mrinfo mrconvert mrcalc mrmath mrstats dwiextract dwidenoise mrdegibbs dwi2mask; do
    check_runs "$tool" -help
done
check_runs dwibiascorrect

echo "ANTs"
check_runs antsRegistration --version
check_runs antsApplyTransforms --version
check_runs N4BiasFieldCorrection --version
check_runs PrintHeader
check_runs antsRegistrationSyN.sh

echo "support"
check_runs jq --version
check_runs python3 --version
CHECKED=$((CHECKED + 1))
if python3 -c 'import numpy, nibabel' 2>/dev/null; then
    pass "python numpy + nibabel"
else
    fail "python numpy + nibabel" "import failed"
fi

# A real read/write cycle through both toolchains, which catches a broken
# NIfTI/zlib stack that --help would never touch.
echo "round trip"
CHECKED=$((CHECKED + 1))
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if python3 - "$TMP" <<'PY' >/dev/null 2>&1
import sys, numpy as np, nibabel as nib
nib.save(nib.Nifti1Image(np.random.rand(6, 6, 6, 3).astype(np.float32), np.eye(4)),
         sys.argv[1] + "/t.nii.gz")
PY
then
    if fslmaths "$TMP/t.nii.gz" -Tmean "$TMP/mean.nii.gz" 2>/dev/null \
       && [ "$(fslval "$TMP/mean.nii.gz" dim4 | tr -d '[:space:]')" = "1" ] \
       && mrconvert -quiet -force "$TMP/mean.nii.gz" "$TMP/mean.mif" 2>/dev/null; then
        pass "fslmaths -> fslval -> mrconvert round trip"
    else
        fail "round trip" "FSL/MRtrix could not read or write a NIfTI"
    fi
else
    fail "round trip" "could not write a test NIfTI"
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "selftest: all $CHECKED checks passed"
    exit 0
fi
echo "selftest: $FAILED of $CHECKED checks FAILED" >&2
exit 1
