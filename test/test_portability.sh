#!/bin/bash
# Guard against bash 4+ syntax in the shell sources.
#
# macOS still ships bash 3.2 as /bin/bash, and `main` (plus run.sh under
# SKIP_CONTAINER=1, plus this whole test suite) runs on the host, not in the
# container.  A bash-4-only construct therefore works everywhere it was
# developed and fails on a Mac, with an error -- "bad substitution" -- that says
# nothing about the cause.
#
# This is a static check: it cannot run bash 3.2 to prove the scripts work, only
# refuse the constructs known to break it.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$(cd "$HERE/.." && pwd)"

PASS=0
FAIL=0
check() {  # check <description> <pattern> <explanation>
    local desc="$1" pattern="$2" why="$3" hits
    # Skip this file (its own patterns would match) and drop comment-only
    # lines, so a construct can still be named in prose where it is explained.
    hits="$(cd "$APP" && grep -rnE "$pattern" \
              --include='*.sh' --include='main' \
              src test docker main run.sh 2>/dev/null \
            | grep -v '^test/test_portability\.sh:' \
            | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#' || true)"
    if [ -z "$hits" ]; then
        PASS=$((PASS + 1)); printf '  ok   -- %s\n' "$desc"
    else
        FAIL=$((FAIL + 1)); printf '  FAIL -- %s (%s)\n' "$desc" "$why"
        printf '%s\n' "$hits" | sed 's/^/           /'
    fi
}

check "no \${x,,} / \${x^^} case conversion" \
      '\$\{[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?(,,|\^\^|,|\^)\}' \
      'bash 4.0+; use the lower() helper in src/common.sh'

check "no associative arrays" \
      '(declare|local|typeset)[[:space:]]+-[A-Za-z]*A' \
      'bash 4.0+; use an indexed array'

check "no mapfile / readarray" \
      '\b(mapfile|readarray)\b' \
      'bash 4.0+; use a while-read loop'

check "no ;;& case fallthrough" \
      ';;&' \
      'bash 4.0+'

check "no |& shorthand" \
      '\|&' \
      'bash 4.0+; use 2>&1 |'

check "no negative array indices" \
      '\$\{[A-Za-z_][A-Za-z0-9_]*\[-' \
      'bash 4.2+; use ${#arr[@]} arithmetic'

check "no wait -n" \
      '\bwait[[:space:]]+-n\b' \
      'bash 4.3+'

printf 'test_portability.sh: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
