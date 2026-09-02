"""Task 2 assessment cases: tool filtering and auth on the security gateway."""

from __future__ import annotations

import json
import subprocess

import httpx

from _harness import ASSESSMENT, PYTHON, Runner

PROJECT = ASSESSMENT / "task2-security-gateway"
SECRET = "assessment-matrix-secret"
GATEWAY = "http://127.0.0.1:9500/mcp"
DOWNSTREAM_STATS = "http://127.0.0.1:9501/_control/stats"


def mint(role: str) -> str:
    """Mint a token for ``role`` using the project's own CLI."""
    return subprocess.run(
        [PYTHON, "tokens.py", role], cwd=str(PROJECT), text=True,
        capture_output=True, env={"GATEWAY_TOKEN_SECRET": SECRET, "PATH": "/usr/bin:/bin"},
        check=True,
    ).stdout.strip()


def downstream_calls() -> int:
    """How many requests the downstream server has received."""
    return httpx.get(DOWNSTREAM_STATS, timeout=10).json()["count"]


def post(payload, token: str | None = None, raw: bytes | None = None) -> httpx.Response:
    """POST to the gateway, optionally with a bearer token or a raw body."""
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if raw is not None:
        return httpx.post(GATEWAY, content=raw, headers=headers, timeout=15)
    return httpx.post(GATEWAY, json=payload, headers=headers, timeout=15)


def summarise(response: httpx.Response) -> str:
    """One-line description of a gateway response."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}, non-JSON body"
    if isinstance(body, dict) and "error" in body:
        error = body["error"]
        return f"HTTP {response.status_code}, `{error['code']}` {error['message']}"
    if isinstance(body, dict) and "result" in body:
        result = body["result"]
        if "tools" in result:
            return f"HTTP {response.status_code}, forwarded — {len(result['tools'])} tools listed"
        text = result.get("content", [{}])[0].get("text", "")
        return f"HTTP {response.status_code}, forwarded — {text}"
    return f"HTTP {response.status_code}, {json.dumps(body)[:60]}"


def call(name: str) -> dict:
    """A ``tools/call`` payload for ``name``."""
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}


LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def run(runner: Runner) -> None:
    """Execute cases 2.1 - 2.12 against a live gateway and downstream."""
    runner.serve(PROJECT, "downstream:app", 9501)
    runner.serve(PROJECT, "gateway:app", 9500,
                 {"DOWNSTREAM_URL": "http://127.0.0.1:9501/mcp", "GATEWAY_TOKEN_SECRET": SECRET})
    admin, viewer = mint("admin"), mint("viewer")

    # -- 2.1 / 2.2 ---------------------------------------------------------
    r = post(LIST, viewer)
    runner.record("2.1", "tools/list passthrough", summarise(r),
                  r.status_code == 200 and "result" in r.json())

    r = post(LIST)
    runner.record("2.2", "tools/list with no token", summarise(r) + " (documented: unauthenticated)",
                  r.status_code == 200 and "result" in r.json())

    # -- 2.3 ---------------------------------------------------------------
    r = post(call("admin_reset_key"), admin)
    runner.record("2.3", "Admin tool call, admin role", summarise(r),
                  r.status_code == 200 and "result" in r.json())

    # -- 2.4 / 2.5 / 2.6, with 2.10 measured across all three --------------
    before = downstream_calls()

    r = post(call("admin_reset_key"), viewer)
    runner.record("2.4", "Admin tool call, viewer role", summarise(r),
                  r.json().get("error", {}).get("code") == -32001)

    r = post(call("admin_reset_key"))
    body = r.json()
    runner.record(
        "2.5", "Admin tool call, no token",
        summarise(r) + " (`-32002` = unauthenticated, distinct from `-32001` unauthorized)",
        body.get("error", {}).get("code") in (-32001, -32002) and r.status_code == 401,
    )

    r = post(call("admin_reset_key"), "garbage.token.value")
    runner.record("2.6", "Admin tool call, malformed token",
                  summarise(r) + " (no crash)",
                  r.json().get("error", {}).get("code") in (-32001, -32002))

    after = downstream_calls()
    runner.record("2.10", "Downstream bypassed on rejection",
                  f"downstream request count before={before}, after={after} "
                  f"(delta {after - before}) across cases 2.4/2.5/2.6",
                  after == before)

    # -- 2.7 / 2.8 ---------------------------------------------------------
    for number, token, role in (("2.7", viewer, "viewer"), ("2.8", admin, "admin")):
        r = post(call("get_weather"), token)
        runner.record(number, f"Non-admin tool call, {role} role", summarise(r),
                      r.status_code == 200 and "result" in r.json())

    # -- 2.9 ---------------------------------------------------------------
    before = downstream_calls()
    r = post(call("administrator_tool"), viewer)
    forwarded = downstream_calls() > before
    body = r.json()
    runner.record(
        "2.9", "Tool name edge case — `administrator_tool`",
        f"{summarise(r)}; forwarded downstream: {forwarded}. "
        "Strict `admin_` prefix, so this is NOT treated as privileged; "
        "downstream rejects it as an unknown tool.",
        body.get("error", {}).get("code") == -32601 and forwarded,
    )

    # -- 2.11 --------------------------------------------------------------
    before = downstream_calls()
    r = post(None, admin, raw=b"{ this is not valid json")
    runner.record("2.11", "Malformed JSON-RPC payload",
                  f"{summarise(r)}; forwarded downstream: {downstream_calls() > before}",
                  r.status_code == 400 and r.json()["error"]["code"] == -32700
                  and downstream_calls() == before)

    # -- 2.12 --------------------------------------------------------------
    before = downstream_calls()
    r = post({"jsonrpc": "2.0", "id": 1, "method": "tools/unknown_method"}, admin)
    runner.record(
        "2.12", "Unknown method",
        f"{summarise(r)}; forwarded downstream: {downstream_calls() > before}. "
        "Documented: only `tools/list` and `tools/call` are inspected; anything "
        "else is authenticated then forwarded, so downstream owns the vocabulary.",
        r.status_code == 200 and downstream_calls() > before,
    )
