#!/usr/bin/env python3
"""Validate the editor, dev-container, and MCP client configuration.

These files are not exercised by any test suite, so without a check they rot
silently: a renamed script or a moved module leaves a task that fails only when
someone tries to use it. This parses each config (VS Code accepts JSON with
comments, so plain ``json.load`` is not enough), then verifies that every path
and module they reference actually exists.

Usage::

    python tools/check_configs.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CONFIGS = [
    ".vscode/settings.json",
    ".vscode/tasks.json",
    ".vscode/launch.json",
    ".vscode/extensions.json",
    ".vscode/mcp.json",
    ".devcontainer/devcontainer.json",
    "examples/claude_desktop_config.json",
]


def strip_jsonc(text: str) -> str:
    """Remove ``//`` comments that are not inside a string literal.

    VS Code's config files are JSONC. A naive regex would corrupt any URL
    containing ``//``, so this walks the text tracking string state.
    """
    out: list[str] = []
    in_string = escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] == "/":
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def load(path: Path) -> dict:
    """Parse a JSON or JSONC config file."""
    return json.loads(strip_jsonc(path.read_text(encoding="utf-8")))


def referenced_paths(text: str) -> set[Path]:
    """Repo-relative paths a config mentions via ``${workspaceFolder}``."""
    found = set()
    for match in re.finditer(r"\$\{workspaceFolder\}/([A-Za-z0-9._/\-]+)", text):
        candidate = match.group(1)
        # Skip things created at runtime rather than committed.
        if candidate.startswith(".venv/") or candidate.endswith(".sqlite3"):
            continue
        found.add(REPO / candidate)
    return found


def check() -> list[str]:
    """Return a list of problems; empty means everything is consistent."""
    problems: list[str] = []

    for relative in CONFIGS:
        path = REPO / relative
        if not path.is_file():
            problems.append(f"{relative}: missing")
            continue
        try:
            config = load(path)
        except json.JSONDecodeError as exc:
            problems.append(f"{relative}: invalid JSON ({exc})")
            continue

        for referenced in sorted(referenced_paths(path.read_text(encoding="utf-8"))):
            if not referenced.exists():
                problems.append(f"{relative}: references missing path {referenced.relative_to(REPO)}")

        # Every task/launch `cwd` must be a real directory.
        for entry in config.get("tasks", []) + config.get("configurations", []):
            cwd = (entry.get("options", {}) or {}).get("cwd") or entry.get("cwd")
            if cwd:
                resolved = REPO / cwd.replace("${workspaceFolder}/", "")
                if not resolved.is_dir():
                    problems.append(f"{relative}: '{entry.get('label') or entry.get('name')}' cwd not found: {cwd}")

    # The MCP server the client configs point at must exist and be runnable.
    server = REPO / "fde-assessment/task1-mcp-server/server.py"
    if not server.is_file():
        problems.append(f"MCP server missing: {server.relative_to(REPO)}")

    # Scripts referenced by tasks must be executable.
    for script in ("setup.sh", "run_tests.sh", "bench/run_all.sh", "tools/demo.sh",
                   "fde-assessment/run_all_tests.sh"):
        path = REPO / script
        if not path.is_file():
            problems.append(f"script missing: {script}")
        elif not path.stat().st_mode & 0o111:
            problems.append(f"script not executable: {script}")

    return problems


def main() -> int:
    """Print the result and exit non-zero if any config is inconsistent."""
    problems = check()
    if problems:
        print("configuration check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"configuration check passed ({len(CONFIGS)} configs, all paths resolve)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
