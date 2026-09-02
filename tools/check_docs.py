#!/usr/bin/env python3
"""Documentation-coverage checker.

Enforces the project's documentation standard, so "everything is documented"
is a build step rather than a claim:

* **Source modules** (everything that is not a test) must have a docstring on
  every module, class, function, method, and property. This is enforced -- a
  gap fails the check.
* **Test support code** -- fixtures, helpers, and any non-``test_`` function in
  a test module -- must also be documented, because those names describe
  plumbing rather than behaviour.
* **Test functions themselves** are reported but not enforced. A well-named
  test is its own specification (``test_exactly_the_limit_is_allowed_and_one
  _more_is_not`` needs no prose), and a docstring that restates the function
  name is noise. Docstrings are added to the tests whose intent is not obvious
  from the name alone.

Dunder methods with no behaviour of their own (``__aiter__``, ``__aenter__``
and friends implementing a stdlib protocol) are exempt: the protocol is the
documentation, and the class docstring says which protocol.

Usage::

    python tools/check_docs.py [root]      # default root: fde-assessment
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Protocol dunders whose meaning is fixed by the language, not by us.
EXEMPT_DUNDERS = {
    "__init__", "__aiter__", "__anext__", "__aenter__", "__aexit__",
    "__iter__", "__next__", "__enter__", "__exit__", "__call__", "__str__",
    "__repr__", "__eq__", "__hash__", "__len__",
}


@dataclass
class Report:
    """Counts and gap lists for one category of code."""

    label: str
    documented: int = 0
    total: int = 0
    gaps: list[str] = field(default_factory=list)

    def record(self, ok: bool, where: str) -> None:
        """Register one inspected definition."""
        self.total += 1
        if ok:
            self.documented += 1
        else:
            self.gaps.append(where)

    @property
    def percent(self) -> float:
        """Documented share, as a percentage. 100.0 when there is nothing to check."""
        return 100.0 if not self.total else 100.0 * self.documented / self.total


def is_test_module(path: Path) -> bool:
    """Whether ``path`` is test code rather than shipped source."""
    return path.name.startswith("test_") or path.name == "conftest.py" or "tests" in path.parts


def check(root: Path) -> tuple[Report, Report, Report]:
    """Walk ``root`` and classify every definition into the three reports."""
    source = Report("source")
    support = Report("test support")
    tests = Report("test functions")

    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        testish = is_test_module(path)
        target = support if testish else source
        target.record(bool(ast.get_docstring(tree)), f"{path}: module")

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                name, kind = node.name, "class"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name, kind = node.name, "function"
                if name in EXEMPT_DUNDERS:
                    continue
            else:
                continue
            documented = bool(ast.get_docstring(node))
            where = f"{path}:{node.lineno}: {kind} {name}"
            if testish and name.startswith("test_"):
                tests.record(documented, where)
            else:
                target.record(documented, where)

    return source, support, tests


def main(argv: list[str]) -> int:
    """Print the report and exit non-zero if an enforced category has gaps."""
    root = Path(argv[1] if len(argv) > 1 else "fde-assessment")
    if not root.is_dir():
        print(f"no such directory: {root}", file=sys.stderr)
        return 2

    source, support, tests = check(root)
    for report in (source, support, tests):
        enforced = report is not tests
        print(
            f"{report.label:15} {report.documented:4}/{report.total:4} "
            f"({report.percent:5.1f}%){'  [enforced]' if enforced else '  [reported]'}"
        )

    failures = [r for r in (source, support) if r.gaps]
    if not failures:
        if tests.gaps:
            print(f"\n{len(tests.gaps)} test functions rely on their name alone (allowed).")
        print("\ndocumentation check passed")
        return 0

    for report in failures:
        print(f"\nundocumented {report.label}:", file=sys.stderr)
        for gap in report.gaps:
            print(f"  {gap}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
