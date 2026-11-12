# app-brain-dwi-preproc-slim

[![brainlife.io](https://img.shields.io/badge/brainlife.io-app-blue.svg)](https://brainlife.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Preprocessing for brain diffusion MRI acquired with **reverse phase-encoding**,
followed by a tensor fit and white-matter ROI statistics.

The app corrects susceptibility distortion, eddy currents and subject motion —
including **within-volume (slice-to-volume) motion** when a GPU is available —
fits the diffusion tensor, and reports mean FA, MD, AD and RD in the 50
white-matter regions of the JHU ICBM-DTI-81 atlas, in each subject's own space.

Every result records how it was made: the subject and session, the shell that
was fitted, where each acquisition parameter came from, whether slice-to-volume
correction ran, and the quality check on both brain masks.

## What it does

| Stage | Step | Tools |
|---|---|---|
| 0 | Merge the phase-encode pair; denoise; remove Gibbs ringing; derive the acquisition parameters and slice timing from the sidecars | MRtrix3 |
| 1 | Estimate the susceptibility field from opposing-phase b=0 volumes; brain-extract | FSL `topup`, `bet` |
| 2 | Correct eddy currents, motion and slice-to-volume motion, replacing outlier slices | FSL `eddy_cuda` |
| 3 | Correct the bias field; pick a shell and fit the tensor; derive RD, AD, colour FA and shape measures | MRtrix3, ANTs, FSL `dtifit` |
| 4 | Register the JHU FA template to the subject's FA and bring the atlas labels into native space | ANTs |
| 5 | Summarise each metric in each of the 50 ROIs | — |
| 6 | Publish the datasets, the QC bundle and `product.json` | — |

Nothing has to be prepared by hand. The app reads what it needs from your data:

* **Acquisition parameters** come from the sidecar: `PhaseEncodingDirection`
  and `TotalReadoutTime`, falling back to `EffectiveEchoSpacing`, the Siemens
  bandwidth field and dcm2niix's estimates. On brainlife the sidecar is the
  dataset's *Metadata/Sidecar*, passed in automatically. Locally, point
  `dwi_json` / `rdwi_json` at it, or keep `dwi.json` next to `dwi.nii.gz`.
* **Slice timing** comes from `SliceTiming` (or Siemens `MosaicRefAcqTimes`).
  The multiband factor is measured from it, and timings that don't make sense
  stop the run instead of producing a wrong file.
* **The topup configuration** is picked from the image size: the fastest one
  the dimensions allow.

The output records where each value came from. Every slice is kept, even with
an odd slice count: cropping one, as FSL used to advise, breaks the multiband
structure slice-to-volume correction depends on.

## Inputs

| Input | Datatype | Required |
|---|---|---|
| Diffusion series, with gradients and sidecar | `neuro/dwi` | yes |
| Reverse phase-encoded series | `neuro/dwi` | yes |
| Slice specification (`slspec`) | `raw` — a dataset holding one `.txt` | no |

**The reverse series is required.** Without it there is no distortion
correction. An uncorrected result would look exactly like a corrected one, and
nothing downstream could tell them apart. Data without an opposing pair needs a
different app.

For slice-to-volume correction the app needs the slice order. It takes the
first of:

1. **An `slspec` file** — one row per excitation, listing the 0-based slices
   acquired together. `templates/` has an example for an 84-slice, multiband-4
   protocol. A file that doesn't match your data is refused.
2. **A declared `slice_order`**, for sidecars without timings (some Philips
   exports). If the sidecar does have `SliceTiming` and disagrees, the run stops.
3. **`SliceTiming` in the sidecar** — the usual case.

With none of these, the app still runs, corrects motion volume by volume only,
and says so in the summary.

## Outputs

| Directory | Datatype | Contents |
|---|---|---|
| `dwi` | `neuro/dwi` | Preprocessed DWI with rotated gradients and its sidecar |
| `mask` | `neuro/mask` | Brain mask |
| `tensor` | `neuro/tensor` | Tensor, FA, MD, AD, RD, CL, CP, CS, colour FA, V1, S0 |
| `roistats` | `raw` | Per-ROI statistics as tidy and wide CSV, plus JSON |
| `reg` | `raw` | Atlas in native space and the ANTs transforms |
| `qc` | `raw` | `eddy_quad` report, motion and outlier files, acquisition parameters, brain-mask reports and overlays |
| `eddyqc` | `raw` | The small QC dataset the group QC App reads |

`roistats/roi_stats.csv` has one row per metric and ROI, with `subject`,
`session` and `run_id`, so files from many subjects concatenate directly.
`roistats/<METRIC>_mean.csv` has one row per subject and one column per ROI.
Each value comes with its SD, median, min, max and voxel count.

## Quality control

`eddy_quad` runs by default. Its report goes in `qc/eddy_quad/`, and the task
page shows per-volume motion and per-ROI FA.

### Brain mask coverage

`bet` sometimes returns a mask with a bite out of it, or one that stops short of
the temporal lobes. Nothing downstream notices, and the run looks fine. The
mask from stage 1 matters most: `eddy` works inside it, so a missing chunk
degrades the corrected data everywhere, and it also hides the problem from
`eddy_quad`. The stage-3 mask is the published `neuro/mask` and bounds every ROI
average.

So both masks are checked against their image and given a verdict:

| Verdict | Meaning | What happens |
|---|---|---|
| `ok` | no brain left outside, and the mask tapers off normally | nothing |
| `suspicious` | brain is missing: a chunk, or a mask that stops mid-brain | repaired, unless `mask_repair` says otherwise |
| `implausible` | not a brain at all: wrong size, or off-centre (`bet` found the neck) | **never** repaired, since growing it would hide the problem |

Missing brain can have two causes. If there is signal outside the mask, `bet`
got it wrong and the mask is repaired. If the image is dark there too, the data
has a dropout. That is reported and left alone: `eddy`'s outlier replacement
deals with it.

The repair adds a second, more permissive `bet`, but only around the defect and
only where there is signal. It never removes a voxel. A repair that would add
more than its cap is discarded and reported instead. Any repair is announced in
the log, on the task page and in `product.json`, because it changes `eddy`'s
result.

`qc/` holds the measurements (`mask_qc_eddy.json`, `mask_qc_final.json`),
overlays (`mask_overlay_*.png`: the mask outlined in red, missing brain in
yellow, repairs in green), and
the mask and image `eddy` actually used (`eddy_mask.nii.gz`,
`eddy_meanb0.nii.gz`).

For a study where every subject must be treated the same way, set
`mask_repair` to `always` or `never`.

## Group quality control (eddy SQUAD)

`eddy_quad` looks at one subject. FSL's `eddy_squad` looks at a **study** and
flags subjects in the tail of the group's motion, outlier and CNR
distributions. It runs as a separate brainlife App from this same repository
and container, taking many `eddyqc` datasets at once. It needs no GPU.

| Output | Contents |
|---|---|
| `squad/group_qc.pdf` | the study-wise report |
| `squad/group_db.json` | the study-wise database |
| `squad/cohorts.json` | who was pooled, who wasn't, and why |
| `squad/updated/<subject>_qc_updated.pdf` | each subject's own report, set against the group |

**Read `cohorts.json` first.** It explains the run.

Each subject's own report is updated by default. That needs the subject's
`qc.pdf`, which the `eddyqc` dataset includes. If one is missing, only that
update is skipped and the group report is still made. Any folder holding
`eddy_quad`'s `qc.json` is accepted as input, including `output/qc/eddy_quad/`
from an earlier local run.

### Why some subjects can be left out

`eddy_squad` only pools subjects whose `eddy` runs used the same features and
whose acquisitions match. Each subject is its own task, so this is easy to
break:

* **no GPU on the node** → no slice-to-volume metrics;
* **a different acquisition** → different shells, volume counts, phase-encode
  directions or readout times;
* **settings changed between runs** → different outlier, CNR or residual
  metrics.

Instead of failing, the App groups subjects by what they share, reports on the
largest group, and lists who was left out and which field differs. Set `cohort`
to report on another group, or `require_homogeneous` to fail instead.

To keep a study together: set `require_gpu: true` for every subject, and use one
kind of sidecar throughout. Phase encoding read from Siemens DICOM fields has
the opposite sign to a BIDS sidecar. The correction comes out the same, but
`eddy_squad` sees different parameters. If you set `pe_dir` / `rpe_dir` by
hand, set them the same way for every subject.

### Pooling sites that ran the same protocol

Two sites can report the same readout slightly differently (0.0959097 vs
0.0965997), because a sidecar can measure the echo train in two ways.
`eddy_squad` compares that value exactly, so it would treat them as two
studies.

`pool_across_acquisition` (**on by default**) merges groups that differ *only*
in readout time, by at most `pool_readout_tolerance` (1.75%) — enough for the
two ways of stating one readout, well short of a genuinely different scanner.
Phase-encode directions must match exactly. Only the App's working copies are
changed, never your inputs, and every merge is disclosed in `cohorts.json`, the
log and `product.json`. Motion, outlier and CNR measures are unaffected; the
distortion-based one is not. Set it to `false` to get one report per
acquisition.

### Group configuration

| Parameter | Default | Meaning |
|---|---|---|
| `eddyqc` | — | The per-subject eddy QC datasets: a `qc.json`, a folder holding one, or a list of either |
| `grouping_variable` | — | A `participants.tsv`-style table (subject column plus one value column), matched **by subject name**; or a file in `eddy_squad`'s own format, matched by position. On brainlife, an optional `raw` input holding one `.tsv`, `.csv` or `.txt` |
| `variable_name` / `variable_is_continuous` | column name / `false` | The variable's label, and whether to plot it as continuous (scatter with a fit) or categorical (violins per class) |
| `update_single_subject_reports` | `true` | Also update each subject's own report |
| `cohort` | largest | Which group of subjects to report on |
| `require_homogeneous` | `false` | Fail if the subjects split into groups, instead of picking the largest |
| `pool_across_acquisition` | `true` | Pool groups that differ only in readout time (see above) |
| `pool_readout_tolerance` | `0.0175` | How far two readout times may differ, as a fraction, and still count as one acquisition |
| `min_subjects` | `2` | The smallest group worth reporting |
| `subject_labels` | from the data | Comma-separated labels replacing the ones from the input metadata |

Group values can be names such as site codes. `eddy_squad` only reads numbers,
so the App numbers the names in sorted order and shows the mapping in
`cohorts.json` and on the task page (the report's axes read `0`, `1`, …).
Continuous variables must be numeric.

Use a table with a subject column when you can. `eddy_squad` matches values by
line position, so a single excluded subject would shift every later value onto
the wrong person. With names, the App lines them up itself and stops if a
subject is missing.

## Configuration

Every parameter is optional; the defaults suit most data.

### Labels

| Parameter | Default | Meaning |
|---|---|---|
| `subject` / `session` | from input metadata | Labels written into the results |
| `split_subject_session` | `false` | Split a session off the subject label (`sub01-MR03` → `sub01` + `MR03`) |
| `session_prefixes` | `ses,MR,visit,tp,V` | Session prefixes `split_subject_session` recognises |

### Acquisition

Only needed when the sidecars are incomplete or wrong.

| Parameter | Default | Meaning |
|---|---|---|
| `pe_dir` / `rpe_dir` | `auto` | Phase-encoding direction of each series, e.g. `j-`. Set them the same way for every subject |
| `readout_time` / `rreadout_time` | `auto` | Total readout time of each series, in seconds |
| `b0_threshold` | `50` | Volumes at or below this b-value count as b=0 |

### Denoising and slice timing

| Parameter | Default | Meaning |
|---|---|---|
| `denoise` / `degibbs` | `true` | MP-PCA denoising / Gibbs ringing removal |
| `slice_order` | `auto` | `auto` uses `SliceTiming`. `ascending`, `descending`, `interleaved`, `rev_interleaved`, `philips_default` or `step` declare the order instead |
| `multiband` / `slice_packages` / `slice_step` | from the sidecar, else `1` / `1` / — | Protocol details for a declared `slice_order` |

### Distortion and brain extraction

| Parameter | Default | Meaning |
|---|---|---|
| `topup_config` | `auto` | Picked from the image size; name one to override |
| `bet_topup_f` | `0.4` | `bet` threshold for the mask `eddy` uses |
| `bet_final_f` | `0.3` | `bet` threshold for the published mask |

### Motion and eddy-current correction

| Parameter | Default | Meaning |
|---|---|---|
| `require_gpu` | `false` | `true` fails without a GPU; `false` falls back to CPU `eddy` without slice-to-volume correction |
| `eddy_repol` | `true` | Detect and replace outlier slices |
| `eddy_ol_type` | `both` | Outlier detection scope. `both` and `gw` need multiband information and fall back to `sw` without it |
| `eddy_niter` / `eddy_fwhm` | `6` / `10,6,0,0,0,0` | Iterations and smoothing per iteration; `eddy_fwhm` needs one value per iteration |
| `eddy_mporder` / `eddy_s2v_niter` | `6` / `6` | Slice-to-volume model order and iterations (GPU only) |
| `eddy_cnr_maps` / `eddy_residuals` | `true` / `false` | Write CNR maps / residuals |
| `eddy_slm` | `none` | Second-level model for the eddy-current field |
| `eddy_qc` | `true` | Run `eddy_quad`. Without it there is no group QC dataset |

Keep `eddy_repol`, `eddy_cnr_maps` and `eddy_residuals` the same across a
study, or the group QC run will split it.

### Brain mask

See [Brain mask coverage](#brain-mask-coverage).

| Parameter | Default | Meaning |
|---|---|---|
| `mask_check` | `true` | Check both masks |
| `mask_repair` | `auto` | `auto` repairs flagged masks; `always` repairs every subject's; `never` only reports |
| `mask_repair_f` | `auto` | `bet` threshold for the permissive pass; `auto` is the stage's own minus 0.2 |
| `mask_warn_fraction` | `0.01` | How much brain-bright signal outside the mask, as a fraction of its volume, counts as missing brain |
| `mask_min_defect_block` | `0.35` | How concentrated a gap must be to count as a defect rather than a ragged edge |
| `mask_repair_cap` / `mask_repair_cap_final` | `0.25` / `0.1` | Discard a repair that would grow the mask by more than this fraction |
| `mask_repair_grow` | `0` | Extra intensity-based growth of the stage-1 mask |
| `mask_figure` | `true` | Write the overlay images |

### Tensor fit

| Parameter | Default | Meaning |
|---|---|---|
| `biascorrect` | `ants` | Bias-field correction: `ants`, `fsl` or `none` |
| `dtifit_shell` | `lowest` | Shell to fit: `lowest`, `highest`, `all`, or a b-value such as `1500` |
| `shell_tolerance` | `100` | b-values closer than this belong to the same shell |
| `dtifit_wls` | `false` | Fit by weighted least squares |
| `dtifit_sse` | `false` | Also write the sum of squared errors |

### Atlas and ROI statistics

| Parameter | Default | Meaning |
|---|---|---|
| `atlas_registration` | `true` | `false` stops after the tensor fit |
| `atlas_interpolation` | `MultiLabel` | Interpolation for the atlas labels |
| `roi_metrics` | `FA, MD, AD, RD` | Metrics to summarise per ROI |
| `roi_restrict_to_mask` | `true` | Keep only ROI voxels inside the brain mask |
| `roi_exclude_zeros` | `false` | Ignore voxels that are exactly zero |
| `write_roi_masks` | `false` | Also write one mask per ROI |
| `nthreads` | all cores | Threads for MRtrix3, ANTs and OpenMP |

A few more parameters cover unusual data: supplying `acqparams.txt` and
`index.txt` yourself, extra `topup` or `eddy` flags, a specific `eddy` build, or
different atlas files. They are listed in `config.json.example`.

## Running it

A GPU is strongly recommended: slice-to-volume correction needs CUDA `eddy`.
Without one, the app still finishes but skips that step.

**On brainlife**, run the app on a subject's two diffusion series, or add it to
a pipeline rule for a whole project. Run the group QC App on the `eddyqc`
datasets it produces.

**Locally**, run it from a folder whose `config.json` points at your files:

```bash
git clone https://github.com/adanyike/app-brain-dwi-preproc-slim.git
cp app-brain-dwi-preproc-slim/config.json.example config.json
$EDITOR config.json
./app-brain-dwi-preproc-slim/main
```

`main` uses Singularity, Docker or a local toolchain, whichever it finds, and
passes a GPU through when there is one. Results land in `output/` and
`product.json`. On brainlife they go next to `config.json` instead, one folder
per output, where brainlife collects them. `OUT_DIR` overrides either. For a
group run, start from `config.json.squad.example`.

The container is pulled, not built. Set `APP_IMAGE` to use another tag or a
local `.sif`, and `EXTRA_BIND` to mount input data stored outside the working
folder.

## Requirements

Everything is in the container:

| | Version |
|---|---|
| FSL | 6.0.7.23 |
| MRtrix3 | 3.0.8 |
| ANTs | 2.6.5 |

The FA template used for registration is this repository's own copy, so the
target is the same whatever FSL release is installed. The atlas labels are
FSL's.

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
