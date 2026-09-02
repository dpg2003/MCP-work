"""MCP security gateway: an authenticating JSON-RPC reverse proxy.

Policy
------
``tools/list``  Forwarded transparently to the downstream server, with or
                without a token. Discovery is not privileged: the tool list is
                static public metadata, and refusing it would break every
                client's bootstrap without protecting anything. (Documented
                decision; flip ``REQUIRE_AUTH_FOR_LIST`` to change it.)

``tools/call``  Requires a valid bearer token. If ``params.name`` starts with
                ``admin_`` the principal's role must be exactly ``admin``.
                Unauthorized calls are answered by the gateway itself; the
                downstream server is never contacted.

Anything else   Requires a valid token, then forwarded.

Error codes
-----------
==============================================  =========================  ======
Condition                                        JSON-RPC code              HTTP
==============================================  =========================  ======
Body is not valid JSON                           -32700 Parse error         400
Body is not a JSON-RPC request/batch             -32600 Invalid Request     400
``tools/call`` with missing/invalid ``name``     -32602 Invalid params      200
Missing / malformed / expired / forged token     -32002 Unauthorized        401
Valid token, insufficient role                   -32001 Unauthorized Tool   200
                                                        Call
Downstream unreachable, slow or unparseable      -32003 Upstream error      502
==============================================  =========================  ======

Authentication (*who are you*) is an HTTP-layer failure, so it gets 401 and a
``WWW-Authenticate`` header. Authorization (*you are known but may not do
this*) is an application-layer failure, so it gets a normal 200 carrying a
JSON-RPC error — which is what an MCP client is equipped to parse.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tokens import ADMIN_ROLE, InvalidToken, Principal, parse_authorization_header

logger = logging.getLogger("fde.gateway")

# Tool-name prefix marking a privileged tool. Matched case-sensitively with a
# literal ``str.startswith``:
#   * "admin_reset_key"  -> privileged
#   * "notadmin_reset"   -> NOT privileged (it does not start with the prefix)
#   * "Admin_Reset"      -> NOT privileged, and therefore reachable by a viewer
#
# The case-sensitive choice is deliberate and safe *because* the downstream
# tool namespace is case-sensitive too: "Admin_Reset" is not the name of any
# real tool, so letting it through gets a -32601 from downstream rather than
# privileged access. A case-insensitive check would instead be a footgun —
# it would block a legitimately-named tool like "Administrative_notes".
# See ``_is_privileged`` and the README for the full argument.
ADMIN_TOOL_PREFIX = "admin_"

REQUIRE_AUTH_FOR_LIST = False
UNAUTHENTICATED_METHODS = frozenset({"tools/list"})

# Resource limits. Without these the gateway will happily buffer an arbitrarily
# large body into memory and authorize an arbitrarily long batch -- one
# unauthenticated request is then enough to exhaust a worker.
MAX_BODY_BYTES = int(os.environ.get("GATEWAY_MAX_BODY_BYTES", 1024 * 1024))
MAX_BATCH_SIZE = int(os.environ.get("GATEWAY_MAX_BATCH_SIZE", 100))

# Bound the downstream connection pool so a burst cannot exhaust file
# descriptors, and so backpressure surfaces as queuing rather than collapse.
MAX_DOWNSTREAM_CONNECTIONS = int(os.environ.get("GATEWAY_MAX_CONNECTIONS", 100))
MAX_KEEPALIVE_CONNECTIONS = int(os.environ.get("GATEWAY_MAX_KEEPALIVE", 20))

PAYLOAD_TOO_LARGE = -32600
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
UNAUTHORIZED_TOOL_CALL = -32001
UNAUTHENTICATED = -32002
UPSTREAM_ERROR = -32003

DEFAULT_DOWNSTREAM_URL = "http://127.0.0.1:9001/mcp"
DEFAULT_TIMEOUT_SECONDS = 5.0


# --------------------------------------------------------------------------
# JSON-RPC helpers
# --------------------------------------------------------------------------
def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC error object, omitting ``data`` when there is none."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _extract_id(message: Any) -> Any:
    """Recover a JSON-RPC id from a possibly-malformed message."""
    if isinstance(message, dict):
        value = message.get("id")
        if isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
            return value
    return None


def _looks_like_request(message: Any) -> bool:
    """Whether ``message`` is structurally a JSON-RPC 2.0 request.

    Checked before authorization so a malformed envelope is rejected as an
    invalid request rather than being probed for a ``method`` it may not have.
    """
    return (
        isinstance(message, dict)
        and message.get("jsonrpc") == "2.0"
        and isinstance(message.get("method"), str)
    )


def _is_privileged(tool_name: str) -> bool:
    """Exact, case-sensitive prefix test. No normalisation, no fuzzy matching."""
    return tool_name.startswith(ADMIN_TOOL_PREFIX)


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------
class BodyTooLarge(Exception):
    """The request body exceeded :data:`MAX_BODY_BYTES`."""


async def read_capped_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """Read the body, refusing to buffer more than ``limit`` bytes.

    Two checks, because either alone is insufficient. The ``Content-Length``
    header is rejected up front so an oversized request costs nothing, but a
    chunked request has no such header and a malicious one can understate it --
    so the streamed read is capped independently and stops the moment the cap is
    passed, rather than after the whole body has been buffered.

    Raises:
        BodyTooLarge: The body is, or claims to be, larger than ``limit``.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise BodyTooLarge(declared)
        except ValueError:
            raise BodyTooLarge("invalid content-length") from None

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge(str(size))
        chunks.append(chunk)
    return b"".join(chunks)


