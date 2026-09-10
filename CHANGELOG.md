# Changelog

## 1.0.0

First release: a containerised brainlife app for brain DWI preprocessing with
reverse phase-encode distortion correction, DTI fitting and JHU ROI extraction.

### The pipeline

- Seven stages driven by `run.sh`, resumable with `--from` / `--only`.
- `acqparams`, `index` and the merged gradient table are derived from the BIDS
  sidecars. `pe_dir`, `rpe_dir`, `readout_time` and `rreadout_time` default to
  `auto` and are resolved per series, falling back through
  `EffectiveEchoSpacing`, the Siemens CSA pair and `dcm2niix`'s `Estimated*`
  names; an explicit value always wins, which is what Philips data needs. The
  source used for each is reported on stderr and in `prep.json`.
- Sidecar fields are found at any depth, so a raw DICOM parameter dump — which
  nests them under `global.const` / `time.samples` and repeats each one per
  volume — works as well as a flat BIDS sidecar. Rows that genuinely disagree
  are refused rather than guessed at.
- `slspec` is derived from `SliceTiming` (or Siemens `MosaicRefAcqTimes`) by
  grouping slices on acquisition time, so the multiband factor is measured
  rather than assumed and a non-uniform grouping is an error instead of a
  silently wrong file. A reference Philips 84-slice specification ships in
  `templates/`.
- Every acquired slice is kept, whatever the slice count. topup defaults to
  `b02b0_1.cnf`, which does not sub-sample and so places no constraint on the
  matrix size; sub-sampling affects only topup's speed, and `topup_config`
  selects a faster schedule where the dimensions allow it. Cropping a slice to
  make the count even is not done, following FSL's guidance that it destroys
  the multiband structure `eddy` needs for slice-to-volume correction.
- `eddy` selects a CUDA build when one is installed, whatever it is called, and
  falls back to a CPU build — without slice-to-volume correction — when there is
  none. Which happened is recorded in the summary.
- The shell to fit is detected from the bvals, so `dtifit_shell` accepts `all`,
  `lowest`, `highest` or an explicit b-value instead of a hard-coded literal.
  Naming a shell the data does not contain is refused up front, and the resolved
  b-value appears in the provenance.
- Westin CL/CP/CS maps and `--save_tensor`, completing brainlife's
  `neuro/tensor` datatype.
- The JHU ICBM-DTI-81 atlas is warped into each subject's native diffusion space
  with `MultiLabel` interpolation, and per-ROI statistics are written with voxel
  counts and dispersion, in both tidy and wide CSV layouts.
- `eddy_quad` QC, motion plots and a `product.json` summary.
- Tool versions: FSL 6.0.7.23, MRtrix3 3.0.8, ANTs 2.6.5.

### The atlas

- The FA template and label image are read from FSL's own installation
  (`$FSLDIR/data/atlases/JHU`) rather than carried in this repository;
  `template_fa` and `atlas` override them.
- All 50 JHU ICBM-DTI-81 regions are reported. FSL's label list omitted the
  inferior fronto-occipital fasciculus before 6.0.5, and a 48-entry list read
  against the 50-label image reports every region from 45 upwards under the
  wrong name.
- `docker/selftest.sh` fails the build when `templates/JHU-ICBM-labels.json`,
  FSL's `JHU-labels.xml` and the label image do not agree on how many regions
  there are.

### The container

- Two-stage build: the builder installs the full toolchain, then FSL is pruned
  and the ANTs and MRtrix3 programs this app runs are collected with their
  libraries. The runtime stage copies only those trees, so removed bytes never
  exist in a lower layer.
- `docker/prune-fsl.sh` — a documented blacklist of FSL subsystems this pipeline
  never touches: FSLeyes and the Qt6/VTK/Mesa stack beneath it, the conda C/C++
  toolchain and LLVM/clang libraries, OpenVINO, FIRST models, standard-space and
  Oxford-MM data, POSSUM, XTRACT, FIX macaque masks, sources, headers, docs,
  conda cache and metadata, and every atlas but the JHU files stage 4 reads.
  matplotlib and pandas are deliberately kept: `eddy_quad` renders its report
  with matplotlib.
- `docker/collect-binaries.sh` — copies named programs plus, via `ldd`, exactly
  the libraries they need from inside their own prefix. A binary that is not
  found fails the build. Applied to ANTs only: 2.6 GB to 135 MB. MRtrix3 is
  copied whole, since trimming it saved 72 MB against the risk of a missing
  binary appearing only at run time.
- `docker/selftest.sh` — the final build step. CUDA binaries are checked by
  linkage rather than execution, because CUDA base images omit `libcuda.so.1`
  and the container runtime injects it at `--gpus` / `--nv` time. Everything
  else is executed and fails on a missing executable or a dynamic-linker error,
  which is what over-pruning actually produces and what `command -v` cannot
  detect. Also verifies topup's config files, the JHU atlas files, and a NIfTI
  round trip through FSL and MRtrix3.
- The build fails when the FSL release ships no `eddy_cuda` binary, rather than
  producing an image that silently skips slice-to-volume correction. Build with
  `--build-arg REQUIRE_CUDA_EDDY=0` for a deliberately CPU-only image.
- A `/usr/local/bin/python -> python3` fallback symlink, since MRtrix3's python
  drivers use `#!/usr/bin/env python` and Ubuntu 22.04 provides no `python`.

### Running it

- `main` selects Singularity, Docker or a local toolchain and passes a GPU
  through when one is present. `EXTRA_BIND` makes input data outside the working
  directory visible inside the container, and a `.sif` requested without
  singularity gives a clear error instead of being handed to docker.
- Unit tests, static checks, a dry run of every stage against a stub toolchain,
  and a synthetic end-to-end smoke test.
