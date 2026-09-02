"""MCP server over stdio with strict per-tool input validation.

STDOUT PURITY
-------------
stdout carries JSON-RPC frames and nothing else. Two mechanisms enforce it:

1. Logging is configured with a single ``StreamHandler(sys.stderr)``, installed
   on the *root* logger with every other handler removed, so neither this
   module nor any dependency can log to stdout.
2. ``mcp.server.stdio.stdio_server()`` claims fd 1: it duplicates the real pipe
   onto a private descriptor that only the transport writes to, and points fd 1
   itself at stderr for the lifetime of the server. A stray ``print()``
   anywhere in the process therefore lands on stderr and cannot interleave with
   a protocol frame. ``_assert_stdout_claimable()`` verifies the precondition
   for that mechanism (``sys.stdout`` still backed by fd 1) before serving, so
   a mis-wired process fails loudly at startup instead of silently corrupting
   the stream.

ERROR MAPPING
-------------
==========================================  ==========================
Condition                                    JSON-RPC code
==========================================  ==========================
Unknown tool name                            -32601 Method not found
Tool input fails Pydantic validation         -32602 Invalid params
Well-formed id with no record                -32000 Customer not found
Unexpected internal failure                  -32603 Internal error
Frame that is not valid JSON                 -32700 Parse error
Valid JSON, invalid JSON-RPC envelope        -32600 Invalid Request
==========================================  ==========================

``-32000`` is inside the JSON-RPC "implementation-defined server error" range
(-32000..-32099), which is exactly what a domain-level "not found" is. Keeping
it off ``-32602`` lets a client distinguish "you sent me garbage" (retrying is
pointless) from "that customer does not exist" (a different customer may work).
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError

from datastore import DATASTORE, CustomerNotFoundError
from stdio_guard import MalformedFrameReporter
from models import GetCustomerRecordInput, TriggerRefundInput, json_schema_for

SERVER_NAME = "fde-customer-ops"
SERVER_VERSION = "1.0.0"

# JSON-RPC / MCP error codes used by this server.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
CUSTOMER_NOT_FOUND = -32000

logger = logging.getLogger("fde.mcp.server")


def configure_logging(level: int = logging.INFO) -> None:
    """Send every log record to stderr. Never touches stdout."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def _assert_stdout_claimable() -> None:
    """Fail fast if the transport will not be able to claim fd 1.

    ``stdio_server()`` only diverts fd 1 when ``sys.stdout`` is still the real
    stdout. If something upstream has already replaced ``sys.stdout`` with an
    unrelated object, that protection is silently skipped -- which is exactly
    the failure mode this server must never have. Refuse to start instead.
    """
    try:
        backed_by_fd1 = sys.stdout.buffer.fileno() == 1
    except (AttributeError, OSError, ValueError):
        backed_by_fd1 = False
    if not backed_by_fd1:
        raise RuntimeError(
            "sys.stdout is not backed by file descriptor 1; refusing to start "
            "because stdout purity cannot be guaranteed"
        )


TOOLS: list[types.Tool] = [
    types.Tool(
        name="get_customer_record",
        title="Get customer record",
        description="Fetch the stored record for a customer id of the form CUST-XXXXX.",
        input_schema=json_schema_for(GetCustomerRecordInput),
    ),
    types.Tool(
        name="trigger_refund",
        title="Trigger refund",
        description=(
            "Issue a refund against a customer account. Requires a positive finite "
            "amount and a reason of at least 10 non-whitespace characters."
        ),
        input_schema=json_schema_for(TriggerRefundInput),
    ),
]

_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "get_customer_record": GetCustomerRecordInput,
    "trigger_refund": TriggerRefundInput,
}


def _validation_error_data(exc: ValidationError) -> list[dict[str, Any]]:
    """Compact, JSON-safe rendering of Pydantic errors for the ``data`` field.

    Deliberately excludes ``url`` and the raw input value: the client already
    knows what it sent, and echoing arbitrary input back into an error payload
    is a needless amplification vector.
    """
    return [
        {
            "field": ".".join(str(p) for p in err["loc"]) or "<root>",
            "error": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors(include_url=False, include_input=False)
    ]


def _text_result(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        structured_content=payload,
    )


def validate_tool_input(tool_name: str, arguments: dict[str, Any] | None) -> BaseModel:
    """Validate ``arguments`` against the tool's model.

    Raises ``MCPError(-32601)`` for an unknown tool and ``MCPError(-32602)``
    for anything that fails validation.
    """
    model = _INPUT_MODELS.get(tool_name)
    if model is None:
        raise MCPError(code=METHOD_NOT_FOUND, message="Unknown tool", data={"tool": tool_name})

    # Absent arguments validate as ``{}`` so that required-field errors are
    # reported as such rather than as a null-payload crash.
    raw = {} if arguments is None else arguments
    if not isinstance(raw, dict):
        raise MCPError(
            code=INVALID_PARAMS,
            message="Invalid params: arguments must be a JSON object",
            data={"tool": tool_name, "received_type": type(raw).__name__},
        )
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise MCPError(
            code=INVALID_PARAMS,
            message="Invalid params",
            data={"tool": tool_name, "errors": _validation_error_data(exc)},
        ) from exc


async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    logger.info("tools/list requested")
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    tool_name = params.name
    logger.info("tools/call name=%s", tool_name)

    validated = validate_tool_input(tool_name, params.arguments)

    try:
        if isinstance(validated, GetCustomerRecordInput):
            return _text_result(DATASTORE.get_customer(validated.customer_id))

        assert isinstance(validated, TriggerRefundInput)  # only two tools exist
        return _text_result(
            DATASTORE.record_refund(validated.customer_id, validated.amount, validated.reason)
        )
    except CustomerNotFoundError as exc:
        # Distinct from -32602: the request was well-formed, the entity is absent.
        raise MCPError(
            code=CUSTOMER_NOT_FOUND,
            message="Customer not found",
            data={"customer_id": exc.customer_id},
        ) from exc
    except MCPError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        # Never leak an internal traceback over the wire.
        logger.exception("unhandled error in tool %s", tool_name)
        raise MCPError(code=INTERNAL_ERROR, message="Internal server error") from exc


def build_server() -> Server:
    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def main_async() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        logger.info("%s v%s listening on stdio", SERVER_NAME, SERVER_VERSION)
        guarded = MalformedFrameReporter(read_stream, write_stream)
        await server.run(guarded, write_stream, server.create_initialization_options())


def main() -> None:
    configure_logging()
    _assert_stdout_claimable()
    try:
        anyio.run(main_async)
    except (KeyboardInterrupt, EOFError):
        logger.info("shutting down")


if __name__ == "__main__":
    main()
