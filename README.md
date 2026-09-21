# app-brain-dwi-preproc-slim

[![brainlife.io](https://img.shields.io/badge/brainlife.io-app-blue.svg)](https://brainlife.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Preprocessing for brain diffusion MRI acquired with **reverse phase-encoding**,
followed by diffusion tensor fitting and white-matter ROI extraction.

The app corrects susceptibility distortion from an opposing phase-encode pair,
corrects eddy currents and subject motion — including **within-volume
(slice-to-volume) motion** where a GPU is available — fits the diffusion tensor,
and reports mean FA, MD, AD and RD in the 50 white-matter regions of the JHU
ICBM-DTI-81 atlas, in each subject's own diffusion space.

Everything runs in one task, and every result carries its provenance: the
subject and session labels, the shell that was fitted, the acquisition
parameters and where each was read from, whether slice-to-volume correction
actually ran, and the quality-control verdict on both brain masks.

## What it does

| Stage | Step | Tools |
|---|---|---|
| 0 | Merge the phase-encode pair; denoise; remove Gibbs ringing; derive the acquisition parameters, volume index and slice-timing specification from the BIDS sidecars | MRtrix3 |
| 1 | Estimate the susceptibility field from opposing-phase b=0 volumes; brain-extract | FSL `topup`, `bet` |
| 2 | Eddy-current, motion and slice-to-volume correction with outlier replacement, applying the field | FSL `eddy_cuda` |
| 3 | B1 bias-field correction; detect the b-value shells and fit the tensor to one; derive RD, AD, colour FA and Westin shape measures | MRtrix3, ANTs, FSL `dtifit` |
| 4 | Register the JHU FA template to each subject's FA and warp the atlas labels into native space | ANTs |
| 5 | Summarise each metric in each of the 50 ROIs | — |
| 6 | Publish the datasets, the quality-control bundle and `product.json` | — |

Three things are measured from your data rather than assumed, so nothing has to
be prepared by hand:

* **The acquisition parameters come from the sidecars.** Phase-encoding
  direction and total readout time are read from `PhaseEncodingDirection` and
  `TotalReadoutTime`, falling back through `EffectiveEchoSpacing`, the Siemens
  bandwidth field and the estimated variants. The source used for each is
  recorded in the output.
* **The slice specification comes from `SliceTiming`** (or Siemens
  `MosaicRefAcqTimes`). Slices are grouped by acquisition time, so the multiband
  factor is measured rather than assumed, and a grouping that does not make
  sense is reported as an error instead of producing a silently wrong file.
* **The topup configuration comes from the matrix size.** topup needs the image
  size to be a multiple of each sub-sampling level in its config, so the app
  reads the dimensions and picks the fastest one they allow. Sub-sampling only
  affects speed, and the config used is recorded in `product.json`.

Every acquired slice is kept, whatever the slice count. Cropping a volume to
make the count even — advice
[FSL has since withdrawn](https://fsl.fmrib.ox.ac.uk/fsl/docs/diffusion/topup/users_guide/index.html)
— destroys the multiband structure `eddy` needs for slice-to-volume correction,
and an odd dimension simply selects a topup config that does not sub-sample.

## Inputs

| Input | Datatype | Required |
|---|---|---|
| Diffusion series with its gradient table and JSON sidecar | `neuro/dwi` | yes |
| Reverse phase-encoded series | `neuro/dwi` | yes |
| Slice specification (`slspec`) | — | no |

**The reverse phase-encoded series is required.** It is what makes the
distortion correction possible, and there is no option to proceed without it: an
uncorrected run is geometrically wrong while being named, shaped and summarised
exactly like a corrected one, so nothing downstream could tell the two apart if
they were pooled. Data with no opposing pair needs a different app.

Slice-to-volume correction needs the slice acquisition order, which the app
takes from the first of these that is available:

1. **An `slspec` input file** — one row per excitation, listing the 0-based
   slices acquired together. A worked example for an 84-slice, multiband-4
   protocol ships in `templates/`; it is not a drop-in for other protocols, and
   a file that does not describe your acquisition is refused rather than passed
   to `eddy`.
2. **A declared `slice_order`**, for sidecars that carry no timings at all —
   some Philips exports, or a converter that dropped the field. This asserts the
   acquisition rather than measuring it, so it is opt-in; where the sidecar does
   carry `SliceTiming`, the declaration is checked against it and a disagreement
   stops the run.
3. **`SliceTiming` in the sidecar** — the usual case, needing no configuration.

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
| `qc` | `raw` | `eddy_quad` report, motion and outlier files, the derived acquisition parameters, and the brain-mask coverage reports and overlays |
| `eddyqc` | `raw` | The lean QC dataset the group QC App consumes |

`roistats/roi_stats.csv` has one row per metric and ROI, carrying `subject`,
`session` and `run_id`, so results from many subjects concatenate directly.
`roistats/<METRIC>_mean.csv` is the same data one row per subject, one column
per ROI. Each row reports the mean, SD, median, min, max and voxel count.

## Quality control

`eddy_quad` runs by default and its report is published under
`qc/eddy_quad/`, alongside the per-volume motion and outlier files and a summary
on the task page showing per-volume motion and per-ROI FA.

### Brain mask coverage

Both brain masks are measured against the image they were extracted from,
because `bet` sometimes returns a mask with a bite out of it or one that stops
short of the temporal lobes — and nothing downstream notices. The run completes
and every output is shaped exactly like a good one's.

It matters most at stage 1. That mask is what `eddy --mask` is given, and `eddy`
estimates its predictions and its outlier detection inside it, so a mask missing
a chunk degrades the corrected data *everywhere*, not only near the defect.
Because the same mask bounds `eddy_quad`'s metrics, a bad mask partly hides
itself from its own QC report. The stage-3 mask is published as `neuro/mask` and
bounds `dtifit` and every ROI average.

Each mask gets one of three verdicts:

| Verdict | Meaning | What happens |
|---|---|---|
| `ok` | nothing brain-bright is left outside the mask, and it tapers rather than ending abruptly | nothing |
| `suspicious` | brain is missing: a chunk, or a mask that stops mid-brain | repaired, unless `mask_repair` says otherwise |
| `implausible` | not a brain at all — far too small or large, or centred away from the signal (`bet` landing on the neck) | **never** repaired: growing it would hide the only symptom |

Where brain appears to be missing, the app distinguishes two causes, because
they need opposite responses. Where there is signal outside the mask, the mask
is at fault and can be repaired. Where the image is dark too, the **data** is at
fault — a dropout — which is reported and never masked over, since `eddy`'s
outlier replacement is what addresses it.

The repair is deliberately dull: a union with a second, more permissive `bet`,
confined to the neighbourhood of the defect so the rest of the mask stays as
`bet` made it, plus enclosed holes, only where there is signal, and never
removing a voxel. If it would add more than its cap it is discarded whole and
the mask is reported instead. A repaired stage-1 mask changes `eddy`'s output,
so it is never quiet about it: the log warns, the task page says so, and
`product.json` records the before and after voxel counts.

Published in `qc/`: `mask_qc_eddy.json` and `mask_qc_final.json` with the full
measurements, `mask_overlay_eddy.png` and `mask_overlay_final.png` showing the
mask outline with anything found missing in yellow and anything the repair added
in green, and `eddy_mask.nii.gz` with `eddy_meanb0.nii.gz` — the mask `eddy`
used and the image it was judged against, so "was the mask the problem?" can be
answered after the fact.

For a study that needs every subject treated identically, set `mask_repair` to
`always` or `never` rather than leaving subjects to differ.

## Group quality control (eddy SQUAD)

`eddy_quad` assesses one subject. FSL's `eddy_squad` assesses a **study**,
flagging the subjects that sit in the tail of the group's motion, outlier and
CNR distributions. It is a separate brainlife App — it takes N subjects where
this one takes one — backed by the same repository and container.

Run it over the `eddyqc` datasets this app published, with the App's input set
to accept multiple datasets. No GPU is needed: it reads the QC databases, not
the images.

| Output | Contents |
|---|---|
| `squad/group_qc.pdf` | the study-wise report |
| `squad/group_db.json` | the study-wise database |
| `squad/cohorts.json` | which subjects pooled, which did not, and why |
| `squad/updated/<subject>_qc_updated.pdf` | each subject's own report with the group's context |

**Start with `cohorts.json`.** It is the file that explains a group run.

Updating the single-subject reports happens by default, because a subject's own
report flagged against its group is half the point of running SQUAD. It needs
each pooled subject's own `qc.pdf`, which the `eddyqc` dataset carries; when a
subject has not published one, that subject is named and only the update is
skipped, rather than losing the group report.

Subjects processed before this App existed need no reprocessing. `eddy_quad` has
always published the database `eddy_squad` reads, so an older task's
`output/qc/eddy_quad/` is a valid input and old and new subjects pool together.

### Why a group run can leave subjects out

`eddy_squad` pools subjects only when `eddy` ran with the same features for all
of them, and when they agree on the acquisition. On brainlife every subject is
an independently launched task, so that is easy to trip:

* **no GPU on the node** → no slice-to-volume metrics for that subject;
* **different acquisition** → a different number of shells, b=0 volumes,
  diffusion-weighted volumes or phase-encode directions, or different readout
  times;
* **settings changed between submissions** → different outlier, CNR or residual
  metrics.

Rather than failing on subject 37, the App groups its inputs by what they have
in common, reports on the largest group, and names the subjects it left out and
the field that differs, with both values. Run it again with `cohort` set to
another group to report on that one too, or set `require_homogeneous` to refuse
the split instead of choosing.

Two settings keep a study together in the first place: `require_gpu: true`
across the project, and **one kind of sidecar** for the whole study, since
phase-encoding read from Siemens DICOM fields carries the opposite sign
convention to a BIDS `PhaseEncodingDirection`. That flips both series together
and leaves the correction unchanged, but changes the parameters `eddy_squad`
compares. Stage 0 warns when it takes that path. The same applies to `pe_dir` /
`rpe_dir` set by hand: whichever way round you set them, set them the same way
for every subject.

### Pooling two sites that ran the same protocol

Two sites running the same protocol can state the readout time slightly
differently — 0.0959097 against 0.0965997 — because a sidecar can describe the
same echo train in two ways. `eddy_squad` compares that field exactly, so it
would call them two studies and the cross-site report could not be made.

`pool_across_acquisition` (**on by default**) merges groups that differ *only*
in that field, in the App's own working copies of the QC databases. Your inputs
are never touched. It is bounded: the phase-encode directions must be identical
and the readout times must agree within `pool_readout_tolerance`, 1.75% by
default, which covers the difference between the two ways of stating one readout
and stays well below the gap between genuinely different scanners. Anything
wider is refused and the groups stay split, with the reason on the task page.

Because a rewritten database no longer says exactly what the scanner said, every
merge is disclosed: `cohorts.json` records each subject's original value, the
log warns, and `product.json` names what it costs. Motion, outlier and CNR
indices do not depend on those parameters and stay comparable; the
distortion-derived index does, and does not. Set it to `false` for one report
per acquisition.

### Group configuration

| Parameter | Default | Meaning |
|---|---|---|
| `eddyqc` | — | The per-subject eddy QC datasets. A path to a `qc.json`, to any folder holding one, or a list of either |
| `grouping_variable` | — | A `participants.tsv`-style table with a subject column and one value column, matched **by subject name**; or a file already in `eddy_squad`'s own format, matched by position |
| `variable_name` / `variable_is_continuous` | column name / `false` | Label for the variable, and whether to draw scatter plots with a regression fit (continuous) or violin plots per class (categorical) |
| `update_single_subject_reports` | `true` | Also rewrite each subject's own report with study-wise context |
| `cohort` | largest | Which group of subjects to report on |
| `require_homogeneous` | `false` | Fail when the inputs split into more than one group, instead of choosing the largest |
| `pool_across_acquisition` | `true` | Pool groups that differ only in the readout time, as described above |
| `pool_readout_tolerance` | `0.0175` | How far apart two readout times may be, as a fraction of the reference site's, and still count as the same acquisition |
| `min_subjects` | `2` | Refuse to call a smaller group a study |
| `subject_labels` | from the data | Comma-separated labels overriding the ones taken from the input metadata |

Class **names** work as grouping values. `eddy_squad` cannot read them — it
parses the column as numbers — so the App encodes named classes to integers in
sorted order, hands SQUAD the numbers, and publishes the mapping in
`cohorts.json` and on the task page. The report's group axes are therefore
labelled `0` and `1`; the task page says which is which. Numeric values pass
through untouched. A variable marked `variable_is_continuous` must be numeric.

The grouping variable is matched by name wherever it can be, because
`eddy_squad` itself matches values to subjects by line position — which
silently attributes one subject's value to another as soon as a subject is
excluded. Supplying a table with a subject column lets the App order the values
to match the subjects it actually used, and refuse when one is missing.

## Configuration

Every parameter is optional. The defaults are what most data wants.

### Labels

| Parameter | Default | Meaning |
|---|---|---|
| `subject` / `session` | from input metadata | Labels written into the results |
| `split_subject_session` | `false` | Split a session off the end of the subject label (`sub01-MR03` → `sub01` + `MR03`) |
| `session_prefixes` | `ses,MR,visit,tp,V` | The suffixes `split_subject_session` recognises |

### Acquisition

Set these only when the sidecars are incomplete or wrong.

| Parameter | Default | Meaning |
|---|---|---|
| `pe_dir` / `rpe_dir` | `auto` | Phase-encoding direction of each series, e.g. `j-`. Set them the same way round for every subject in a study |
| `readout_time` / `rreadout_time` | `auto` | Total readout time of each series, in seconds |
| `b0_threshold` | `50` | A volume at or below this b-value counts as unweighted |

### Denoising and the slice specification

| Parameter | Default | Meaning |
|---|---|---|
| `denoise` / `degibbs` | `true` | MP-PCA denoising / Gibbs ringing removal |
| `slice_order` | `auto` | `auto` derives the slice specification from `SliceTiming`. `ascending`, `descending`, `interleaved`, `rev_interleaved`, `philips_default` or `step` declare it from the protocol instead, for sidecars carrying no timings |
| `multiband` / `slice_packages` / `slice_step` | from the sidecar, else `1` / `1` / — | Protocol parameters used with a declared `slice_order` |

### Distortion and brain extraction

| Parameter | Default | Meaning |
|---|---|---|
| `topup_config` | `auto` | Chosen from the matrix size. Name a config explicitly to override |
| `bet_topup_f` | `0.4` | `bet` threshold for the mask `eddy` is given |
| `bet_final_f` | `0.3` | `bet` threshold for the published `neuro/mask` |

### Motion and eddy-current correction

| Parameter | Default | Meaning |
|---|---|---|
| `require_gpu` | `false` | `true` fails when no GPU is visible; `false` falls back to a CPU eddy and skips slice-to-volume correction |
| `eddy_repol` | `true` | Detect and replace outlier slices |
| `eddy_ol_type` | `both` | Scope of outlier detection. `both` and `gw` need the multiband structure and fall back to `sw` without it |
| `eddy_niter` / `eddy_fwhm` | `6` / `10,6,0,0,0,0` | Iterations and per-iteration smoothing. `eddy_fwhm` must have exactly `eddy_niter` entries |
| `eddy_mporder` / `eddy_s2v_niter` | `6` / `6` | Slice-to-volume motion model order and iterations (GPU only) |
| `eddy_cnr_maps` / `eddy_residuals` | `true` / `false` | Write CNR maps / per-volume residuals |
| `eddy_slm` | `none` | Second-level model for the eddy-current field |
| `eddy_qc` | `true` | Run `eddy_quad`. With this off there is no group QC dataset |

Keeping `eddy_repol`, `eddy_cnr_maps` and `eddy_residuals` the same across a
study matters: changing them between subjects gives those subjects different QC
metrics, which splits a group SQUAD run.

### Brain mask

Described under [Quality control](#quality-control) above.

| Parameter | Default | Meaning |
|---|---|---|
| `mask_check` | `true` | Measure both masks against the image |
| `mask_repair` | `auto` | `auto` repairs only a mask the check flags; `always` repairs every subject's, so a study is processed identically; `never` reports and changes nothing |
| `mask_repair_f` | `auto` | `bet` threshold for the permissive estimate. `auto` is the stage's own threshold minus 0.2 |
| `mask_warn_fraction` | `0.01` | How much brain-bright signal outside the mask, as a fraction of its volume, counts as missing brain |
| `mask_min_defect_block` | `0.35` | How concentrated a defect has to be before it counts as one rather than the ragged edge every mask has |
| `mask_repair_cap` / `mask_repair_cap_final` | `0.25` / `0.1` | Discard the repair if it would add more than this fraction of the mask |
| `mask_repair_grow` | `0` | Extra intensity-growth iterations on the stage-1 mask. Off by default |
| `mask_figure` | `true` | Write the overlay PNGs |

### The tensor fit

| Parameter | Default | Meaning |
|---|---|---|
| `biascorrect` | `ants` | B1 bias correction: `ants`, `fsl` or `none` |
| `dtifit_shell` | `lowest` | Which shell to fit: `lowest`, `highest`, `all`, or a b-value such as `1500`. `lowest`/`highest` rank only the diffusion-weighted shells |
| `shell_tolerance` | `100` | The b-value gap that separates two shells |
| `dtifit_wls` | `false` | Fit by weighted least squares |
| `dtifit_sse` | `false` | Also write the sum of squared errors |

### Atlas and ROI statistics

| Parameter | Default | Meaning |
|---|---|---|
| `atlas_registration` | `true` | Set false to stop after preprocessing and the tensor fit |
| `atlas_interpolation` | `MultiLabel` | Interpolation used when warping atlas labels |
| `roi_metrics` | `FA, MD, AD, RD` | Metrics to summarise per ROI |
| `roi_restrict_to_mask` | `true` | Intersect every ROI with the brain mask before averaging |
| `roi_exclude_zeros` | `false` | Drop exactly-zero voxels from each ROI average |
| `write_roi_masks` | `false` | Also write one binary mask per ROI |
| `nthreads` | all cores | Threads for MRtrix3, ANTs and OpenMP |

A GPU is strongly recommended: slice-to-volume correction requires a CUDA build
of `eddy`. Without one the app completes but skips it.

A few further parameters exist as escape hatches for unusual data — supplying
`acqparams.txt` and `index.txt` directly, passing extra flags to `topup` or
`eddy`, naming a specific `eddy` build, or overriding the atlas files. They are
listed with their defaults in `config.json.example`, and are not needed for data
whose sidecars are complete.

## Running it

**On brainlife**, submit the app against a diffusion dataset from the Apps page,
or add it to a pipeline rule to process a whole project. The group QC App is
submitted the same way, against the `eddyqc` datasets of a processed project.

**Locally**, the app runs from a directory containing a `config.json` naming your
files:

```bash
git clone https://github.com/adanyike/app-brain-dwi-preproc-slim.git
cp app-brain-dwi-preproc-slim/config.json.example config.json
$EDITOR config.json
./app-brain-dwi-preproc-slim/main
```

`main` selects Singularity, Docker or a local toolchain automatically and passes
a GPU through when one is present. Results appear in `output/` and
`product.json`. For a group QC run, start from `config.json.squad.example`
instead; `main` recognises it by its `eddyqc` input.

The container is pulled, not built. Point `APP_IMAGE` at another tag or a local
`.sif` to override the default, and `EXTRA_BIND` at any input data living
outside the working directory.

## Requirements

Provided by the container:

| | Version |
|---|---|
| FSL | 6.0.7.23 |
| MRtrix3 | 3.0.8 |
| ANTs | 2.6.5 |

The FA template the atlas stage registers to is this repository's own copy, so
the registration target is the same image however the app is run and whatever
FSL release is installed. The label image it warps is FSL's. All 50 JHU
ICBM-DTI-81 regions are reported.

The container is published, so nothing needs building to run the app.

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
