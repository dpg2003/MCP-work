#!/usr/bin/env bash
# Run every benchmark. These are measurements, not tests -- they print numbers,
# because throughput figures are machine-dependent and a CI box's noise is not a
# regression.
#
# The performance *properties* that must hold (bounded buffers, an unblocked
# event loop, batching actually occurring) are asserted in the pytest suites
# instead, where they can be checked without depending on absolute timings.
#
# A benchmark that fails to *run*, however, is a real error: this script exits
# non-zero so a crashed benchmark can never be mistaken for a clean run.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${VENV:-$here/.venv}/bin/python"

if [ ! -x "$python" ]; then
    echo "no virtualenv at $(dirname "$(dirname "$python")") -- run ./setup.sh first" >&2
    exit 2
fi

failed=()
run() {
    local label="$1"; shift
    echo "===== $label ====="
    if ! "$@"; then
        failed+=("$label")
    fi
    echo
}

run "Task 3: redaction throughput" "$python" "$here/bench/bench_redactor.py"
run "Task 4: rate limiter" "$python" "$here/bench/bench_limiter.py"
for concurrency in 4 16 64; do
    run "Task 4: gateway end to end (concurrency $concurrency)" \
        "$python" "$here/bench/bench_gateway.py" "$concurrency" $((concurrency * 40))
done

if [ ${#failed[@]} -ne 0 ]; then
    printf 'BENCHMARKS FAILED TO RUN: %s\n' "${failed[*]}" >&2
    exit 1
fi
echo "all benchmarks completed"
