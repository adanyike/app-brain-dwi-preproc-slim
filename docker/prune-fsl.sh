#!/bin/bash
# Remove the parts of an FSL installation this app never touches.
#
# fslinstaller.py installs the whole distribution -- FSLeyes and its GUI stack,
# every atlas, the FIRST shape models, FEAT, MELODIC, possum, xtract data.  This
# pipeline uses ten FSL programs: fslval, fslroi, fslmerge, fslmaths, bet,
# topup, eddy (CUDA or CPU), dtifit, eddy_quad and -- for the group QC App that
# shares this image -- eddy_squad.
#
# This is a blacklist, not a whitelist, and deliberately so.  Deleting a
# named subsystem that turns out to be needed is caught immediately by
# docker/selftest.sh; a whitelist that misses a shell wrapper's helper fails
# later, inside somebody's job.  If a deletion below turns out to matter, the
# fix is to remove that line -- the cost is a bigger image, not a broken one.
set -euo pipefail

FSLDIR="${FSLDIR:?FSLDIR must be set}"
[ -d "$FSLDIR" ] || { echo "prune-fsl: $FSLDIR is not a directory" >&2; exit 1; }

before="$(du -sm "$FSLDIR" 2>/dev/null | cut -f1)"

drop() {  # drop <reason> <path>...
    local reason="$1"; shift
    for path in "$@"; do
        if [ -e "$path" ]; then
            local size; size="$(du -sm "$path" 2>/dev/null | cut -f1)"
            printf '  - %-46s %6s MB  (%s)\n' "${path#"$FSLDIR"/}" "${size:-?}" "$reason"
            rm -rf "$path"
        fi
    done
}

echo "prune-fsl: starting at ${before:-?} MB"

# Conda package cache: tarballs already unpacked into the environment.
drop "conda package cache" "$FSLDIR/pkgs"

# Reference data. Stage 4 warps FSL's JHU ICBM-DTI-81 label image, so that file
# and the label list beside it are kept; every other atlas goes, FSL's own JHU FA
# template included -- the app registers to its own copy under templates/.
KEEP_ATLAS_FILES="JHU/JHU-ICBM-labels-1mm.nii.gz JHU-labels.xml"
keep_jhu() {
    local atlases="$FSLDIR/data/atlases" kept="$FSLDIR/data/atlases.keep" rel
    [ -d "$atlases" ] || return 0
    for rel in $KEEP_ATLAS_FILES; do
        [ -f "$atlases/$rel" ] || { echo "prune-fsl: $atlases/$rel is missing" >&2; exit 1; }
        mkdir -p "$kept/$(dirname "$rel")"
        cp -p "$atlases/$rel" "$kept/$rel"
    done
    drop "atlases (JHU labels and label list kept)" "$atlases"
    mv "$kept" "$atlases"
    for rel in $KEEP_ATLAS_FILES; do printf '  + kept %s\n' "data/atlases/$rel"; done
}
keep_jhu

drop "FIRST shape models" "$FSLDIR/data/first"
drop "standard spaces"    "$FSLDIR/data/standard"
drop "POSSUM simulator"   "$FSLDIR/data/possum"
drop "XTRACT protocols"   "$FSLDIR/data/xtract_data"
drop "MIST models"        "$FSLDIR/data/mist"

# Sources, headers and documentation.
drop "sources"        "$FSLDIR/src"
drop "headers"        "$FSLDIR/include"
drop "documentation"  "$FSLDIR/doc" "$FSLDIR/share/doc" "$FSLDIR/man" "$FSLDIR/share/man"
drop "refdoc"         "$FSLDIR/refdoc"

# FSLeyes is a wxPython desktop viewer. There is no display in a batch
# container, and nothing in this pipeline launches it.
#
# matplotlib, pandas and seaborn are deliberately NOT in this list: eddy_quad
# renders its QC report with matplotlib and eddy_squad draws its study-wise
# plots with seaborn, while neither --help imports them, so deleting them
# produces an image whose QC fails only at run time.
drop "FSLeyes viewer" "$FSLDIR/bin/fsleyes" "$FSLDIR/bin/fsleyes_"*
for pkg in fsleyes fsleyes_props fsleyes_widgets wx wxPython PyQt5 PySide2 \
           notebook jupyter jupyterlab IPython ipython sphinx; do
    drop "GUI/notebook stack" "$FSLDIR"/lib/python*/site-packages/"$pkg" \
                              "$FSLDIR"/lib/python*/site-packages/"$pkg"-*
done

# The conda environment FSL ships carries a complete C/C++ toolchain, pulled in
# as a build dependency of other packages. Nothing is compiled at run time.
drop "conda compiler sysroot" "$FSLDIR/x86_64-conda-linux-gnu"
drop "gcc internals"          "$FSLDIR/libexec/gcc" "$FSLDIR/lib/gcc"
drop "LLVM/clang libraries"   "$FSLDIR"/lib/libLLVM*.so* "$FSLDIR"/lib/libclang*.so*

# Native GUI stack underneath FSLeyes. Removing the python packages above left
# the Qt, VTK and Mesa libraries they bind to.
drop "Qt6"            "$FSLDIR/lib/qt6" "$FSLDIR"/lib/libQt6*.so*
drop "VTK"            "$FSLDIR"/lib/libvtk*.so*
drop "Mesa DRI"       "$FSLDIR/lib/dri" "$FSLDIR"/lib/libGL*.so* "$FSLDIR"/lib/libEGL*.so*
drop "fonts and tcl"  "$FSLDIR/fonts" "$FSLDIR/tcl"

# OpenVINO is an Intel inference runtime, pulled in as a dependency. None of the
# nine FSL programs this pipeline runs touches it.
drop "OpenVINO runtime" "$FSLDIR"/lib/openvino-*

# Remaining reference data: the Oxford-MM warps and the FIX macaque masks are
# never read.
drop "Oxford-MM template"  "$FSLDIR/data/omm"
drop "FIX macaque masks"   "$FSLDIR"/lib/python*/site-packages/pyfix/resources

# Conda package metadata: needed to install or update packages inside the
# environment, which this image never does.
drop "conda metadata" "$FSLDIR/conda-meta"

# Test suites shipped inside installed python packages.
find "$FSLDIR" -type d -name tests -path '*/site-packages/*' -prune \
     -exec rm -rf {} + 2>/dev/null || true

# Static archives and CMake glue are build-time only.
find "$FSLDIR" -type f \( -name '*.a' -o -name '*.la' -o -name '*.pyc' \) \
     -delete 2>/dev/null || true
find "$FSLDIR" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

after="$(du -sm "$FSLDIR" 2>/dev/null | cut -f1)"
echo "prune-fsl: ${before:-?} MB -> ${after:-?} MB"
