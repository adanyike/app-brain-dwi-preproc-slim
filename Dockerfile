# Slim brain DWI preprocessing container: FSL (with CUDA eddy), MRtrix3, ANTs.
#
#   docker build -t <you>/brain-dwi-preproc-slim:1.0.0 .
#
# Same pipeline as ../brainlife, same pinned tool versions, built to be small.
# The full image installs three complete neuroimaging distributions; this one
# keeps only what the pipeline executes.
#
# Two stages. The builder installs everything exactly as the full image does,
# then prunes FSL and resolves the ANTs and MRtrix3 binaries this app calls,
# with their libraries, via ldd. The runtime stage starts from a clean base and
# copies in only those trees, so nothing deleted survives in a lower layer.
#
# The last build step runs docker/selftest.sh, which *executes* every required
# tool. That is deliberate: over-pruning does not remove a binary, it removes
# the library the binary loads, and `command -v` cannot see the difference.
#
# CUDA 11.8 / Ubuntu 22.04 is not a free choice -- see the comment in
# ../brainlife/Dockerfile for why those two are locked together.

# ========================================================== toolchain ========
# Everything expensive and slow-changing: FSL, ANTs, and MRtrix3 compiled from
# source. Nothing here depends on the scripts under docker/, so iterating on a
# prune rule never rebuilds it.
#
# It can also be built and tagged on its own, which survives `docker builder
# prune` (build cache does not) and makes later rebuilds take minutes:
#
#     docker build --target toolchain -t <you>/bdp-toolchain:1.0.0 .
#     docker build --build-arg TOOLCHAIN_IMAGE=<you>/bdp-toolchain:1.0.0 -t <you>/brain-dwi-preproc-slim:1.0.0 .
#
# With TOOLCHAIN_IMAGE set, this stage is unreferenced and BuildKit skips it
# entirely -- no FSL download, no compile.
# Declared here, before the first FROM: an ARG referenced by a FROM must be in
# the global scope. Declared inside a stage it is stage-scoped, and the next
# FROM sees it as blank ("base name should not be blank").
ARG TOOLCHAIN_IMAGE=toolchain

FROM nvidia/cuda:11.8.0-runtime-ubuntu22.04 AS toolchain

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    FSLDIR=/opt/fsl \
    MRTRIX_DIR=/opt/mrtrix3

