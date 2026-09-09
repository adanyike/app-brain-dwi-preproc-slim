# Changelog

## 1.0.0 (slim)

Slim packaging of `app-brain-dwi-preproc`. The pipeline, its pinned tool
versions and its outputs are identical to the full app; only the container
build differs.

- Two-stage build: the builder installs the full toolchain, then FSL is pruned
  and the ANTs and MRtrix3 programs this app runs are collected with their
  libraries. The runtime stage copies only those trees, so removed bytes never
  exist in a lower layer.
- `docker/prune-fsl.sh` — a documented blacklist of FSL subsystems this
  pipeline never touches: FSLeyes and the Qt6/VTK/Mesa stack beneath it, the
  conda C/C++ toolchain and LLVM/clang libraries the environment carries as
  build dependencies, OpenVINO, atlases, FIRST models, standard-space and
  Oxford-MM data, POSSUM, XTRACT, FIX macaque masks, sources, headers, docs,
  conda cache and metadata. matplotlib and pandas are deliberately kept:
  `eddy_quad` renders its report with matplotlib.
- `docker/collect-binaries.sh` — copies named programs plus, via `ldd`, exactly
  the libraries they need from inside their own prefix. A binary that is not
  found fails the build. Applied to ANTs only: 2.6 GB to 135 MB. MRtrix3 is
  copied whole, since trimming it saved 72 MB against the risk of a missing
  binary appearing only at run time.
- `docker/selftest.sh` — the final build step. CUDA binaries are checked by
  linkage rather than execution: CUDA base images deliberately omit
  `libcuda.so.1` (the container runtime injects it at `--gpus` / `--nv` time),
  so `eddy_cuda` cannot run during `docker build` and exits 127 with a loader
  error that says nothing about packaging. Everything else is executed and
  fails on a missing executable or a dynamic-linker error, which is what
  over-pruning actually produces; `command -v` cannot detect either. Also
  verifies topup's `b02b0.cnf` and a NIfTI round trip through FSL and MRtrix3.
- `test/test_parity.sh` — asserts every shared file is byte-identical to the
  full app, so the two cannot drift apart.
- A `/usr/local/bin/python -> python3` fallback symlink. MRtrix3's python
  drivers use `#!/usr/bin/env python` and Ubuntu 22.04 provides no `python`;
  FSL's bundled interpreter supplies one and still takes precedence, so this
  only matters if that interpreter is ever pruned or leaves the front of PATH.

The notes below describe the pipeline itself and apply to both apps.

## 1.0.0

First release. Ports the MATLAB-driven cluster pipeline
(`preproc_AP_PA_longitudinal_2019.m`, brain branch) to a containerised
brainlife app.

Added
- Seven-stage pipeline driven by `run.sh`, resumable with `--from` / `--only`.
- `acqparams`, `index` and the merged gradient table derived from BIDS sidecars.
- `slspec` derived from `SliceTiming`, verified against the reference Philips
  84-slice specification.
- Automatic eddy binary selection with a CPU fallback when no GPU is visible.
- Shell detection from the bvals, so `dtifit_shell` accepts `all`, `lowest`,
  `highest` or an explicit b-value instead of a hard-coded per-study literal.
  Naming a shell the data does not contain is refused up front, and the
  resolved b-value is what appears in the provenance.
- `eddy_quad` QC, motion plots and a `product.json` summary.
- Westin CL/CP/CS maps and `--save_tensor`, completing brainlife's
  `neuro/tensor` datatype.
- Per-ROI statistics with voxel counts and dispersion, in both tidy and legacy
  wide CSV layouts.
- `EXTRA_BIND` for local runs, so input data outside the working directory is
  visible inside the container; a `.sif` requested without singularity now gives
  a clear error instead of being handed to docker.
- Unit tests, static checks and a synthetic end-to-end smoke test.

Added
- `pe_dir`, `rpe_dir`, `readout_time` and `rreadout_time` default to `auto` and
  are derived per series, so a study does not need a hand-entered acqparams
  value per subject. The phase-encoding direction falls back to the Siemens CSA
  pair (`InPlanePhaseEncodingDirection` for the axis,
  `CsaImage.PhaseEncodingDirectionPositive` for the sign) when there is no
  `PhaseEncodingDirection`; the readout time falls back to
  1/`BandwidthPerPixelPhaseEncode` and then to `dcm2niix`'s `Estimated*` names.
  An explicit value still wins, which is what Philips data needs. Each run
  reports the source of both values per series, on stderr and in `prep.json`,
  and warns when the two series resolve their readout time through different
  fields.

Fixed
- Sidecar fields were only read from the top level of the JSON, so a raw DICOM
  parameter dump — which nests them under `global.const` / `time.samples` and
  repeats each one per volume — looked empty. `SliceTiming`,
  `CsaImage.MosaicRefAcqTimes`, `PhaseEncodingDirection`, `TotalReadoutTime`,
  `EffectiveEchoSpacing` and `ReconMatrixPE` are now found at any depth, and a
  per-volume repetition collapses to the one row the series has (see
  `python/sidecar.py`). Rows that genuinely disagree are refused rather than
  guessed at. The visible symptom was slice-to-volume correction being disabled
  on a dump that did carry the timings.
- The "no PhaseEncodingDirection in the sidecar" error told you to set
  `dwi_pe_dir`/`rdwi_pe_dir`, keys `00_prepare.sh` never reads. It now names
  the real ones, `pe_dir`/`rpe_dir` (likewise `readout_time`/`rreadout_time`),
  and all four are documented in the README's parameter table.
- `find_eddy` matched a hardcoded list of CUDA eddy names that omitted
  `eddy_cuda11.0`, the name FSL 6.0.7.18 and 6.0.7.19 ship. On those releases
  the search fell through to `eddy_openmp` and slice-to-volume correction was
  silently skipped. It now discovers whatever `eddy_cuda*` is installed,
  preferring the unsuffixed modern name and otherwise the highest version.

Fixed relative to the original scripts
- b=0 volumes are merged in a defined order rather than by a `bzero*` glob,
  which misordered ten or more volumes against `acqparams`.
- The post-eddy brain mask is hole-filled; the original `fslmaths -fillh` was
  appended to a command buffer that was never executed.

Changed
- MRtrix3 pinned to 3.0.8 (was 3.0.4), ANTs to 2.6.5 (was 2.5.3) and FSL to
  6.0.7.23 (was 6.0.7.16), all current releases. The ANTs unpack no longer
  assumes the archive's top-level directory is named after the version.
- The container build globs for an `eddy_cuda*` binary and now *fails* when
  there is none, rather than listing the eddy binaries and ignoring the result.
  FSL 6.0.7.20 shipped without `eddy_cuda`; an image built on it would fall
  back to CPU eddy and silently skip slice-to-volume correction. Build with
  `--build-arg REQUIRE_CUDA_EDDY=0` for a deliberately CPU-only image.
- The atlas is warped with `MultiLabel` interpolation instead of ANTs' linear
  default; set `atlas_interpolation: "Linear"` for the legacy behaviour.

Removed
- The final 1 mm isotropic upsampling of the DWI and its brain mask, along with
  the `upscale`, `vox_size` and `upscale_mask_threshold` settings. It produced a
  large interpolated volume that nothing downstream consumed, and `mrresize`,
  the command that performed it, no longer exists in MRtrix3.
