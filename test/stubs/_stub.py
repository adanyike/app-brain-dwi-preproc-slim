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

import json
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

    # Reject the invalid combinations the real eddy rejects.  Group-wise outlier
    # detection and slice-to-volume correction both need the multiband
    # structure, and eddy exits rather than guessing at it.
    has_mb_structure = "--slspec" in options or "--mb" in options
    if options.get("--ol_type") in ("both", "gw") and not has_mb_structure:
        sys.exit("EddyInputError:  --ol_type indicating mb-groups without "
                 "providing mb structure\nTerminating program")
    if "--mporder" in options and not has_mb_structure:
        sys.exit("EddyInputError:  --mporder specified without providing mb "
                 "structure\nTerminating program")

    imain = load(nii(options["--imain"]))
    data = np.asanyarray(imain.dataobj)
    base = options["--out"]
    save(nii(base), data, imain)

    # Real eddy records its invocation here; the dry-run harness asserts on it.
    with open(base + ".eddy_command_txt", "w") as fh:
        fh.write(" ".join([os.path.basename(sys.argv[0])] + argv) + "\n")

    # Real eddy renormalises each direction after rotating it, so a volume
    # recorded as 0 0 0 -- every unweighted one -- comes back as NaN.
    bvecs = read_bvecs(options["--bvecs"])
    with np.errstate(invalid="ignore", divide="ignore"):
        norms = np.linalg.norm(bvecs, axis=0)
        rotated = bvecs / norms
    write_bvecs(base + ".eddy_rotated_bvecs", rotated)
    n = data.shape[3] if data.ndim == 4 else 1
    with open(base + ".eddy_movement_rms", "w") as fh:
        for i in range(n):
            fh.write("%.4f %.4f\n" % (0.1 + 0.01 * (i % 5), 0.05))
    with open(base + ".eddy_parameters", "w") as fh:
        for _ in range(n):
            fh.write(" ".join(["0"] * 16) + "\n")

    # Which of these exist is what eddy_quad keys its QC metrics off, and so
    # what decides whether subjects can be pooled by eddy_squad later.
    if "--repol" in argv:
        with open(base + ".eddy_outlier_report", "w") as fh:
            fh.write("")
        with open(base + ".eddy_outlier_map", "w") as fh:
            fh.write("")
    if "--mporder" in options:
        with open(base + ".eddy_movement_over_time", "w") as fh:
            for _ in range(n):
                fh.write(" ".join(["0"] * 6) + "\n")
    if "--cnr_maps" in argv:
        save(base + ".eddy_cnr_maps.nii.gz", np.asanyarray(data)[..., :1], imain)
    if "--residuals" in argv:
        save(base + ".eddy_residuals.nii.gz", data, imain)


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


def option(argv, *names):
    """The value of the first of `names` present in argv, or None."""
    for name in names:
        if name in argv:
            index = argv.index(name)
            if index + 1 < len(argv):
                return argv[index + 1]
        for item in argv:
            if item.startswith(name + "="):
                return item.split("=", 1)[1]
    return None


def cmd_eddy_quad(argv):
    """Write the database real QUAD writes, with the flags the arguments imply.

    The group side turns on exactly these flags, so a stub that always wrote the
    same qc.json would make the cohort logic untestable.  Which metrics QUAD can
    compute is decided by which eddy outputs exist, so that is what is checked
    here: a CNR map on disk means CNR metrics, --field means the susceptibility
    field, and so on.
    """
    outdir = option(argv, "--output-dir", "-o") or "quad"
    os.makedirs(outdir, exist_ok=True)
    base = argv[0] if argv and not argv[0].startswith("-") else ""
    bvals = read_bvals(option(argv, "--bvals", "-b")) if option(argv, "--bvals", "-b") else np.zeros(1)

    b0 = bvals < 50
    shells = sorted({int(round(b / 50.0)) * 50 for b in bvals[~b0]})
    # QUAD derives the flags from eddy's output, not from its own arguments.
    has_s2v = os.path.exists(base + ".eddy_movement_over_time")
    has_ol = os.path.exists(base + ".eddy_outlier_report")
    has_cnr = os.path.exists(base + ".eddy_cnr_maps.nii.gz")
    has_rss = os.path.exists(base + ".eddy_residuals.nii.gz")
    has_field = option(argv, "--field", "-f") is not None

    acqp = option(argv, "--eddyParams", "-par")
    n_pe = 1
    if acqp and os.path.exists(acqp):
        with open(acqp) as fh:
            n_pe = len({line.strip() for line in fh if line.strip()}) or 1

    qc = {
        "data_file_eddy": base,
        "data_no_dw_vols": int((~b0).sum()),
        "data_no_b0_vols": int(b0.sum()),
        "data_no_PE_dirs": n_pe,
        "data_no_shells": len(shells),
        "data_unique_bvals": shells,
        "data_vox_size": [2.0, 2.0, 2.0],
        "data_protocol": [[b, int((bvals == b).sum())] for b in shells],
        "qc_mot_abs": 0.42,
        "qc_mot_rel": 0.19,
        "qc_params_flag": True,
        "qc_params_avg": [0.0] * 9,
        "qc_s2v_params_flag": has_s2v,
        "qc_field_flag": has_field,
        "qc_ol_flag": has_ol,
        "qc_outliers_tot": 1.25,
        "qc_outliers_b": [1.0] * len(shells),
        "qc_outliers_pe": [1.0] * n_pe,
        "qc_cnr_flag": has_cnr,
        "qc_cnr_avg": [12.0] + [2.5] * len(shells) if has_cnr else [],
        "qc_cnr_std": [1.0] + [0.3] * len(shells) if has_cnr else [],
        "qc_rss_flag": has_rss,
    }
    if has_s2v:
        qc["qc_s2v_params_avg_std"] = [0.0] * 6
    if has_field:
        qc["qc_vox_displ_std"] = 0.8
    with open(os.path.join(outdir, "qc.json"), "w") as fh:
        json.dump(qc, fh, indent=4, sort_keys=True)
    with open(os.path.join(outdir, "qc.pdf"), "w") as fh:
        fh.write("%PDF-1.4 stub single-subject report\n")


