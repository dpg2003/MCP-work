"""Refuse to collect more than one project in a single pytest process.

Task 3 and Task 4 each define a top-level ``app.py`` and ``providers.py``.
Within each project those names are right; across them they are irreconcilable,
because ``sys.modules`` is global and the first import of ``providers`` wins for
the whole process.

Collecting both does not merely fail -- it can *silently* bind Task 4's tests to
Task 3's ``app`` module, which is far worse than an error. So a multi-project
run is refused outright and pointed at the supported command instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ASSESSMENT_ROOT = Path(__file__).resolve().parent
PROJECTS = sorted(p.resolve() for p in ASSESSMENT_ROOT.glob("task*") if p.is_dir())

MESSAGE = """
Refusing to collect more than one project in a single pytest process.

These are four independent projects. Two of them define a top-level `app.py`
and `providers.py`; `sys.modules` is global, so one process would bind
whichever copy it imported first and could silently test the wrong module.

Run each suite in its own process:

    {root}/run_all_tests.sh

or run one project directly:

    cd {example} && pytest
""".format(root=ASSESSMENT_ROOT, example=PROJECTS[-1] if PROJECTS else ASSESSMENT_ROOT)


def _projects_in_scope(target: Path) -> set[Path]:
    """Projects a collection rooted at ``target`` would reach.

    A project is in scope if it sits under the target (running from the repo
    root) or if the target sits under it (running a single file or directory
    inside one project).
    """
    return {
        project
        for project in PROJECTS
        if project == target or target in project.parents or project in target.parents
    }


def check(config: pytest.Config) -> None:
    """Raise ``UsageError`` if this invocation spans more than one project."""
    targets = [Path(str(arg).split("::")[0]).resolve() for arg in config.args] or [
        Path(str(config.invocation_params.dir)).resolve()
    ]
    in_scope: set[Path] = set()
    for target in targets:
        in_scope |= _projects_in_scope(target)
    if len(in_scope) > 1:
        raise pytest.UsageError(MESSAGE)
