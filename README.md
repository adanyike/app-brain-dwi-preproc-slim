# app-brain-dwi-preproc

[![brainlife.io](https://img.shields.io/badge/brainlife.io-app-blue.svg)](https://brainlife.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Preprocessing for brain diffusion MRI acquired with **reverse phase-encoding**,
followed by diffusion tensor fitting and white-matter ROI extraction.

The app corrects susceptibility distortion from an opposing phase-encode pair,
corrects eddy currents and subject motion — including **within-volume
(slice-to-volume) motion** where a GPU is available — fits the diffusion tensor,
and reports mean FA, MD, AD and RD in the 50 white-matter regions of the JHU
ICBM-DTI-81 atlas, in each subject's own diffusion space.

## What it does

| Stage | Step | Tools |
|---|---|---|
| 0 | Merge the phase-encode pair; denoise; remove Gibbs ringing; derive the acquisition parameters, volume index and slice-timing specification from the BIDS sidecars | MRtrix3 |
| 1 | Estimate the susceptibility field from opposing-phase b=0 volumes; brain-extract | FSL `topup`, `bet` |
| 2 | Eddy-current, motion and slice-to-volume correction with outlier replacement, applying the field | FSL `eddy_cuda` |
| 3 | B1 bias-field correction; detect the b-value shells and fit the tensor to one; derive RD, AD, colour FA and Westin shape measures | MRtrix3, ANTs, FSL `dtifit` |
| 4 | Register the JHU FA template to each subject's FA and warp the atlas labels into native space | ANTs |
| 5 | Mean, SD and median of each metric in each of the 50 ROIs | — |

All stages run as a single task. Quality-control output from `eddy_quad` and a
summary with per-volume motion and per-ROI FA are produced alongside the results.

Three details worth knowing, because they are derived rather than assumed:

* **The acquisition parameters are read from the sidecars.** Phase-encoding
  direction and total readout time are taken from `PhaseEncodingDirection` and
  `TotalReadoutTime`, falling back through `EffectiveEchoSpacing`,
  Siemens `BandwidthPerPixelPhaseEncode`, and the estimated variants. Nothing
  has to be prepared by hand, and the source used is recorded in the output.
* **The slice specification is derived from `SliceTiming`** (or Siemens
  `MosaicRefAcqTimes`). Slices are grouped by acquisition time, so the multiband
  factor is measured rather than assumed; a non-uniform grouping is reported as
  an error instead of producing a silently wrong file.
* **The topup configuration is chosen from the matrix size.** topup requires the
  image size to be a multiple of each sub-sampling level in its config, so the
  app reads the dimensions and picks the fastest one they allow: `b02b0_4.cnf`
  when every dimension divides by 4, `b02b0_2.cnf` when they divide by 2, and
  `b02b0_1.cnf` otherwise. Sub-sampling only affects speed — FSL states the
  results are very close to identical — and the resolved config is recorded in
  `product.json`.

* **The brain mask is checked against the image, not trusted.** `bet` sometimes
  returns a mask with a bite out of it or one that stops short of the temporal
  lobes, and nothing downstream notices: the run completes and every output is
  shaped exactly like a good one's. Both masks are measured against the image
  they were extracted from, a mask that is missing brain is repaired
  additively and within a cap, and the verdict, the numbers and a PNG overlay
  are published either way. See
  [Brain mask coverage](#brain-mask-coverage) below.

Because of that last point, every acquired slice is kept whatever the slice
count. There is no reason to crop or duplicate a slice to make the count even:
[FSL withdrew that advice](https://fsl.fmrib.ox.ac.uk/fsl/docs/diffusion/topup/users_guide/index.html)
because a cropped volume no longer carries the multiband structure `eddy` needs
for slice-to-volume correction, and an odd dimension simply selects
`b02b0_1.cnf` instead.

## Inputs

| Input | Datatype | Required |
|---|---|---|
| Diffusion series with its gradient table and JSON sidecar | `neuro/dwi` | yes |
| Reverse phase-encoded series | `neuro/dwi` | yes |
| Slice specification (`slspec`) | — | no |

The reverse series is what makes the distortion correction possible, so the app
will not run without it. There is no option to proceed anyway: an uncorrected
run is geometrically wrong while being named, shaped and summarised exactly like
a corrected one, so nothing downstream could tell the two apart if they were
pooled. Data with no opposing pair needs a different app.

Slice-to-volume correction needs to know the slice acquisition order, which the
app resolves from the first of these that is available:

1. **An `slspec` input file.** One row per excitation, listing the 0-based
   slices acquired together. A file for an 84-slice, multiband-4 protocol ships
   in `templates/` as a worked example of the format — it is not a drop-in for
   other protocols, and a slspec that does not describe the acquisition (wrong
   slice count, out-of-range or repeated indices, ragged rows) is refused rather
   than passed to `eddy`.
2. **A declared `slice_order`**, plus `multiband`, `slice_packages` and
   `slice_step` as the protocol requires. This is for sidecars that carry no
   timings at all — some Philips exports, or a converter that dropped the field.
   It asserts the acquisition rather than measuring it, so it is opt-in; where
   the sidecar does carry `SliceTiming`, the declaration is checked against it
   and a disagreement stops the run. Note that Philips's own `default` scan
   order interleaves with a step of roughly √(slices per package) rather than
   the step of 2 that `interleaved` means, so check `philips_default` against
   your protocol printout or give `slice_step` explicitly.
3. **`SliceTiming` in the sidecar**, the usual case, needing no configuration.

Given none of the three the app still completes, but corrects motion
volume-to-volume only and records that in the summary.

## Outputs

| Directory | Datatype | Contents |
|---|---|---|
| `dwi` | `neuro/dwi` | Preprocessed DWI with rotated gradients |
| `mask` | `neuro/mask` | Brain mask |
| `tensor` | `neuro/tensor` | Tensor, FA, MD, AD, RD, CL, CP, CS, colour FA, V1, S0 |
| `roistats` | `raw` | Per-ROI statistics, tidy and wide CSV, plus JSON |
| `reg` | `raw` | Atlas in native space and the ANTs transforms |
| `qc` | `raw` | `eddy_quad` report, motion and outlier files, derived acquisition parameters, brain-mask coverage reports and overlays, and the mask `eddy` used |
| `eddyqc` | `raw` | `qc.json`, `qc.pdf` and the cohort signature — the lean dataset the group QC App consumes |

`roistats/roi_stats.csv` has one row per metric and ROI, carrying `subject`,
`session` and `run_id` so results from many subjects can be concatenated
directly. `roistats/<METRIC>_mean.csv` is the same data one row per subject,
one column per ROI.

## Brain mask coverage

The mask matters more than it looks. The stage-1 mask is what `eddy --mask` is
given, and `eddy` estimates its Gaussian-process predictions and its outlier
detection inside it, so a mask with a bite out of it degrades the corrected data
*everywhere*, not only near the defect — and because the same mask bounds
`eddy_quad`'s voxel-wise metrics, a bad mask partly hides itself from its own QC
report. The stage-3 mask is published as `neuro/mask` and bounds `dtifit` and
every ROI average.

So both are measured against the image they came from, and the verdict is one of
three:

| Verdict | Meaning | What happens |
|---|---|---|
| `ok` | nothing brain-bright is left outside the mask beyond `mask_warn_fraction` of its volume, and the mask tapers rather than ending abruptly | nothing |
| `suspicious` | brain is missing: a chunk, or a mask that stops mid-brain | repaired, unless `mask_repair` says otherwise |
| `implausible` | not a brain at all — a few percent of the field of view, a volume outside the range a brain can be, or a centre far from the centre of the signal (`bet` landing on the neck) | **never** repaired: growing it would hide the only symptom |

Two detectors have to agree that something is missing, and they are chosen
because each sees what the other cannot. One reflects the mask about its own
centroid along the left-right axis: where the other hemisphere has brain and this
side does not, something was removed. The other looks inside the union of the
three directional span fills and within a couple of voxels of the mask. A third
measurement, the per-slice area profile, catches the one defect neither sees — a
mask that ends at half its widest slice instead of tapering. Missing voxels are
then split in two, because they call for opposite responses: where there is
signal the mask is at fault and can be repaired, and where the image is dark too
the **data** is at fault — a dropout — which is reported and never masked over,
since `eddy`'s outlier replacement, not a bigger mask, is what addresses it.

The repair is deliberately dull: union with a second `bet` at a lower threshold,
confined to the neighbourhood of the defect so the rest of the mask stays exactly
as `bet` made it, plus enclosed holes, only where there is signal, never into a
dropout, and never removing a voxel. If it would add more than the cap it is
discarded whole and the mask is reported instead — a repair that large is not a
repair. The cap differs by stage on purpose: losing brain is the expensive error
for `eddy`, while an over-inclusive published mask contaminates every ROI mean,
so stage 3's cap is the tighter one.

A repaired stage-1 mask changes `eddy`'s output, so it is never quiet about it:
the log warns, the task page says so in those terms, and `product.json` records
the before and after voxel counts.

Published in `qc/`: `mask_qc_eddy.json` and `mask_qc_final.json` (the full
measurements), `mask_overlay_eddy.png` and `mask_overlay_final.png` (slices with
the mask outline, what was found missing in yellow and anything the repair added
in green), and `eddy_mask.nii.gz` — the mask `eddy` actually used, which the App
did not publish before and without which "was the mask the problem?" cannot be
answered after the fact.

| Parameter | Default | Meaning |
|---|---|---|
| `mask_check` | `true` | Measure both masks against the image |
| `mask_repair` | `auto` | `auto` repairs only a mask the check flags; `always` repairs every subject's, so a study is processed identically; `never` reports and changes nothing |
| `mask_repair_f` | `auto` | The `bet` threshold for the permissive estimate. `auto` is the stage's own `f` minus 0.2 |
| `mask_warn_fraction` | `0.01` | How much brain-bright signal outside the mask, as a fraction of its volume, counts as missing brain |
| `mask_repair_cap` / `mask_repair_cap_final` | `0.25` / `0.1` | Discard the repair if it would add more than this fraction of the mask |
| `mask_repair_grow` | `0` | Extra intensity-growth iterations on the stage-1 mask. Off by default; bounded above by the in-mask 99.5th percentile so it cannot walk into the skull |
| `mask_figure` | `true` | Write the overlay PNGs |

A note for group analysis: mask repair is a per-subject difference that
`eddy_squad` cannot see — it compares eddy's parameters, not masks — so it never
splits a cohort. A study that wants strict comparability should set `mask_repair`
to `always` or `never` explicitly rather than leaving subjects to differ.

## Group quality control (eddy SQUAD)

`eddy_quad` assesses one subject; FSL's `eddy_squad` assesses a study, flagging
the subjects that sit in the tail of the group's motion, outlier and CNR
distributions. It is a separate brainlife App — it takes N subjects where the
pipeline takes one — registered against this same repository and container:
`main` routes a task to `run_squad.sh` when its config carries the group input,
and to `run.sh` otherwise.

Run it over the `eddyqc` datasets the pipeline published, with the App's input
set to accept multiple datasets. brainlife then writes them into `config.json`
as an array and describes them in `_inputs`, in the same order:

```json
{ "eddyqc": ["../5f0e.../eddyqc", "../5f0f.../eddyqc"] }
```

| Output | Contents |
|---|---|
| `squad/group_qc.pdf` | the study-wise report |
| `squad/group_db.json` | the study-wise database |
| `squad/cohorts.json` | which subjects pooled, which did not, and why |
| `squad/subject_list.txt`, `squad/grouping_variable.txt` | exactly what `eddy_squad` was given |
| `squad/updated/<subject>_qc_updated.pdf` | single-subject reports with the group's context, unless `update_single_subject_reports` is turned off |

Updating the single-subject reports happens by default, because a subject's own
report flagged against its group is half the point of running SQUAD. It needs one
thing the group report itself does not: each pooled subject's own `qc.pdf`, which
`eddy_squad` opens to append the study-wise pages to. When a subject has not
published one, the update alone is skipped and that subject is named, rather than
losing the group report — which is what `eddy_squad` would do on its own.

If the image's FSL cannot perform the update at all, the App publishes the group
report regardless and the log says why. Set `update_single_subject_reports` to
false to skip the attempt.

### Cohorts, and why a group run can refuse

`eddy_squad` pools subjects only when `eddy` was run with the same features for
all of them — it compares six flags in each `qc.json` and raises
`Eddy output inconsistency detected!` otherwise. On brainlife every subject is
an independently launched task, so that is easy to trip:

* **no GPU on the node** → no slice-to-volume metrics (`qc_s2v_params_flag`);
* **no reverse phase-encoded series** → no susceptibility field (`qc_field_flag`);
* **`eddy_repol`, `eddy_cnr_maps`, `eddy_residuals` changed between submissions**
  → no outlier, CNR or residual metrics.

Newer FSL releases compare the eddy **input** data as well, and refuse the study
with `Inconsistency detected in eddy input data in <field>!` when subjects
disagree on the acquisition — the topup acquisition parameters, the shell
b-values, the voxel size, the volume counts.

So each subject publishes a **cohort signature** (`eddyqc/squad_ready.json`, also
shown on the task page): those six flags plus every acquisition field SQUAD
compares, taken **exactly** as QUAD wrote them. The comparison is exact because
SQUAD's is: a b-value of 1495 against 1500 is a different cohort, since pooling
them would fail the whole study rather than split it. The group App buckets its
inputs by signature, reports on the largest cohort, and names the subjects it
left out and the field that differs, with both values — rather than failing on
subject 37. Run it again with `cohort` set to another signature to report on that
one too, or set `require_homogeneous` to refuse the split instead of choosing.

If your FSL turns out to tolerate a difference, narrow what the key compares with
`signature_fields` (a list of `data_*` field names) and those subjects pool again.
And if a group run is refused anyway — a future release comparing something this
app does not — the failure is followed by a comparison of every eddy input field
across the staged subjects, naming the field and which subjects hold which value.

Setting `require_gpu: true` across a project is the way to stop the cohort
splitting in the first place. So is using **one kind of sidecar** for the whole
study: phase encoding derived from the Siemens CSA fields carries the opposite
sign convention to a BIDS `PhaseEncodingDirection`, which flips both series
together and leaves the correction unchanged — but changes the acqparams, which
`eddy_squad` compares exactly. Stage 0 warns when it takes the CSA path.

### Subjects processed before this App existed

Nothing needs reprocessing. `eddy_quad` has always published `qc.json`, and that
is all `eddy_squad` reads — so an older task's `output/qc/eddy_quad/` is a valid
input, and old and new subjects pool together as long as `eddy` ran with the
same features. Two things differ: those datasets carry no `squad_ready.json`, so
subject labels come from brainlife's input metadata or the directory name (use
`subject_labels` if neither is right), and to see one subject's signature
without a group run, summarise its database directly:

```bash
python3 python/eddyqc_summary.py --qc-json <task>/output/qc/eddy_quad/qc.json \
    --subject sub-01 --out squad_ready.json
```

### Group configuration

| Parameter | Default | Meaning |
|---|---|---|
| `eddyqc` | — | The per-subject eddy QC datasets. A path to a `qc.json`, to any folder holding one (`eddyqc/`, an archived `qc` dataset, or an older task's `output/qc/eddy_quad/`), or a list of either |
| `grouping_variable` | — | A `participants.tsv`-style table with a subject column and one value column, matched **by subject name**; or a file already in `eddy_squad`'s own format, matched by position |
| `variable_name` / `variable_is_continuous` | column name / `false` | Label for the variable, and whether to draw scatter plots with a regression fit (continuous) or violin plots per class (categorical) |
| `update_single_subject_reports` | `true` | Also rewrite each subject's own report with study-wise context. Needs that subject's `qc.pdf` among the inputs |
| `cohort` | largest | The signature (or its short hash) of the cohort to report on |
| `require_homogeneous` | `false` | Fail when the inputs split into more than one cohort, instead of choosing the largest |
| `min_subjects` | `2` | Refuse to call a smaller group a study |
| `signature_fields` | every `data_*` field | Which acquisition fields decide cohort membership. Narrow it when your FSL tolerates a difference |
| `subject_labels` | from the data | Comma-separated labels overriding the ones taken from `squad_ready.json` / `_inputs` |

The grouping variable is matched by name wherever it can be: `eddy_squad` itself
matches values to subjects by line position, which silently attributes one
subject's value to another as soon as a subject is excluded from the cohort.
Supplying a table with a subject column lets the App order the values to match
the subject list it actually staged, and refuse when a value is missing.

## Configuration

Every parameter is optional.

| Parameter | Default | Meaning |
|---|---|---|
| `dtifit_shell` | `lowest` | Which shell to fit: `lowest`, `highest`, `all`, or a b-value such as `1500`. `lowest`/`highest` rank only the diffusion-weighted shells |
| `denoise` / `degibbs` | `true` | MP-PCA denoising / Gibbs ringing removal |
| `eddy_repol` | `true` | Detect and replace outlier slices |
| `eddy_mporder` | `6` | Order of the slice-to-volume motion model (CUDA only) |
| `eddy_niter` / `eddy_fwhm` | `6` / `10,6,0,0,0,0` | Eddy iterations and per-iteration smoothing |
| `require_gpu` | `false` | `true` fails when no GPU is visible; `false` falls back to a CPU eddy and skips slice-to-volume correction |
| `biascorrect` | `ants` | B1 bias correction: `ants`, `fsl` or `none` |
| `slice_order` | `auto` | How the `eddy` slice specification is obtained when no `slspec` **input** is given (that file wins if present). `auto` derives it from `SliceTiming`; `ascending`, `descending`, `interleaved`, `rev_interleaved`, `philips_default` or `step` declare it from the protocol instead, for sidecars carrying no timings |
| `multiband` / `slice_packages` / `slice_step` | `1` / `1` / — | Protocol parameters used with a declared `slice_order` |
| `acqp` / `index` | derived | Supply `acqparams.txt` / `index.txt` directly, replacing the values derived from the sidecars. For datasets whose sidecars are incomplete |
| `topup_config` | `auto` | FSL topup schedule, chosen from the matrix size: `b02b0_4.cnf`, `b02b0_2.cnf` or `b02b0_1.cnf` as the dimensions divide by 4, 2 or neither. Name one explicitly to override |
| `atlas_registration` | `true` | Set false to stop after preprocessing and the tensor fit |
| `atlas_interpolation` | `MultiLabel` | Interpolation used when warping atlas labels |
| `template_fa` / `atlas` | FSL's JHU data | Override the FA template and label image the atlas stage uses |
| `atlas_labels` | `templates/JHU-ICBM-labels.json` | ROI names and abbreviations for the label image |
| `roi_metrics` | `FA, MD, AD, RD` | Metrics to summarise per ROI |
| `mask_check` / `mask_repair` | `true` / `auto` | Brain-mask coverage check and repair — see [Brain mask coverage](#brain-mask-coverage) |
| `subject` / `session` | from input metadata | Labels written into the results |
| `nthreads` | all cores | Threads for MRtrix3, ANTs and OpenMP |

A GPU is strongly recommended: slice-to-volume correction requires a CUDA build
of `eddy`. Without one the app completes but skips it.

## Running it

**On brainlife**, submit the app against a diffusion dataset from the Apps page,
or add it to a pipeline rule to process a whole project. The group QC App is
submitted the same way, against the `eddyqc` datasets of a processed project.

**Locally**, the app runs from a directory containing a `config.json` that names
your files:

```bash
git clone https://github.com/adanyike/app-brain-dwi-preproc-slim.git
cp app-brain-dwi-preproc-slim/config.json.example config.json
$EDITOR config.json
./app-brain-dwi-preproc-slim/main
```

`main` selects Singularity, Docker or a local toolchain automatically and passes
a GPU through when one is present. Results appear in `output/` and
`product.json`.

For a group QC run, start from `config.json.squad.example` instead; `main`
recognises it by its `eddyqc` input and runs `run_squad.sh`. No GPU is needed —
it reads the QC databases, not the images.

The container is pulled, not built: `main` defaults to
`docker://nyeguh/brain-dwi-preproc-slim:1.0.0`. Point `APP_IMAGE` at another tag
or a local `.sif` to override it, and `EXTRA_BIND` at any input data living
outside the working directory. `bash test/run_tests.sh` exercises every stage
against a stub toolchain, so it runs without FSL, MRtrix3 or ANTs installed.

## Requirements

Provided by the container:

| | Version |
|---|---|
| FSL | 6.0.7.23 |
| MRtrix3 | 3.0.8 |
| ANTs | 2.6.5 |

The atlas stage uses two images from different places. The FA template it
registers to is this repository's `templates/JHU-ICBM-FA-1mm.nii.gz`, so the
registration target is the same image however the app is run and whatever FSL
release is installed. The label image it warps is FSL's, read from
`$FSLDIR/data/atlases/JHU`, along with the label list the ROI names are checked
against; the names and abbreviations themselves live in
`templates/JHU-ICBM-labels.json`, and FSL listed 48 of the 50 regions before
6.0.5. The self-test refuses to build an image whose label list and label image
disagree on how many regions there are, or whose template and label image do not
share a grid — the transform is estimated from one and applied to the other.

## Citing

If you use this app, please cite brainlife.io and the methods it runs.

**Distortion, eddy current and motion correction**

* Andersson JLR, Skare S, Ashburner J. How to correct susceptibility distortions
  in spin-echo echo-planar images: application to diffusion tensor imaging.
  *NeuroImage* 2003; 20(2):870–888.
* Andersson JLR, Sotiropoulos SN. An integrated approach to correction for
  off-resonance effects and subject movement in diffusion MR imaging.
  *NeuroImage* 2016; 125:1063–1078.
* Andersson JLR, Graham MS, Zsoldos E, Sotiropoulos SN. Incorporating outlier
  detection and replacement into a non-parametric framework for movement and
  distortion correction of diffusion MR images. *NeuroImage* 2016; 141:556–572.
* Andersson JLR, Graham MS, Drobnjak I, Zhang H, Filippini N, Bastiani M.
  Towards a comprehensive framework for movement and distortion correction of
  diffusion MR images: Within volume movement. *NeuroImage* 2017; 152:450–466.
* Bastiani M, Cottaar M, Fitzgibbon SP, et al. Automated quality control for
  within and between studies diffusion MRI data using a non-parametric framework
  for movement and distortion correction. *NeuroImage* 2019; 184:801–812.

**Denoising and ringing removal**

* Veraart J, Novikov DS, Christiaens D, Ades-aron B, Sijbers J, Fieremans E.
  Denoising of diffusion MRI using random matrix theory. *NeuroImage* 2016;
  142:394–406. doi:10.1016/j.neuroimage.2016.08.016
* Kellner E, Dhital B, Kiselev VG, Reisert M. Gibbs-ringing artifact removal
  based on local subvoxel-shifts. *Magnetic Resonance in Medicine* 2016;
  76:1574–1581.

**Bias correction and registration**

* Tustison NJ, Avants BB, Cook PA, et al. N4ITK: improved N3 bias correction.
  *IEEE Transactions on Medical Imaging* 2010; 29(6):1310–1320.
* Avants BB, Epstein CL, Grossman M, Gee JC. Symmetric diffeomorphic image
  registration with cross-correlation. *Medical Image Analysis* 2008;
  12(1):26–41.

**Brain extraction and software**

* Smith SM. Fast robust automated brain extraction. *Human Brain Mapping* 2002;
  17(3):143–155.
* Jenkinson M, Beckmann CF, Behrens TEJ, Woolrich MW, Smith SM. FSL.
  *NeuroImage* 2012; 62(2):782–790.
* Tournier J-D, Smith RE, Raffelt DA, et al. MRtrix3: A fast, flexible and open
  software framework for medical image processing and visualisation.
  *NeuroImage* 2019; 202:116137.

**Atlas**

* Mori S, Wakana S, van Zijl PCM, Nagae-Poetscher LM. *MRI Atlas of Human White
  Matter*. Elsevier, 2005.
* Wakana S, Caprihan A, Panzenboeck MM, et al. Reproducibility of quantitative
  tractography methods applied to cerebral white matter. *NeuroImage* 2007;
  36(3):630–644.

## License

MIT — see [LICENSE](LICENSE). The JHU ICBM-DTI-81 FA template and label atlas are
distributed with FSL under the FSL licence for non-commercial research use.
