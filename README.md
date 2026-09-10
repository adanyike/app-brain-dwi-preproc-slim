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

Slice-to-volume correction needs to know the slice acquisition order. Normally
that is derived from `SliceTiming` in the sidecar. Where the sidecar does not
carry it — some Philips exports, or a converter that dropped the field — supply
the `eddy` slice specification directly as the `slspec` input: one row per
excitation, listing the 0-based slices acquired together. A file for a Philips
84-slice, multiband-3 protocol ships in `templates/` as a worked example. Given
neither, the app still completes, but corrects motion volume-to-volume only and
records that in the summary.

## Outputs

| Directory | Datatype | Contents |
|---|---|---|
| `dwi` | `neuro/dwi` | Preprocessed DWI with rotated gradients |
| `mask` | `neuro/mask` | Brain mask |
| `tensor` | `neuro/tensor` | Tensor, FA, MD, AD, RD, CL, CP, CS, colour FA, V1, S0 |
| `roistats` | `raw` | Per-ROI statistics, tidy and wide CSV, plus JSON |
| `reg` | `raw` | Atlas in native space and the ANTs transforms |
| `qc` | `raw` | `eddy_quad` report, motion and outlier files, derived acquisition parameters |

`roistats/roi_stats.csv` has one row per metric and ROI, carrying `subject`,
`session` and `run_id` so results from many subjects can be concatenated
directly. `roistats/<METRIC>_mean.csv` is the same data one row per subject,
one column per ROI.

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
| `topup_config` | `auto` | FSL topup schedule, chosen from the matrix size: `b02b0_4.cnf`, `b02b0_2.cnf` or `b02b0_1.cnf` as the dimensions divide by 4, 2 or neither. Name one explicitly to override |
| `atlas_registration` | `true` | Set false to stop after preprocessing and the tensor fit |
| `atlas_interpolation` | `MultiLabel` | Interpolation used when warping atlas labels |
| `template_fa` / `atlas` | FSL's JHU data | Override the FA template and label image the atlas stage uses |
| `atlas_labels` | `templates/JHU-ICBM-labels.json` | ROI names and abbreviations for the label image |
| `roi_metrics` | `FA, MD, AD, RD` | Metrics to summarise per ROI |
| `subject` / `session` | from input metadata | Labels written into the results |
| `nthreads` | all cores | Threads for MRtrix3, ANTs and OpenMP |

A GPU is strongly recommended: slice-to-volume correction requires a CUDA build
of `eddy`. Without one the app completes but skips it.

## Running it

**On brainlife**, submit the app against a diffusion dataset from the Apps page,
or add it to a pipeline rule to process a whole project.

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
