#!/bin/bash
# collect-binaries.sh <source-prefix> <dest-prefix> <binary> [binary ...]
#
# Copy the named binaries out of a toolchain prefix, together with exactly the
# shared libraries they need *from that same prefix*, and nothing else.
#
# ANTs and MRtrix3 each ship a couple of hundred executables; this app uses a
# handful.  The rest are dead weight in the image.  Rather than guess which
# libraries to keep, ask the dynamic linker: `ldd` resolves each binary's
# dependencies, and anything resolving inside the source prefix is copied.
# Libraries resolving outside it (libc, libstdc++, libgomp, ...) come from the
# base image and are deliberately left alone -- copying those over the base
# image's own copies is how you get subtle ABI breakage.
set -euo pipefail

SRC="${1:?usage: collect-binaries.sh <src-prefix> <dest-prefix> <binary>...}"
DEST="${2:?missing destination prefix}"
shift 2
[ $# -gt 0 ] || { echo "collect-binaries: no binaries named" >&2; exit 1; }

mkdir -p "$DEST/bin" "$DEST/lib"

# The dependency list is accumulated in a file rather than piped, so that the
# missing-binary counter survives in this shell instead of dying in a subshell.
libs_file="$(mktemp)"
trap 'rm -f "$libs_file"' EXIT

missing=0
copied=0
for name in "$@"; do
    src_path=""
    for dir in "$SRC/bin" "$SRC"; do
        if [ -f "$dir/$name" ]; then src_path="$dir/$name"; break; fi
    done
    if [ -z "$src_path" ]; then
        echo "collect-binaries: NOT FOUND in $SRC: $name" >&2
        missing=$((missing + 1))
        continue
    fi
    cp -a "$src_path" "$DEST/bin/$name"
    copied=$((copied + 1))

    # Shell wrappers have no ELF dependencies; ldd on them is meaningless.
    if file -b "$src_path" | grep -q ELF; then
        ldd "$src_path" 2>/dev/null | awk '{print $3}' | grep -E "^${SRC}/" >> "$libs_file" || true
    fi
done

while read -r lib; do
    [ -n "$lib" ] && [ -f "$lib" ] || continue
    cp -aL "$lib" "$DEST/lib/" 2>/dev/null || true
done < <(sort -u "$libs_file")

if [ "$missing" -ne 0 ]; then
    echo "collect-binaries: $missing binary/binaries not found in $SRC -- refusing to build a broken image" >&2
    exit 1
fi

echo "collect-binaries: $copied binaries + $(find "$DEST/lib" -type f | wc -l) libraries -> $DEST"
du -sh "$DEST" 2>/dev/null || true
