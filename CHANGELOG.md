# Changelog

## Unreleased

### Group quality control (eddy SQUAD)

- A second brainlife App, sharing this repository and container: `run_squad.sh`
  runs FSL's `eddy_squad` over the eddy QC databases of a whole study, and
  `main` routes a task to it when its config carries the group input (`eddyqc`,
  `qc_folders`) or sets `mode: "group"`. The per-subject pipeline is unchanged.
- Each subject now publishes `output/eddyqc/` — `qc.json`, `qc.pdf` and a
  `squad_ready.json` carrying the subject label and a **cohort signature**. The
  existing `qc/` bundle is untouched; the new one exists because a group task
  stages every subject it is given, and staging hundreds of mean-b0 volumes to
  read a 2 KB database is not worth doing.
- The signature compares the eddy **input** data as well as the output flags,
  exactly. Newer FSL releases check the acquisition too -- `Inconsistency
  detected in eddy input data in topup acquisition parameters!` is what a real
  study hit -- so a key looser than that comparison pools subjects SQUAD then
  rejects, failing the whole study instead of splitting one cohort. Rounding is
  gone with it: b-values of 1495 and 1500 are different cohorts, because they
  are different to SQUAD. `signature_fields` narrows the comparison where an FSL
  tolerates a difference, and a refusal this app did not predict is followed by
  a field-by-field comparison of the staged subjects rather than a traceback.
- The signature is what makes a group run predictable. `eddy_squad` pools
  subjects only when `eddy` ran with the same features for all of them, and
  raises `Eddy output inconsistency detected!` otherwise — which on brainlife is
  easy to trip, because slice-to-volume correction depends on a GPU being
  visible and the susceptibility field on the subject having a reverse
  phase-encoded series. The signature records those six flags plus the shell
  structure, so the group App buckets its inputs, reports on the largest cohort,
  and names who it left out and why. Volume counts are deliberately left out of
  the signature: SQUAD pools those correctly, and excluding them would discard
  data for nothing.
- The grouping variable is matched to subjects **by name** when given as a
  `participants.tsv`-style table, because `eddy_squad` matches by line position
  and a single excluded subject otherwise shifts every later value onto the
  wrong person. A file already in SQUAD's format is passed through, and refused
  when its value count does not match the cohort.
- `--update` is supported: the QC databases are copied into the work directory
  first, since brainlife stages inputs read-only and `eddy_squad -u` writes into
  the folders it is given, and the rewritten reports are collected under each
  subject's label. It needs two things the group report does not -- every
  subject's own `qc.pdf`, and a PDF library in FSL's python to merge with -- so
  both are checked first and the update alone is skipped when either is absent.
  The dependency is checked by importing `eddy_qc.SQUAD.squad_update` itself
  rather than by guessing the library's name, which has changed between
  releases, and the Dockerfile installs `PyPDF2<3` into FSL's interpreter (not
  the system one, which is not what eddy_squad runs under).
- `product.json` for a group task reports the cohort, the exclusions and the
  per-subject motion, outlier and SNR distributions, worst subject first.
- A crash in `eddy_squad`'s update step no longer costs the group report. FSL
  6.0.7.x fails there every time -- `squad_update` hands `ref_page` an empty
  list where the eddy parameters belong -- and it fails *after* writing the
  group database, so the study-wise report exists when the traceback appears.
  The App detects that, re-runs without `--update`, and says so.
- Subjects processed before any of this existed need no reprocessing: a QUAD
  folder is found under `eddyqc/`, an archived `qc` dataset, an older task's
  `output/qc/eddy_quad/`, or eddy_quad's own `.qc/`, and a subject label is
  taken from the first directory in the path that names something other than a
  kind of output.
- Subject and session labels are now settled once, in stage 0, and carried in
  `state.sh`; stage 6 needs them for the QC dataset and stage 5 no longer
  resolves them separately.
- `docker/selftest.sh` checks `eddy_squad` and the seaborn import its study-wise
  plots need, which `eddy_quad` never touches.

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
  silently wrong file. A supplied `slspec` is checked against the acquisition
  the same way — wrong slice count, out-of-range or repeated indices, or ragged
  rows are refused rather than passed to `eddy`. An 84-slice, multiband-4
  specification ships in `templates/` as a worked example of the format.
