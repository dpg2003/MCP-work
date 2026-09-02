#!/usr/bin/env bash
# Run every project's test suite, using the virtualenv created by ./setup.sh.
#
# Also runs the documentation-coverage check and the editor/MCP configuration
# check, so "everything is documented" and "the VS Code tasks still point at
# real paths" are verified rather than asserted. Extra arguments are passed
# through to pytest.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv="${VENV:-$here/.venv}"

if [ ! -x "$venv/bin/python" ]; then
    echo "no virtualenv at $venv -- run ./setup.sh first" >&2
    exit 2
fi

echo "===== documentation coverage ====="
"$venv/bin/python" "$here/tools/check_docs.py" "$here/fde-assessment" || exit 1

echo
echo "===== editor / MCP configuration ====="
"$venv/bin/python" "$here/tools/check_configs.py" || exit 1

PYTHON="$venv/bin/python" "$here/fde-assessment/run_all_tests.sh" "$@"
