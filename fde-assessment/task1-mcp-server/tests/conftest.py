"""Test harness: drives the MCP server as a real subprocess over stdio.

Nothing here imports the server module in-process. The whole point of the
exercise is the wire behaviour, so every assertion is made against bytes that
actually crossed a pipe.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PROJECT_ROOT / "server.py"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 10.0


class StdioClient:
    """Minimal raw JSON-RPC client speaking newline-delimited frames over stdio.

    Raw on purpose: it will happily send frames the official client SDK would
    refuse to construct (missing ``jsonrpc``, non-JSON garbage, ``NaN``), which
    is exactly what the malformed-input tests need.
    """

    def __init__(self) -> None:
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER_PATH)],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        # Every line stdout ever produced, in order, kept verbatim for the
        # stdout-purity assertions.
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self._responses: dict[Any, dict[str, Any]] = {}
        self._unmatched: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.Condition()
        self._eof = False
        self._next_id = 1000

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # -- plumbing -----------------------------------------------------------
    def _read_stdout(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            with self._lock:
                self.stdout_lines.append(line)
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    # Kept in stdout_lines so the purity test can fail on it.
                    self._lock.notify_all()
                    continue
                if isinstance(message, dict) and "id" in message and message["id"] is not None:
                    self._responses[message["id"]] = message
                else:
                    self._unmatched.put(message)
                self._lock.notify_all()
        with self._lock:
            self._eof = True
            self._lock.notify_all()

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:  # type: ignore[union-attr]
            self.stderr_lines.append(line.rstrip("\n"))

    def send_raw(self, payload: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()

    def send(self, message: dict[str, Any]) -> None:
        self.send_raw(json.dumps(message))

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def wait_for(self, request_id: Any, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        with self._lock:
            if not self._lock.wait_for(
                lambda: request_id in self._responses or self._eof, timeout=timeout
            ):
                raise TimeoutError(f"no response for id={request_id!r}")
            if request_id in self._responses:
                return self._responses[request_id]
        raise TimeoutError(f"server closed stdout before answering id={request_id!r}")

    def wait_for_null_id(self, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        """Await the next response carrying no (or a null) id."""
        return self._unmatched.get(timeout=timeout)

    # -- protocol convenience ----------------------------------------------
    def initialize(self) -> dict[str, Any]:
        request_id = self.next_id()
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pytest-stdio-client", "version": "1.0.0"},
                },
            }
        )
        response = self.wait_for(request_id)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def request(self, method: str, params: Any = None, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        request_id = self.next_id()
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.wait_for(request_id, timeout=timeout)

    def call_tool(self, name: str, arguments: Any = None, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.request("tools/call", params, **kwargs)

    def close(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=5)


@pytest.fixture
def raw_client():
    """A started-but-not-initialized server subprocess."""
    client = StdioClient()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def client(raw_client: StdioClient) -> StdioClient:
    """An initialized server subprocess, ready for tools/* traffic."""
    response = raw_client.initialize()
    assert "result" in response, response
    return raw_client
