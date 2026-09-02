#!/usr/bin/env bash
# Run every benchmark. These are measurements, not tests -- they print numbers
# and never fail a build, because throughput figures are machine-dependent and
# a CI box's noise is not a regression.
#
# The performance *properties* that must hold (bounded buffers, an unblocked
# event loop, batching actually occurring) are asserted in the pytest suites
# instead, where they can be checked without depending on absolute timings.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${VENV:-$here/.venv}/bin/python"

echo "===== Task 3: redaction throughput ====="
"$python" "$here/bench/bench_redactor.py"
echo
echo "===== Task 4: rate limiter ====="
"$python" "$here/bench/bench_limiter.py"
echo
echo "===== Task 4: gateway end to end ====="
for concurrency in 4 16 64; do
    "$python" "$here/bench/bench_gateway.py" "$concurrency" $((concurrency * 40))
done
