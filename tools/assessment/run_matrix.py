#!/usr/bin/env python3
"""Run every case in the assessment test-case document and render the results.

Executes all 54 cases against the real implementations -- the MCP server as a
real subprocess over a real pipe, the HTTP services on real sockets, the timeout
cases with real wall-clock latency -- and writes a filled-in markdown matrix.

Usage::

    python tools/assessment/run_matrix.py [output.md]

Default output: ASSESSMENT_RESULTS.md at the repository root.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import REPO, Runner  # noqa: E402

import task1  # noqa: E402
import task2  # noqa: E402
import task3  # noqa: E402
import task4  # noqa: E402

TASKS = [
    ("Task 1: Custom MCP Server (Strict Validation & Transport Handling)", task1, 16),
    ("Task 2: MCP Security Gateway Proxy (Tool Filtering & Auth)", task2, 12),
    ("Task 3: LLM Gateway Streaming Guardrail (PII Redaction)", task3, 12),
    ("Task 4: Rate-Limiting & Model Fallback Router", task4, 14),
]


def sort_key(number: str) -> tuple[int, int]:
    """Order cases numerically, so 4.10 follows 4.9 rather than 4.1."""
    major, _, minor = number.partition(".")
    return int(major), int(minor)


def main(argv: list[str]) -> int:
    """Run every task and write the filled-in matrix."""
    destination = Path(argv[1]) if len(argv) > 1 else REPO / "ASSESSMENT_RESULTS.md"
    started = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    sections: list[tuple[str, list, int]] = []
    for title, module, expected in TASKS:
        runner = Runner()
        print(f"running {title} ...", file=sys.stderr)
        try:
            module.run(runner)
        finally:
            runner.stop()
        cases = sorted(runner.cases, key=lambda c: sort_key(c.number))
        if len(cases) != expected:
            print(f"  WARNING: {len(cases)} cases, document lists {expected}", file=sys.stderr)
        sections.append((title, cases, expected))

    lines = [
        "# FDE Assessment — Measured Test Results",
        "",
        "Every case from the assessment test-case document, executed against the",
        "implementation and filled in automatically.",
        "",
        f"- **Generated:** {started}",
        "- **Regenerate:** `python tools/assessment/run_matrix.py`",
        "- **Scope:** all four tasks, 54 cases — the complete assessment. "
        "(An earlier draft of the source document referenced a fifth task; it was "
        "confirmed not to exist.)",
        "",
        "Nothing here is asserted by hand. The MCP server is driven as a real",
        "subprocess over a real pipe; the HTTP services run on real sockets; the",
        "timeout cases use real wall-clock latency against the documented 3000 ms",
        "threshold. Rows marked `n/a` are informational rather than pass/fail.",
        "",
    ]

    totals = [0, 0]
    for title, cases, _ in sections:
        passed = sum(1 for c in cases if c.passed)
        failed = sum(1 for c in cases if c.passed is False)
        totals[0] += passed
        totals[1] += failed
        lines += [
            "---",
            "",
            f"## {title}",
            "",
            "| # | Test Case | Measured Result | Pass/Fail |",
            "|---|---|---|---|",
        ]
        for case in cases:
            measured = case.measured.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {case.number} | {case.name} | {measured} | {case.verdict} |")
        lines.append("")

    lines += [
        "---",
        "",
        "## Summary Scorecard",
        "",
        "| Task | Total Cases | Passed | Failed |",
        "|---|---|---|---|",
    ]
    for title, cases, _ in sections:
        label = title.split(":")[0]
        lines.append(
            f"| {label} | {len(cases)} | {sum(1 for c in cases if c.passed)} "
            f"| {sum(1 for c in cases if c.passed is False)} |"
        )
    lines.append(
        f"| **Total** | **{sum(len(c) for _, c, _ in sections)}** "
        f"| **{totals[0]}** | **{totals[1]}** |"
    )
    lines.append("")

    destination.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {destination}  ({totals[0]} passed, {totals[1]} failed)", file=sys.stderr)
    return 1 if totals[1] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
