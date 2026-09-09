#!/bin/bash
# Static checks on the Dockerfile that `bash -n` on its RUN bodies cannot make.
#
# Exists because a real build failed on one of them: an ARG referenced by a FROM
# was declared inside a stage rather than in the global scope, so Docker saw a
# blank base name. Checking only that the ARG line preceded the FROM line was
# not enough -- position relative to the *first* FROM is what matters.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE="$HERE/../Dockerfile"

PASS=0
FAIL=0
check() {  # check <description> <condition-exit-status>
    if [ "$2" -eq 0 ]; then PASS=$((PASS + 1)); else
        FAIL=$((FAIL + 1)); printf 'FAIL: %s\n' "$1" >&2
    fi
}

[ -f "$DOCKERFILE" ] || { echo "no Dockerfile at $DOCKERFILE" >&2; exit 1; }

# 1. Every ARG a FROM refers to must be declared before the first FROM.
first_from_line="$(grep -nE '^FROM ' "$DOCKERFILE" | head -1 | cut -d: -f1)"
from_args="$(grep -oE '^FROM +\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?' "$DOCKERFILE" \
             | sed -E 's/^FROM +\$\{?//; s/\}?$//' | sort -u)"
for arg in $from_args; do
    decl_line="$(grep -nE "^ARG +${arg}(=|$)" "$DOCKERFILE" | head -1 | cut -d: -f1)"
    if [ -z "$decl_line" ]; then
        check "ARG $arg (used in FROM) is declared" 1
    elif [ "$decl_line" -gt "$first_from_line" ]; then
        check "ARG $arg is declared before the first FROM (global scope), not inside a stage" 1
    else
        check "ARG $arg global" 0
    fi
done

# 2. Every stage referenced by COPY --from must exist, or be an external image.
stages="$(grep -oE '^FROM .* AS +[A-Za-z0-9_-]+' "$DOCKERFILE" | awk '{print $NF}')"
for ref in $(grep -oE '^COPY --from=[A-Za-z0-9_.:/-]+' "$DOCKERFILE" | cut -d= -f2 | sort -u); do
    if grep -qx "$ref" <<< "$stages" || [[ "$ref" == *[:/]* ]]; then
        check "COPY --from=$ref resolves" 0
    else
        check "COPY --from=$ref names a defined stage" 1
    fi
done

# 3. Where a self-test exists (the slim variant), it must be the final RUN: it
#    is what proves the trimming did not break anything, so nothing may modify
#    the image after it.
if [ -f "$HERE/../docker/selftest.sh" ]; then
    last_run="$(grep -nE '^RUN ' "$DOCKERFILE" | tail -1)"
    if grep -q 'selftest.sh' <<< "$last_run"; then
        check "selftest.sh is the last RUN" 0
    else
        check "selftest.sh is the last RUN (found instead: ${last_run:0:60})" 1
    fi
fi

if [ $((PASS + FAIL)) -eq 0 ]; then
    printf '%s: no applicable checks (single-stage Dockerfile, no self-test)\n' "$(basename "$0")"
    exit 0
fi
printf '%s: %d passed, %d failed\n' "$(basename "$0")" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