- Where a sidecar carries no timings at all — some Philips exports — the
  excitation order can be declared with `slice_order` (`ascending`,
  `descending`, `interleaved`, `rev_interleaved`, `philips_default`, `step`)
  plus `multiband`, `slice_packages` and `slice_step`, and the slspec built
  from it. It is opt-in because it asserts the acquisition rather than
  measuring it, and where `SliceTiming` is present the declaration is compared
  against it and a disagreement stops the run.
- `acqp` and `index` accept a hand-made `acqparams.txt` / `index.txt` in place
  of the derived ones, for datasets whose sidecars are incomplete.
- The topup configuration is chosen from the matrix size, since topup requires
  the image size to be a multiple of each sub-sampling level in its config:
  `b02b0_4.cnf` when every dimension divides by 4, `b02b0_2.cnf` when they
  divide by 2, `b02b0_1.cnf` otherwise. Sub-sampling affects only speed, so
  this takes the fastest schedule the data allows; an explicit `topup_config`
  still wins, and the resolved value is recorded in `product.json`.
- Every acquired slice is kept, whatever the slice count: an odd dimension
  simply selects `b02b0_1.cnf`. Cropping a slice to make the count even is not
  done, following FSL's guidance that it destroys the multiband structure
  `eddy` needs for slice-to-volume correction.
- `eddy` selects a CUDA build when one is installed, whatever it is called, and
  falls back to a CPU build — without slice-to-volume correction — when there is
  none. Which happened is recorded in the summary. The slice specification is
  passed whenever one exists, not only for slice-to-volume correction, because
  group-wise outlier replacement needs the multiband structure and runs on CPU
  too; without a slice specification `--ol_type` degrades to `sw` rather than
  being handed to `eddy` as an invalid combination.
- The gradient directions `eddy` writes back are cleaned before anything reads
  them. An unweighted volume is recorded as `0 0 0` with a b-value that is
  often small but non-zero, and rotating then renormalising that vector yields
  NaN, which MRtrix refuses outright — so `0 0 0` is restored for volumes at or
  below `b0_threshold`, and a non-finite direction on a diffusion-weighted
  volume is refused rather than repaired.
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

- The FA template stage 4 registers to is the repository's own
  (`templates/JHU-ICBM-FA-1mm.nii.gz`), so the registration target does not vary
  with the FSL release or with how the app is launched. The label image it warps
  is FSL's, read from `$FSLDIR/data/atlases/JHU`. `template_fa` and `atlas`
  override either.
- All 50 JHU ICBM-DTI-81 regions are reported. FSL's label list omitted the
  inferior fronto-occipital fasciculus before 6.0.5, and a 48-entry list read
  against the 50-label image reports every region from 45 upwards under the
  wrong name.
- `docker/selftest.sh` fails the build when `templates/JHU-ICBM-labels.json`,
  FSL's `JHU-labels.xml` and the label image do not agree on how many regions
  there are, or when the FA template and the label image do not share a grid —
  the transform is estimated from one and applied to the other, and nothing else
  ties the two files together.

### The container

- Two-stage build: the builder installs the full toolchain, then FSL is pruned
  and the ANTs and MRtrix3 programs this app runs are collected with their
  libraries. The runtime stage copies only those trees, so removed bytes never
  exist in a lower layer.
- `docker/prune-fsl.sh` — a documented blacklist of FSL subsystems this pipeline
  never touches: FSLeyes and the Qt6/VTK/Mesa stack beneath it, the conda C/C++
  toolchain and LLVM/clang libraries, OpenVINO, FIRST models, standard-space and
  Oxford-MM data, POSSUM, XTRACT, FIX macaque masks, sources, headers, docs,
  conda cache and metadata, and every atlas but the JHU label image and label
  list stage 4 reads — FSL's own JHU FA template included, since the app
  registers to its own copy. matplotlib and pandas are deliberately kept:
  `eddy_quad` renders its report with matplotlib.
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