RUN apt-get update && apt-get install -y --no-install-recommends \
        bc bzip2 ca-certificates curl dc file less libgomp1 libquadmath0 \
        python3 tar unzip wget xz-utils \
    && rm -rf /var/lib/apt/lists/*

ARG FSL_VERSION=6.0.7.23
RUN wget -q -O /tmp/fslinstaller.py \
        https://fsl.fmrib.ox.ac.uk/fsldownloads/fslconda/releases/fslinstaller.py \
 && python3 /tmp/fslinstaller.py -d ${FSLDIR} -V ${FSL_VERSION} --no_self_update \
 && rm -f /tmp/fslinstaller.py /root/.cache -rf

ENV PATH="${FSLDIR}/share/fsl/bin:${FSLDIR}/bin:${PATH}"

ARG ANTS_VERSION=2.6.5
RUN wget -q -O /tmp/ants.zip \
        https://github.com/ANTsX/ANTs/releases/download/v${ANTS_VERSION}/ants-${ANTS_VERSION}-ubuntu-22.04-X64-gcc.zip \
 && unzip -q /tmp/ants.zip -d /tmp/ants-extract \
 && mv "$(find /tmp/ants-extract -maxdepth 1 -mindepth 1 -type d | head -1)" /opt/ants \
 && rm -rf /tmp/ants.zip /tmp/ants-extract

ARG MRTRIX_VERSION=3.0.8
# MRtrix3's build honours NUMBER_OF_PROCESSORS and defaults to one job per core.
# Each Eigen-heavy translation unit wants 1-3 GB, so on a machine with more
# cores than spare memory that default thrashes and can get the BuildKit daemon
# OOM-killed -- which surfaces as "rpc error: EOF", not as a compiler error.
# Default to whichever is smaller: one job per core, or one job per 2 GB of RAM.
# Set MRTRIX_BUILD_JOBS to override.
ARG MRTRIX_BUILD_JOBS=""
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ git libeigen3-dev libfftw3-dev libpng-dev libtiff5-dev zlib1g-dev \
 && git clone --depth 1 --branch ${MRTRIX_VERSION} \
        https://github.com/MRtrix3/mrtrix3.git ${MRTRIX_DIR} \
 && cd ${MRTRIX_DIR} \
 && ./configure -nogui \
 && NUMBER_OF_PROCESSORS="${MRTRIX_BUILD_JOBS:-$(awk -v n="$(nproc)" '/^MemTotal:/ {j=int($2/2097152); if (j<1) j=1; if (j>n) j=n; print j}' /proc/meminfo)}" \
    ./build -persistent -nopaginate \
 && rm -rf ${MRTRIX_DIR}/.git ${MRTRIX_DIR}/tmp \
 && rm -rf /var/lib/apt/lists/*

# ============================================================== trim =========
# Cheap and frequently edited: prune FSL, then collect the ANTs and MRtrix3
# programs this app runs. Kept in its own stage so that changing a rule costs
# seconds rather than a rebuild of the toolchain above.
FROM ${TOOLCHAIN_IMAGE} AS trimmed

COPY docker/prune-fsl.sh /tmp/prune-fsl.sh
RUN bash /tmp/prune-fsl.sh

# ANTs ships ~200 executables and this app runs four of them:
# antsRegistrationSyN.sh (the wrapper), which shells out to antsRegistration,
# antsApplyTransforms and PrintHeader, plus N4BiasFieldCorrection for
# MRtrix's `dwibiascorrect ants`. Verified against the v2.6.5 script source.
COPY docker/collect-binaries.sh /tmp/collect-binaries.sh
RUN bash /tmp/collect-binaries.sh /opt/ants /opt/ants-slim \
        antsRegistrationSyN.sh antsRegistration antsApplyTransforms \
        PrintHeader N4BiasFieldCorrection

# MRtrix3 is NOT trimmed, deliberately. The whole built tree is 84 MB and a
# whitelist takes it to 12 MB -- 72 MB against an image measured in gigabytes,
# about 1% of what the trimming saves overall. It is not worth the risk: the
# binaries MRtrix3 needs are not all obvious from the pipeline, since its python
# drivers shell out to others (dwibiascorrect invokes mrconvert, mrcalc,
# dwiextract and dwi2mask; lib/mrtrix3 calls mrinfo for every image it inspects),
# and a command missed by the whitelist fails only at run time, part-way through
# somebody's job. ANTs is trimmed because there the same risk buys 2.5 GB.

# ============================================================ runtime ========
FROM nvidia/cuda:11.8.0-runtime-ubuntu22.04

LABEL org.opencontainers.image.title="app-brain-dwi-preproc-slim"
LABEL org.opencontainers.image.description="brainlife app (slim): brain DWI preprocessing, DTI fit and JHU ROI extraction"
LABEL org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    FSLDIR=/opt/fsl \
    ANTSPATH=/opt/ants/bin \
    MRTRIX_DIR=/opt/mrtrix3

# Runtime libraries only -- no compilers, no archivers, no download tools.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bc ca-certificates dc jq libgomp1 libquadmath0 \
        libfftw3-double3 libpng16-16 libtiff5 zlib1g \
        python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

COPY --from=trimmed /opt/fsl          /opt/fsl
COPY --from=trimmed /opt/ants-slim    /opt/ants
COPY --from=trimmed /opt/mrtrix3      /opt/mrtrix3

ENV PATH="${FSLDIR}/share/fsl/bin:${FSLDIR}/bin:${ANTSPATH}:${MRTRIX_DIR}/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/ants/lib:${MRTRIX_DIR}/lib:${LD_LIBRARY_PATH:-}" \
    FSLOUTPUTTYPE=NIFTI_GZ \
    FSLMULTIFILEQUIT=TRUE

RUN pip3 install --no-cache-dir --break-system-packages "numpy>=1.24" "nibabel>=5.1" \
    || pip3 install --no-cache-dir "numpy>=1.24" "nibabel>=5.1"

# eddy_squad's update step merges the study-wise pages into each subject's own
# report with PyPDF2, and FSL does not ship it. It goes into FSL's own
# interpreter, not the system one, because that is what eddy_squad runs under.
# Pinned below 3.0: the code uses PdfFileMerger / PdfFileReader / PdfFileWriter,
# which 3.0 removed.
#
# Not fatal. Everything except `update_single_subject_reports` works without it,
# and an image built on a network that cannot reach PyPDF2 should still be a
# usable image -- run_squad.sh checks at run time and skips just that step.
#
# Three ways in, because a pruned FSL may have no working pip of its own: its
# own pip, pip bootstrapped with ensurepip, and failing both, the system pip
# installing *into* FSL's site-packages. The install is verified by importing
# it under FSL's interpreter -- `pip install` reporting success into the wrong
# environment is the failure this is guarding against.
RUN { FSL_PY="${FSLDIR}/bin/python"; \
      if ! "$FSL_PY" -m pip --version >/dev/null 2>&1; then \
          "$FSL_PY" -m ensurepip --default-pip >/dev/null 2>&1; \
      fi; \
      if "$FSL_PY" -m pip --version >/dev/null 2>&1; then \
          "$FSL_PY" -m pip install --no-cache-dir "PyPDF2<3"; \
      else \
          site="$("$FSL_PY" -c 'import site; print(site.getsitepackages()[0])')"; \
          echo "FSL's python has no pip; installing PyPDF2 into $site with the system pip"; \
          pip3 install --no-cache-dir --break-system-packages --target "$site" "PyPDF2<3" \
              || pip3 install --no-cache-dir --target "$site" "PyPDF2<3"; \
      fi; \
      "$FSL_PY" -c 'import PyPDF2; print("PyPDF2 " + PyPDF2.__version__ + " importable by FSL python")'; \
    } || echo "WARNING: PyPDF2 is not installed; eddy_squad cannot update single-subject reports" >&2

# FSL 6.0.7.x cannot update single-subject QC reports at all: SQUAD's
# squad_update calls utils/ref_page.py's main(pdf, data, ec) with an empty list,
# and ref_page then calls ec.MethodsText() -- so `eddy_squad --update` dies with
# "AttributeError: 'list' object has no attribute 'MethodsText'" on every run,
# whatever the data, *after* writing the group database.
#
# Guard the two call sites instead of supplying a methods object: at group level
# there is no single eddy command, so there is nothing truthful to put there.
# What the patched report loses is one paragraph of descriptive prose on one
# page; MethodsText() cannot move a QC number.
#
# Conditional, so a release that fixes this upstream is left alone; reversible,
# with the original kept beside it; and verified by importing the module, so a
# mangled file fails this build rather than somebody's task.
RUN { FSL_PY="${FSLDIR}/bin/python"; \
      REF="$("$FSL_PY" -c 'import eddy_qc.utils.ref_page as m; print(m.__file__)')"; \
      if ! grep -q 'ec\.MethodsText()' "$REF"; then \
          echo "ref_page.py does not call ec.MethodsText() unguarded; not patching"; \
      elif grep -q 'hasattr(ec, "MethodsText")' "$REF"; then \
          echo "ref_page.py is already patched"; \
      else \
          cp -p "$REF" "$REF.orig"; \
          sed -i 's/ec\.MethodsText()/(ec.MethodsText() if hasattr(ec, "MethodsText") else "Methods text is not available when eddy_squad updates a single-subject report.")/g' "$REF"; \
          "$FSL_PY" -c 'import eddy_qc.utils.ref_page'; \
          echo "patched $REF for the eddy_squad --update bug (original kept as $REF.orig)"; \
      fi; \
    } || echo "WARNING: could not patch ref_page.py; eddy_squad --update will fail and the group report will be produced without it" >&2

# MRtrix3's python drivers (dwibiascorrect and friends) start with
# `#!/usr/bin/env python`, and Ubuntu 22.04 provides no `python` at all -- only
# python3.  In this image they resolve to FSL's bundled conda interpreter,
# because $FSLDIR/bin is prepended to PATH ahead of everything else, and
# MRtrix3 3.0.8 runs fine there (its distutils/pipes imports are guarded
# fallbacks for Python 2.7; modern Python takes the shutil/shlex branch).
#
# This symlink does NOT change that resolution -- FSL's python still wins.  It
# is a fallback, so the scripts keep working if FSL's interpreter is ever
# pruned, relocated, or drops off the front of PATH.
RUN ln -sf /usr/bin/python3 /usr/local/bin/python

# Slice-to-volume correction is CUDA-only and FSL has both renamed and once
# omitted this binary, so glob rather than match known names. See
# ../brainlife/Dockerfile for the full reasoning.
ARG REQUIRE_CUDA_EDDY=1
RUN set -e; \
    found=""; \
    for dir in $(echo "$PATH" | tr ':' ' '); do \
        [ -d "$dir" ] || continue; \
        for entry in "$dir"/eddy_cuda*; do \
            if [ -x "$entry" ] && [ -f "$entry" ]; then found="${entry##*/}"; fi; \
        done; \
    done; \
    if [ -n "$found" ]; then \
        echo "CUDA eddy available: $found"; \
    elif [ "${REQUIRE_CUDA_EDDY}" = "1" ]; then \
        echo "ERROR: this FSL build ships no eddy_cuda binary." >&2; \
        echo "       FSL 6.0.7.20 is known to be missing it -- use 6.0.7.21 or newer." >&2; \
        echo "       For a deliberately CPU-only image: --build-arg REQUIRE_CUDA_EDDY=0." >&2; \
        exit 1; \
    else \
        echo "WARNING: no eddy_cuda; this image cannot do slice-to-volume correction." >&2; \
    fi

# The app itself, so selftest.sh can be re-run from the published image.
COPY . /opt/app
WORKDIR /opt/app

# Nothing below this line: if the trimming broke anything, the build stops here
# rather than in somebody's job.
RUN bash docker/selftest.sh && du -sh /opt/fsl /opt/ants /opt/mrtrix3

WORKDIR /
CMD ["/bin/bash"]