class Denied(Exception):
    """A per-message authorization decision to deny."""

    def __init__(self, code: int, message: str, http_status: int, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data


def authorize(message: dict[str, Any], principal: Principal | None,
              auth_failure: str | None) -> None:
    """Raise ``Denied`` if ``message`` must not reach the downstream server.

    ``principal`` is ``None`` when no usable token was presented; in that case
    ``auth_failure`` explains why, for the log and for the client.
    """
    method = message.get("method")

    needs_auth = REQUIRE_AUTH_FOR_LIST or method not in UNAUTHENTICATED_METHODS
    if needs_auth and principal is None:
        raise Denied(
            UNAUTHENTICATED,
            "Unauthorized",
            401,
            {"reason": auth_failure or "no credentials presented"},
        )

    if method != "tools/call":
        return

    params = message.get("params")
    if not isinstance(params, dict):
        raise Denied(INVALID_PARAMS, "Invalid params: params must be an object", 200)

    name = params.get("name")
    # Guard the exact "undefined.startsWith" crash: an absent or non-string
    # name is an invalid request, never a permitted one.
    if not isinstance(name, str) or not name:
        raise Denied(
            INVALID_PARAMS,
            "Invalid params: 'name' must be a non-empty string",
            200,
            {"received": None if name is None else type(name).__name__},
        )

    if _is_privileged(name) and (principal is None or principal.role != ADMIN_ROLE):
        logger.warning(
            "blocked privileged tool call name=%s subject=%s role=%s",
            name,
            getattr(principal, "subject", None),
            getattr(principal, "role", None),
        )
        raise Denied(
            UNAUTHORIZED_TOOL_CALL,
            "Unauthorized Tool Call",
            200,
            {"tool": name, "required_role": ADMIN_ROLE},
        )


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
def create_app(
    downstream_url: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_body_bytes: int = MAX_BODY_BYTES,
    max_batch_size: int = MAX_BATCH_SIZE,
) -> FastAPI:
    """Build the gateway.

    ``client`` is injectable so the tests can wire the gateway straight to the
    downstream ASGI app without binding a port.
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Own an httpx client for the app's lifetime, unless one was injected."""
        if app.state.client is None:
            app.state.client = httpx.AsyncClient(
                timeout=app.state.timeout_seconds,
                limits=httpx.Limits(
                    max_connections=MAX_DOWNSTREAM_CONNECTIONS,
                    max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                ),
            )
        try:
            yield
        finally:
            # Only close a client we own; an injected one belongs to the caller.
            if app.state.injected_client is None and app.state.client is not None:
                await app.state.client.aclose()

    app = FastAPI(title="MCP security gateway", lifespan=lifespan)
    app.state.downstream_url = downstream_url or os.environ.get(
        "DOWNSTREAM_URL", DEFAULT_DOWNSTREAM_URL
    )
    app.state.timeout_seconds = timeout_seconds
    app.state.injected_client = client
    app.state.client = client
    app.state.max_body_bytes = max_body_bytes
    app.state.max_batch_size = max_batch_size

    @app.get("/healthz")
    async def healthz():
        """Liveness probe. Does not touch the downstream server."""
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp(request: Request):
        """Authenticate, authorize each message, forward what is permitted.

        Handles single requests and batches through one code path: a batch is
        just a list of messages, each authorized independently, with the
        locally-rejected results merged back into the downstream responses.
        """
        try:
            raw = await read_capped_body(request, app.state.max_body_bytes)
        except BodyTooLarge as exc:
            logger.warning("rejected oversized body: %s", exc)
            return JSONResponse(
                error_response(
                    None, PAYLOAD_TOO_LARGE, "Invalid Request: payload too large",
                    {"max_bytes": app.state.max_body_bytes},
                ),
                status_code=413,
            )
        try:
            payload = json.loads(raw)
        except ValueError:
            return JSONResponse(error_response(None, PARSE_ERROR, "Parse error"), status_code=400)

        # Authenticate once per HTTP request; the token is a property of the
        # connection, not of an individual message in a batch.
        principal: Principal | None = None
        auth_failure: str | None = None
        try:
            principal = parse_authorization_header(request.headers.get("authorization"))
        except InvalidToken as exc:
            auth_failure = str(exc)

        is_batch = isinstance(payload, list)
        messages = payload if is_batch else [payload]

        if is_batch and not messages:
            return JSONResponse(
                error_response(None, INVALID_REQUEST, "Invalid Request: empty batch"),
                status_code=400,
            )

        if is_batch and len(messages) > app.state.max_batch_size:
            # Authorization is per message, so an unbounded batch is unbounded
            # work for one request. Refuse rather than degrade.
            logger.warning("rejected oversized batch of %d", len(messages))
            return JSONResponse(
                error_response(
                    None, PAYLOAD_TOO_LARGE, "Invalid Request: batch too large",
                    {"max_batch_size": app.state.max_batch_size, "received": len(messages)},
                ),
                status_code=413,
            )

        forward: list[dict[str, Any]] = []
        local_responses: list[dict[str, Any]] = []
        http_status = 200

        for message in messages:
            if not _looks_like_request(message):
                local_responses.append(
                    error_response(_extract_id(message), INVALID_REQUEST, "Invalid Request")
                )
                if not is_batch:
                    http_status = 400
                continue
            try:
                # Each sub-call in a batch is authorized independently.
                authorize(message, principal, auth_failure)
            except Denied as denied:
                local_responses.append(
                    error_response(message.get("id"), denied.code, denied.message, denied.data)
                )
                if not is_batch:
                    http_status = denied.http_status
                continue
            forward.append(message)

        headers = {}
        if http_status == 401:
            headers["WWW-Authenticate"] = 'Bearer realm="mcp-gateway"'

        if not forward:
            body: Any = local_responses if is_batch else (local_responses[0] if local_responses else [])
            return JSONResponse(body, status_code=http_status, headers=headers)

        try:
            upstream = await _forward(app, forward if is_batch else forward[0])
        except _UpstreamFailure as failure:
            # One sanitized error. The upstream's own body never reaches the client.
            logger.error("downstream failure: %s", failure.internal_detail)
            errors = [
                error_response(message.get("id"), UPSTREAM_ERROR, "Upstream server error",
                               {"reason": failure.public_reason})
                for message in forward
            ]
            body = local_responses + errors if is_batch else errors[0]
            return JSONResponse(body, status_code=502)

        if is_batch:
            upstream_list = upstream if isinstance(upstream, list) else [upstream]
            return JSONResponse(local_responses + upstream_list, status_code=200)
        return JSONResponse(upstream, status_code=200)

    return app


class _UpstreamFailure(Exception):
    """A downstream call that failed, split into what may and may not be shared.

    ``public_reason`` is a fixed enum value safe to return to the client;
    ``internal_detail`` carries the status code, exception text or response
    body and goes only to the gateway's log. Keeping them as separate fields
    is what makes it hard to leak the second by accident.
    """

    def __init__(self, public_reason: str, internal_detail: str) -> None:
        super().__init__(public_reason)
        self.public_reason = public_reason
        self.internal_detail = internal_detail


async def _forward(app: FastAPI, payload: Any) -> Any:
    """POST ``payload`` downstream and return its decoded JSON-RPC body."""
    client: httpx.AsyncClient = app.state.client
    try:
        response = await client.post(
            app.state.downstream_url, json=payload, timeout=app.state.timeout_seconds
        )
    except httpx.TimeoutException as exc:
        raise _UpstreamFailure("timeout", f"{type(exc).__name__}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise _UpstreamFailure("unreachable", f"{type(exc).__name__}: {exc}") from exc

    if response.status_code >= 400:
        # Deliberately does not include response.text: upstream error bodies
        # routinely carry stack traces and internal hostnames.
        raise _UpstreamFailure(
            "bad_status", f"downstream returned {response.status_code}: {response.text[:500]!r}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise _UpstreamFailure("invalid_response", f"undecodable body: {response.text[:200]!r}") from exc


app = create_app()
