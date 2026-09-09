#!/usr/bin/env python3
"""Stand-in implementations of the FSL / MRtrix3 / ANTs commands this app calls.

They do just enough real work -- correct output file names, correct array
shapes, plausible values -- for `run.sh` to be executed end to end without a
neuroimaging toolchain installed.  This exercises the plumbing (argument
construction, file hand-off between stages, the state file, output layout);
it says nothing about whether the *science* is right, which only a run against
the real binaries can tell you.

Every command dispatches through this one file; the name it was invoked as
selects the behaviour.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import nibabel as nib


def load(path):
    return nib.load(path)


def save(path, data, like=None, dtype=np.float32):
    affine = like.affine if like is not None else np.eye(4)
    header = like.header.copy() if like is not None else None
    if header is not None:
        header.set_data_dtype(dtype)
    nib.save(nib.Nifti1Image(np.asarray(data, dtype=dtype), affine, header), path)


def data_of(path):
    return np.asanyarray(load(path).dataobj).astype(np.float64)


def strip_ext(path):
    for ext in (".nii.gz", ".nii"):
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


def nii(path):
    return path if path.endswith((".nii", ".nii.gz")) else path + ".nii.gz"


def read_bvals(path):
    with open(path) as fh:
        return np.array([float(v) for v in fh.read().split()])


def read_bvecs(path):
    with open(path) as fh:
        rows = [[float(v) for v in line.split()] for line in fh if line.strip()]
    return np.array(rows)


def write_bvals(path, values):
    with open(path, "w") as fh:
        fh.write(" ".join("%g" % v for v in values) + "\n")


def write_bvecs(path, array):
    with open(path, "w") as fh:
        for row in array:
            fh.write(" ".join("%.6f" % v for v in row) + "\n")


# ----------------------------------------------------------------- FSL ----

def cmd_fslval(argv):
    img = load(nii(argv[0]))
    shape = list(img.shape) + [1, 1, 1, 1]
    zooms = list(img.header.get_zooms()) + [1, 1, 1, 1]
    key = argv[1]
    lookup = {"dim1": shape[0], "dim2": shape[1], "dim3": shape[2], "dim4": shape[3],
              "pixdim1": zooms[0], "pixdim2": zooms[1], "pixdim3": zooms[2]}
    if key not in lookup:
        sys.exit("stub fslval: unsupported key %s" % key)
    print(lookup[key])


def cmd_fslroi(argv):
    src, dst = nii(argv[0]), nii(argv[1])
    img = load(src)
    data = np.asanyarray(img.dataobj)
    while data.ndim < 4:
        data = data[..., None]
    rest = [int(v) for v in argv[2:]]

    if len(rest) == 2:                       # <tmin> <tsize>
        bounds = [0, -1, 0, -1, 0, -1] + rest
    elif len(rest) == 8:                     # x y z t
        bounds = rest
    else:
        sys.exit("stub fslroi: expected 2 or 8 numeric arguments, got %d" % len(rest))

    slices = []
    for axis in range(4):
        start, size = bounds[2 * axis], bounds[2 * axis + 1]
        stop = data.shape[axis] if size < 0 else start + size
        slices.append(slice(start, stop))
    out = data[tuple(slices)]
    if out.shape[3] == 1:
        out = out[..., 0]
    save(dst, out, img)


def cmd_fslmerge(argv):
    if argv[0] != "-t":
        sys.exit("stub fslmerge: only -t is supported")
    dst, sources = nii(argv[1]), argv[2:]
    volumes = []
    first = load(nii(sources[0]))
    for src in sources:
        data = np.asanyarray(load(nii(src)).dataobj)
        if data.ndim == 3:
            data = data[..., None]
        volumes.append(data)
    save(dst, np.concatenate(volumes, axis=3), first)


def cmd_fslmaths(argv):
    current = data_of(nii(argv[0]))
    reference = load(nii(argv[0]))
    dst = nii(argv[-1])
    i = 1
    while i < len(argv) - 1:
        op = argv[i]
        if op == "-Tmean":
            current = current.mean(axis=3) if current.ndim == 4 else current
            i += 1
        elif op == "-mul":
            other = data_of(nii(argv[i + 1]))
            if other.ndim == 4 and current.ndim == 3:
                current = current[..., None] * other
            else:
                current = current * other
            i += 2
        elif op == "-fillh":
            i += 1                            # no-op: the stub mask has no holes
        elif op == "-bin":
            current = (current > 0).astype(np.float64)
            i += 1
        elif op in ("-thr", "-uthr"):
            value = float(argv[i + 1])
            current = np.where(current >= value, current, 0) if op == "-thr" \
                else np.where(current <= value, current, 0)
            i += 2
        else:
            sys.exit("stub fslmaths: unsupported operation %s" % op)
    save(dst, current, reference)


def cmd_bet(argv):
    src, base = nii(argv[0]), argv[1]
    img = load(src)
    data = np.asanyarray(img.dataobj).astype(np.float64)
    if data.ndim == 4:
        data = data.mean(axis=3)
    # Threshold at a fraction of the robust maximum -- crude, but it yields a
    # connected central blob for the phantom, which is all the pipeline needs.
    mask = (data > 0.25 * np.percentile(data, 98)).astype(np.uint8)
    if "-m" in argv or "-n" in argv:
        save(base + "_mask.nii.gz", mask, img, dtype=np.uint8)
    if "-n" not in argv:
        save(nii(base), data * mask, img)


def cmd_topup(argv):
    options = dict(a.split("=", 1) for a in argv if a.startswith("--") and "=" in a)
    imain = load(nii(options["--imain"]))
    data = np.asanyarray(imain.dataobj).astype(np.float64)
    base = options["--out"]
    save(base + "_fieldcoef.nii.gz", data[..., 0] * 0, imain)
    with open(base + "_movpar.txt", "w") as fh:
        for _ in range(data.shape[3] if data.ndim == 4 else 1):
            fh.write("0 0 0 0 0 0\n")
    if "--iout" in options:
        save(nii(options["--iout"]), data, imain)
    if "--fout" in options:
        save(nii(options["--fout"]), data[..., 0] * 0, imain)


def cmd_eddy(argv):
    options = dict(a.split("=", 1) for a in argv if a.startswith("--") and "=" in a)
    imain = load(nii(options["--imain"]))
    data = np.asanyarray(imain.dataobj)
    base = options["--out"]
    save(nii(base), data, imain)

    # Real eddy records its invocation here; the dry-run harness asserts on it.
    with open(base + ".eddy_command_txt", "w") as fh:
        fh.write(" ".join([os.path.basename(sys.argv[0])] + argv) + "\n")

    bvecs = read_bvecs(options["--bvecs"])
    write_bvecs(base + ".eddy_rotated_bvecs", bvecs)
    n = data.shape[3] if data.ndim == 4 else 1
    with open(base + ".eddy_movement_rms", "w") as fh:
        for i in range(n):
            fh.write("%.4f %.4f\n" % (0.1 + 0.01 * (i % 5), 0.05))
    with open(base + ".eddy_parameters", "w") as fh:
        for _ in range(n):
            fh.write(" ".join(["0"] * 16) + "\n")
    with open(base + ".eddy_outlier_report", "w") as fh:
        fh.write("")


def cmd_dtifit(argv):
    options = dict(a.split("=", 1) for a in argv if a.startswith("--") and "=" in a)
    img = load(nii(options["--data"]))
    data = np.asanyarray(img.dataobj).astype(np.float64)
    mask = data_of(nii(options["--mask"])) > 0
    base = options["--out"]

    b0 = data[..., 0]
    scale = b0 / (b0.max() or 1.0)
    l1 = np.where(mask, 0.0017 * scale + 1e-4, 0)
    l2 = np.where(mask, 0.0006 * scale + 1e-4, 0)
    l3 = np.where(mask, 0.0004 * scale + 1e-4, 0)
    mean = (l1 + l2 + l3) / 3
    numerator = np.sqrt(1.5 * ((l1 - mean) ** 2 + (l2 - mean) ** 2 + (l3 - mean) ** 2))
    denominator = np.sqrt(l1 ** 2 + l2 ** 2 + l3 ** 2)
    fa = np.where(denominator > 0, numerator / np.where(denominator > 0, denominator, 1), 0)

    save(base + "_FA.nii.gz", fa, img)
    save(base + "_MD.nii.gz", mean, img)
    save(base + "_L1.nii.gz", l1, img)
    save(base + "_L2.nii.gz", l2, img)
    save(base + "_L3.nii.gz", l3, img)
    save(base + "_S0.nii.gz", b0, img)
    save(base + "_MO.nii.gz", np.zeros_like(fa), img)
    vector = np.zeros(fa.shape + (3,))
    vector[..., 1] = mask
    save(base + "_V1.nii.gz", vector, img)
    save(base + "_V2.nii.gz", vector, img)
    save(base + "_V3.nii.gz", vector, img)
    if "--save_tensor" in argv:
        save(base + "_tensor.nii.gz", np.repeat(fa[..., None], 6, axis=3), img)


def cmd_eddy_quad(argv):
    outdir = argv[argv.index("--output-dir") + 1] if "--output-dir" in argv else "quad"
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "qc.json"), "w") as fh:
        fh.write('{"stub": true}\n')


# ------------------------------------------------------------- MRtrix3 ----

def positional(argv, flags_with_value, flags_without_value):
    """Split an MRtrix-style argument list into positionals and options."""
    out, options, i = [], {}, 0
    while i < len(argv):
        token = argv[i]
        if token in flags_with_value:
            options.setdefault(token, []).append(argv[i + 1])
            i += 2
        elif token in flags_without_value:
            options[token] = True
            i += 1
        elif token.startswith("-") and token not in ("-",):
            # Unknown flag: assume it takes no value.
            options[token] = True
            i += 1
        else:
            out.append(token)
            i += 1
    return out, options


MRTRIX_VALUE_FLAGS = {"-nthreads", "-noise", "-fslgrad", "-mask", "-shells",
                      "-export_grad_fsl", "-abs", "-voxel", "-interp", "-scale",
                      "-output", "-datatype"}
MRTRIX_BARE_FLAGS = {"-force", "-bzero", "-quiet", "-info", "-nogui"}


def mrtrix_parse(argv):
    args, options, i = [], {}, 0
    while i < len(argv):
        token = argv[i]
        if token == "-fslgrad":
            options["-fslgrad"] = (argv[i + 1], argv[i + 2]); i += 3
        elif token == "-export_grad_fsl":
            options["-export_grad_fsl"] = (argv[i + 1], argv[i + 2]); i += 3
        elif token in MRTRIX_VALUE_FLAGS:
            options[token] = argv[i + 1]; i += 2
        elif token in MRTRIX_BARE_FLAGS:
            options[token] = True; i += 1
        else:
            args.append(token); i += 1
    return args, options


def cmd_passthrough(argv):
    """dwidenoise / mrdegibbs / dwibiascorrect: copy input to output."""
    args, options = mrtrix_parse(argv)
    src, dst = nii(args[-2]), nii(args[-1])
    img = load(src)
    save(dst, np.asanyarray(img.dataobj), img)
    if "-noise" in options:
        data = np.asanyarray(img.dataobj)
        save(nii(options["-noise"]), np.zeros(data.shape[:3]), img)


def cmd_dwibiascorrect(argv):
    """Reject a wrong invocation the way MRtrix3 does.

    This stub used to accept the algorithm as an optional leading word and
    shrug when it was absent.  That made the dry run pass on a call the real
    command rejects, and a real dataset then failed at stage 3 with
    "argument algorithm: invalid choice".  A stub that is more permissive than
    the tool it stands in for is worse than no stub at all.
    """
    algorithms = ("ants", "fsl")
    if not argv or argv[0].startswith("-"):
        sys.stderr.write(
            "Usage: dwibiascorrect algorithm [ options ] ...\n"
            "dwibiascorrect: [ERROR] the following arguments are required: algorithm\n")
        sys.exit(1)
    if argv[0] not in algorithms:
        sys.stderr.write(
            "Usage: dwibiascorrect algorithm [ options ] ...\n"
            "dwibiascorrect: [ERROR] argument algorithm: invalid choice: %r "
            "(choose from %s)\n" % (argv[0], ", ".join(repr(a) for a in algorithms)))
        sys.exit(1)
    cmd_passthrough(argv[1:])


def cmd_dwiextract(argv):
    args, options = mrtrix_parse(argv)
    src, dst = nii(args[-2]), nii(args[-1])
    img = load(src)
    data = np.asanyarray(img.dataobj)
    bvecs_path, bvals_path = options["-fslgrad"]
    bvals, bvecs = read_bvals(bvals_path), read_bvecs(bvecs_path)

    if "-bzero" in options:
        keep = np.where(bvals <= 50)[0]
    elif "-shells" in options:
        wanted = [float(v) for v in options["-shells"].split(",")]
        keep = np.array([i for i, b in enumerate(bvals)
                         if any(abs(b - w) <= 100 for w in wanted)])
    else:
        keep = np.arange(len(bvals))

    save(dst, data[..., keep], img)
    if "-export_grad_fsl" in options:
        out_bvecs, out_bvals = options["-export_grad_fsl"]
        write_bvecs(out_bvecs, bvecs[:, keep])
        write_bvals(out_bvals, bvals[keep])


def cmd_mrcalc(argv):
    """A small RPN evaluator covering the operators this app uses."""
    args, _ = mrtrix_parse(argv)
    dst = nii(args[-1])
    tokens = args[:-1]
    reference = None
    stack = []

    def operand(token):
        nonlocal reference
        try:
            return float(token)
        except ValueError:
            pass
        img = load(nii(token))
        if reference is None:
            reference = img
        return np.asanyarray(img.dataobj).astype(np.float64)

    binary = {
        "-add": lambda a, b: a + b,
        "-subtract": lambda a, b: a - b,
        "-multiply": lambda a, b: a * b,
        "-mult": lambda a, b: a * b,
        "-divide": lambda a, b: a / b,
        "-div": lambda a, b: a / b,
        "-gt": lambda a, b: (a > b).astype(np.float64),
        "-lt": lambda a, b: (a < b).astype(np.float64),
        "-eq": lambda a, b: (a == b).astype(np.float64),
    }

    for token in tokens:
        if token in binary:
            b, a = stack.pop(), stack.pop()
            stack.append(binary[token](a, b))
        elif token == "-if":
            otherwise, then, condition = stack.pop(), stack.pop(), stack.pop()
            stack.append(np.where(condition > 0, then, otherwise))
        elif token == "-round":
            stack.append(np.rint(stack.pop()))
        elif token == "-abs":
            stack.append(np.abs(stack.pop()))
        elif token.startswith("-"):
            sys.exit("stub mrcalc: unsupported operator %s" % token)
        else:
            stack.append(operand(token))

    if len(stack) != 1:
        sys.exit("stub mrcalc: %d values left on the stack" % len(stack))
    save(dst, stack[0], reference)


# ------------------------------------------------------------------ ANTs ----

def cmd_ants_registration(argv):
    options = {}
    i = 0
    while i < len(argv) - 1:
        if argv[i].startswith("-"):
            options[argv[i]] = argv[i + 1]
            i += 2
        else:
            i += 1
    fixed, moving, base = nii(options["-f"]), nii(options["-m"]), options["-o"]
    fixed_img = load(fixed)
    moving_data = np.asanyarray(load(moving).dataobj).astype(np.float64)

    with open(base + "0GenericAffine.mat", "w") as fh:
        fh.write("stub affine\n")
    shape = fixed_img.shape[:3]
    save(base + "1Warp.nii.gz", np.zeros(shape + (1, 3)), fixed_img)
    save(base + "1InverseWarp.nii.gz", np.zeros(shape + (1, 3)), fixed_img)
    save(base + "Warped.nii.gz", resample_nearest(moving_data, shape), fixed_img)


def resample_nearest(data, shape):
    """Nearest-neighbour resample onto a target shape by index scaling."""
    grids = np.meshgrid(*[
        np.clip((np.arange(shape[axis]) * data.shape[axis] / shape[axis]).astype(int),
                0, data.shape[axis] - 1)
        for axis in range(3)
    ], indexing="ij")
    return data[grids[0], grids[1], grids[2]]


def cmd_ants_apply(argv):
    options = {}
    i = 0
    while i < len(argv):
        if argv[i] in ("-i", "-r", "-o", "-n", "-d"):
            options[argv[i]] = argv[i + 1]
            i += 2
        else:
            i += 1
    source = np.asanyarray(load(nii(options["-i"])).dataobj).astype(np.float64)
    reference = load(nii(options["-r"]))
    out = resample_nearest(source, reference.shape[:3])
    save(nii(options["-o"]), out, reference)


DISPATCH = {
    "fslval": cmd_fslval, "fslroi": cmd_fslroi, "fslmerge": cmd_fslmerge,
    "fslmaths": cmd_fslmaths, "bet": cmd_bet, "topup": cmd_topup,
    "dtifit": cmd_dtifit, "eddy_quad": cmd_eddy_quad,
    "dwidenoise": cmd_passthrough, "mrdegibbs": cmd_passthrough,
    "dwibiascorrect": cmd_dwibiascorrect, "dwiextract": cmd_dwiextract,
    "mrcalc": cmd_mrcalc,
    "antsRegistrationSyN.sh": cmd_ants_registration,
    "antsApplyTransforms": cmd_ants_apply,
}


def main():
    name = os.path.basename(sys.argv[0])
    if name.startswith("eddy"):
        return cmd_eddy(sys.argv[1:]) if name != "eddy_quad" else cmd_eddy_quad(sys.argv[1:])
    handler = DISPATCH.get(name)
    if handler is None:
        sys.exit("stub: no implementation for %r" % name)
    handler(sys.argv[1:])


if __name__ == "__main__":
    main()
