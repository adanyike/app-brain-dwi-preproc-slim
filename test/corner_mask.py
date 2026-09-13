#!/usr/bin/env python3
"""Write a mean b=0 and a mask that is not a brain, for the dry run's refusal case.

`bet` locking onto the neck, a bright artefact or a coil element is the failure
mode the "implausible" verdict exists for: the mask is somewhere plausible-looking
to a file listing and nowhere near the brain.  No fractional threshold turns the
synthetic phantom into that, so it is written directly.

    python3 test/corner_mask.py <scenario dir>

reads `<dir>/input/dwi/dwi.nii.gz` and writes `<dir>/meanb0.nii.gz` and
`<dir>/mask.nii.gz`.
"""

import os
import sys

import numpy as np
import nibabel as nib


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    scenario = argv[1]
    image = nib.load(os.path.join(scenario, "input", "dwi", "dwi.nii.gz"))
    data = np.asanyarray(image.dataobj).astype(np.float64)
    if data.ndim == 4:
        data = data.mean(axis=3)
    nib.save(nib.Nifti1Image(data.astype(np.float32), image.affine),
             os.path.join(scenario, "meanb0.nii.gz"))

    corner = np.zeros(data.shape, dtype=np.uint8)
    corner[1:5, 1:5, 1:4] = 1
    nib.save(nib.Nifti1Image(corner, image.affine),
             os.path.join(scenario, "mask.nii.gz"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
