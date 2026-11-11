#!/usr/bin/env bash
# One-command setup: create a virtualenv and install every project's deps.
#
# The four projects have overlapping but distinct requirements files. They are
# installed into one virtualenv here for convenience; each project also pins
# its own requirements.txt so it can be set up standalone.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv="${VENV:-$here/.venv}"
python_bin="${PYTHON_BIN:-python3}"

if [ ! -d "$venv" ]; then
    echo "creating virtualenv at $venv"
    "$python_bin" -m venv "$venv"
fi

"$venv/bin/pip" install --quiet --upgrade pip
for requirements in "$here"/fde-assessment/task*/requirements.txt; do
    echo "installing $(basename "$(dirname "$requirements")")"
    "$venv/bin/pip" install --quiet -r "$requirements"
done

cat <<DONE

setup complete.

  run all tests:   ./run_tests.sh
  check docs:      $venv/bin/python tools/check_docs.py
  one project:     cd fde-assessment/task1-mcp-server && $venv/bin/python -m pytest

DONE
