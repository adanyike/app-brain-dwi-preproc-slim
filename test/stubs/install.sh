#!/bin/bash
# Populate a directory with symlinks that make _stub.py answer to every command
# name the pipeline calls.  Put that directory first on PATH to dry-run the app
# without FSL, MRtrix3 or ANTs installed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${1:?usage: install.sh <bindir>}"
mkdir -p "$BIN"

for name in fslval fslroi fslmerge fslmaths bet topup dtifit eddy_quad \
            eddy_openmp eddy_cpu eddy eddy_cuda10.2 dwidenoise mrdegibbs dwibiascorrect \
            dwiextract mrcalc \
            antsRegistrationSyN.sh antsApplyTransforms; do
    ln -sf "$HERE/_stub.py" "$BIN/$name"
done
# A fake nvidia-smi lets the dry run exercise the CUDA branch of find_eddy.
if [ "${WITH_FAKE_GPU:-0}" = "1" ]; then
    printf '#!/bin/sh\necho "GPU 0: stub"\n' > "$BIN/nvidia-smi"
    chmod +x "$BIN/nvidia-smi"
fi

echo "installed stub toolchain into $BIN"
