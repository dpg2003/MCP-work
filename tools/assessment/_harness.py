"""Shared plumbing for the assessment matrix runner.

Each task module exposes ``run()`` returning a list of :class:`Case`. The runner
executes them against the real implementations -- a real subprocess for the MCP
server, real sockets for the HTTP services -- and renders the filled-in table.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
ASSESSMENT = REPO / "fde-assessment"
PYTHON = str(REPO / ".venv" / "bin" / "python")


@dataclass
class Case:
    """One row of the assessment matrix."""

    number: str
    name: str
    measured: str
    passed: bool | None            # None -> informational, no pass/fail claimed
    note: str = ""

    @property
    def verdict(self) -> str:
        """Rendered Pass/Fail cell."""
        if self.passed is None:
            return "n/a"
        return "**Pass**" if self.passed else "**FAIL**"


@dataclass
class Runner:
    """Collects cases and owns any subprocesses a task needs."""

    cases: list[Case] = field(default_factory=list)
    procs: list[subprocess.Popen] = field(default_factory=list)

    def record(self, number: str, name: str, measured: str,
               passed: bool | None, note: str = "") -> None:
        """Add one measured result."""
        self.cases.append(Case(number, name, str(measured), passed, note))

    def serve(self, cwd: Path, target: str, port: int, env: dict | None = None) -> None:
        """Start a uvicorn app and wait until it answers."""
        environment = {**os.environ, **(env or {})}
        proc = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", target, "--port", str(port), "--log-level", "warning"],
            cwd=str(cwd), env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        deadline = time.time() + 30
        while time.time() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    time.sleep(0.4)
                    return
            time.sleep(0.1)
        raise RuntimeError(f"{target} never came up on port {port}")

    def stop(self) -> None:
        """Terminate everything this runner started."""
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
        self.procs.clear()


class StdioMCP:
    """Minimal raw JSON-RPC client driving the Task 1 server as a subprocess."""

    def __init__(self) -> None:
        """Launch the server and complete the MCP handshake."""
        self.proc = subprocess.Popen(
            [PYTHON, "server.py"],
            cwd=str(ASSESSMENT / "task1-mcp-server"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self.stdout_lines: list[str] = []
        self._id = 0
        self.handshake = self.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {},
             "clientInfo": {"name": "matrix", "version": "1.0"}},
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, message: dict) -> None:
        """Write one JSON-RPC frame."""
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def send_raw(self, text: str) -> None:
        """Write a raw line, bypassing JSON encoding."""
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def read(self) -> dict:
        """Read one response frame, recording it for the stdout-purity check."""
        line = self.proc.stdout.readline()
        self.stdout_lines.append(line.rstrip("\n"))
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        """Send a request and return its response."""
        self._id += 1
        message = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.read()

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """Invoke a tool and return the raw JSON-RPC response."""
        params: dict = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.request("tools/call", params)

    def close(self) -> str:
        """Shut the server down and return everything it wrote to stderr."""
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # pragma: no cover
            self.proc.kill()
        return self.proc.stderr.read()


def describe(response: dict) -> str:
    """One-line summary of a JSON-RPC response, for the Measured column."""
    if "error" in response:
        error = response["error"]
        return f"`{error['code']}` {error['message']}"
    result = response.get("result", {})
    if "structuredContent" in result:
        content = result["structuredContent"]
        if "name" in content:
            return f"OK — record for {content['customer_id']} ({content['name']})"
        if "refund_id" in content:
            return f"OK — refund {content['refund_id']}, amount {content['amount']}"
        return f"OK — {json.dumps(content)[:70]}"
    if "tools" in result:
        return "OK — " + ", ".join(t["name"] for t in result["tools"])
    return "OK"