def cmd_eddy_squad(argv):
    """Stand in for eddy_squad: same contract, none of the plotting.

    It reproduces the two behaviours the group stage depends on -- the refusal
    when the subjects' eddy flags disagree, and the group_db schema, where each
    QC index is one row per subject in list order -- plus --update writing
    qc_updated.pdf back into each listed folder.
    """
    flags = [a for a in argv if a.startswith("-")]
    outdir = option(argv, "--output-dir", "-o") or "squad"
    grouping = option(argv, "--grouping", "-g")
    # Real eddy_squad parses with argparse, where --update is nargs="?" -- so a
    # token following it is swallowed as its value, and a caller who puts the
    # subject list after --update loses it.  Model that, or the argument order
    # this depends on is untested.
    update_value = option(argv, "--update", "-u")
    consumed = {option(argv, "--output-dir", "-o"), grouping, update_value}
    positional = [a for a in argv if not a.startswith("-") and a not in consumed]
    if not positional:
        sys.exit("eddy_squad: no subject list given (argparse: --update swallowed it?)")

    with open(positional[0]) as fh:
        folders = [line.strip() for line in fh if line.strip()]
    if not folders:
        sys.exit("eddy_squad: the subject list is empty")

    databases = []
    for folder in folders:
        path = os.path.join(folder, "qc.json")
        if not os.path.isfile(path):
            sys.exit("eddy_squad: %s does not contain a qc.json" % folder)
        with open(path) as fh:
            databases.append(json.load(fh))

    # The check that makes this whole exercise necessary: squad_db.py compares
    # these flags across subjects and raises before writing anything.
    names = ("qc_params_flag", "qc_s2v_params_flag", "qc_field_flag",
             "qc_ol_flag", "qc_cnr_flag", "qc_rss_flag")
    for name in names:
        if len({bool(db.get(name)) for db in databases}) > 1:
            sys.exit("ValueError: Eddy output inconsistency detected!")

    if grouping is not None:
        with open(grouping) as fh:
            lines = [line.strip() for line in fh if line.strip()]
        if len(lines) - 2 != len(folders):
            sys.exit("eddy_squad: the grouping variable has %d values for %d subjects"
                     % (len(lines) - 2, len(folders)))

    first = databases[0]
    os.makedirs(outdir, exist_ok=True)
    group = {
        "data_no_subjects": len(databases),
        "data_no_shells": first.get("data_no_shells"),
        "data_no_pes": first.get("data_no_PE_dirs"),
        "data_no_b0_vols": first.get("data_no_b0_vols"),
        "data_no_dw_vols": first.get("data_no_dw_vols"),
        "data_unique_bvals": first.get("data_unique_bvals"),
        "data_vox_size": first.get("data_vox_size"),
        "ol_flag": bool(first.get("qc_ol_flag")),
        "par_flag": bool(first.get("qc_params_flag")),
        "s2v_par_flag": bool(first.get("qc_s2v_params_flag")),
        "susc_flag": bool(first.get("qc_field_flag")),
        "cnr_flag": bool(first.get("qc_cnr_flag")),
        "rss_flag": bool(first.get("qc_rss_flag")),
        "qc_motion": [[db.get("qc_mot_abs"), db.get("qc_mot_rel")] for db in databases],
        "qc_outliers": [[db.get("qc_outliers_tot")] + list(db.get("qc_outliers_b") or [])
                        + list(db.get("qc_outliers_pe") or []) for db in databases],
        "qc_cnr": [list(db.get("qc_cnr_avg") or []) for db in databases],
    }
    with open(os.path.join(outdir, "group_db.json"), "w") as fh:
        json.dump(group, fh, indent=4, sort_keys=True)
    with open(os.path.join(outdir, "group_qc.pdf"), "w") as fh:
        fh.write("%%PDF-1.4 stub group report for %d subjects\n" % len(databases))

    if "-u" in flags or "--update" in flags:
        for folder in folders:
            with open(os.path.join(folder, "qc_updated.pdf"), "w") as fh:
                fh.write("%PDF-1.4 stub updated report\n")


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

    The algorithm is required and must be one MRtrix3 knows.  A stub that is
    more permissive than the tool it stands in for is worse than no stub at
    all: the dry run passes on a call the real command rejects.
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
    "eddy_squad": cmd_eddy_squad,
    "dwidenoise": cmd_passthrough, "mrdegibbs": cmd_passthrough,
    "dwibiascorrect": cmd_dwibiascorrect, "dwiextract": cmd_dwiextract,
    "mrcalc": cmd_mrcalc,
    "antsRegistrationSyN.sh": cmd_ants_registration,
    "antsApplyTransforms": cmd_ants_apply,
}


def main():
    name = os.path.basename(sys.argv[0])
    # Every eddy* name that is not one of the QC tools is eddy itself: the
    # pipeline picks between eddy_openmp, eddy_cpu, eddy and eddy_cuda*.
    handler = DISPATCH.get(name)
    if handler is None and name.startswith("eddy"):
        handler = cmd_eddy
    if handler is None:
        sys.exit("stub: no implementation for %r" % name)
    handler(sys.argv[1:])


if __name__ == "__main__":
    main()
