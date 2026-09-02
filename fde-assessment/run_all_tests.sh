#!/usr/bin/env bash
# Run each project's test suite in its own process.
#
# These are four independent projects that happen to share a repository. Two of
# them legitimately define a top-level `app.py` and a top-level `providers.py`,
# which is correct within each project and unresolvable across them: Python's
# sys.modules is global, so a single pytest process collecting all four would
# bind whichever copy it imported first. Separate processes are the fix, and
# they also honour each project's own pytest.ini and dependency set.
set -uo pipefail

PYTHON="${PYTHON:-python}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
failed=()

for project in "$here"/task*/; do
    name="$(basename "$project")"
    printf '\n===== %s =====\n' "$name"
    if ! (cd "$project" && "$PYTHON" -m pytest "$@"); then
        failed+=("$name")
    fi
done

printf '\n===== summary =====\n'
if [ ${#failed[@]} -eq 0 ]; then
    echo "all suites passed"
    exit 0
fi
printf 'FAILED: %s\n' "${failed[*]}"
exit 1
