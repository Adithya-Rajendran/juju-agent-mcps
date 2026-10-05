"""MCP client for the juju-docs / juju-ops / cos-metrics HTTP services.

Routes autoscaler tool calls through the running systemd services
(juju-docs-mcp / juju-ops-mcp / cos-metrics-mcp, started with
``--transport streamable-http``) instead of importing the Python packages
directly. The autoscaler then exercises the same MCP surface as any other
client and validates the production path on every tick: if the MCP path is
broken, it fails loudly instead of silently bypassing it via in-process
imports.

Each service speaks MCP over streamable HTTP at
``http://127.0.0.1:<port>/mcp``. :func:`collect_tools` lists the tools of
every endpoint and produces OpenAI-format tool specs plus name->callable
wrappers compatible with :func:`eval.harness.run_one`.

Every call opens a short-lived MCP session (initialize, call, close). On
localhost that costs a few milliseconds, keeps the callables free of
session lifecycle, and makes a service restart between ticks harmless.

Run as a script to health-check the services (exit 1 if any is down)::

    python -m eval.auto.mcp_client [--wait SECONDS] [URL ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

ToolFn = Callable[..., Awaitable[dict[str, Any]]]
DEFAULT_ENDPOINTS = (
    "http://127.0.0.1:8001/mcp",  # juju-docs-mcp
    "http://127.0.0.1:8002/mcp",  # juju-ops-mcp
    "http://127.0.0.1:8003/mcp",  # cos-metrics-mcp
)
COS_ENDPOINT = DEFAULT_ENDPOINTS[2]


@asynccontextmanager
async def _session(http: httpx.AsyncClient, url: str) -> AsyncIterator[ClientSession]:
    """Open and initialize an MCP session on ``url`` over ``http``.

    The caller's client is reused (not closed), so its timeout policy
    applies to MCP requests too.
    """
    async with (
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def _describe(exc: BaseException) -> str:
    """One-line description of ``exc``, unwrapping task-group exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"


def _result_to_dict(result: CallToolResult) -> dict[str, Any]:
    """Convert an MCP tool result to the tool's own response dict.

    The servers' tools all return dicts, which FastMCP sends as
    ``structuredContent`` (and as JSON text for older clients). Tool
    exceptions arrive as ``isError`` results with the message as text.
    """
    text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
    if result.isError:
        return {"ok": False, "reason": text or "tool returned an error"}
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"ok": True, "result": text}
    return parsed if isinstance(parsed, dict) else {"ok": True, "result": parsed}


def _tool_parameters(input_schema: dict[str, Any]) -> dict[str, Any]:
    """Turn an MCP tool ``inputSchema`` into an OpenAI ``parameters`` object."""
    params = dict(input_schema)
    # Pydantic's model title is noise to chat-completions backends.
    params.pop("title", None)
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return params


def _make_caller(url: str, tool_name: str, http: httpx.AsyncClient) -> ToolFn:
    """Build the async callable that invokes ``tool_name`` on ``url``.

    Transport failures come back as ``{"ok": False, "reason": ...}`` rather
    than raising, so one bad call doesn't abort a whole harness run.
    """

    async def call(**kwargs: Any) -> dict[str, Any]:
        try:
            async with _session(http, url) as session:
                result = await session.call_tool(tool_name, kwargs)
        except Exception as e:  # noqa: BLE001 - report every failure to the model
            return {"ok": False, "reason": f"MCP {tool_name} at {url}: {_describe(e)}"}
        return _result_to_dict(result)

    call.__name__ = tool_name
    return call


async def collect_tools(
    http: httpx.AsyncClient,
    endpoints: tuple[str, ...] = DEFAULT_ENDPOINTS,
) -> tuple[list[dict[str, Any]], dict[str, ToolFn]]:
    """Discover tools across all configured MCP endpoints.

    Returns ``(specs, fns)`` where ``specs`` is a list of OpenAI-format
    tool dicts (suitable for the ``tools=`` field on a chat-completions
    request) and ``fns`` is a name->callable map (each callable invokes
    the tool on the endpoint that listed it).

    Raises ``RuntimeError`` if any endpoint is unreachable. The
    autoscaler relies on all three services being up; we'd rather
    fail fast at startup than discover a missing service mid-tick.
    """
    specs: list[dict[str, Any]] = []
    fns: dict[str, ToolFn] = {}

    for url in endpoints:
        try:
            async with _session(http, url) as session:
                listed = await session.list_tools()
        except Exception as e:  # noqa: BLE001 - re-raised with context
            raise RuntimeError(
                f"MCP endpoint {url} not reachable ({_describe(e)}). "
                f"Is the systemd service up? Try: systemctl --user status "
                f"juju-docs-mcp juju-ops-mcp cos-metrics-mcp"
            ) from e

        for tool in listed.tools:
            if tool.name in fns:
                # The three servers are disjoint by design; keep the first.
                continue
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": (tool.description or "").strip(),
                        "parameters": _tool_parameters(tool.inputSchema),
                    },
                }
            )
            fns[tool.name] = _make_caller(url, tool.name, http)

    return specs, fns


async def get_scale_signal(
    http: httpx.AsyncClient, app: str = "postgresql",
    cos_endpoint: str = COS_ENDPOINT,
) -> dict[str, Any]:
    """Direct caller for cos-metrics' ``get_scale_signal``.

    Used by the autoscaler driver's ``evaluate_breach()`` to compute the
    BREACH hint without going through the model's tool dispatch. Same
    contract as :func:`cos_metrics.tools.get_scale_signal` (returns the
    full ``signals_evaluated`` payload), but reaches the tool through the
    cos-metrics-mcp service like every other tool call.
    """
    return await _make_caller(cos_endpoint, "get_scale_signal", http)(app=app)


async def check_endpoints(
    endpoints: tuple[str, ...] = DEFAULT_ENDPOINTS, wait_s: float = 0.0,
) -> bool:
    """Health check: list tools on each endpoint, retrying for up to ``wait_s``.

    Prints one line per endpoint; returns False if any stays unreachable.
    """
    deadline = time.monotonic() + wait_s
    async with httpx.AsyncClient(timeout=10) as http:
        for url in endpoints:
            while True:
                try:
                    specs, _ = await collect_tools(http, (url,))
                except RuntimeError as e:
                    if time.monotonic() >= deadline:
                        print(f"ERROR: {e}", file=sys.stderr)
                        return False
                    await asyncio.sleep(2)
                    continue
                print(f"{url}: {len(specs)} tools")
                break
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m eval.auto.mcp_client",
        description="Check that the MCP services answer tools/list over streamable HTTP.",
    )
    ap.add_argument(
        "endpoints", nargs="*", default=list(DEFAULT_ENDPOINTS),
        help="MCP endpoint URLs (default: the three local services)",
    )
    ap.add_argument(
        "--wait", type=float, default=0.0, metavar="SECONDS",
        help="keep retrying each endpoint for up to this long (default: 0)",
    )
    args = ap.parse_args(argv)
    ok = asyncio.run(check_endpoints(tuple(args.endpoints), args.wait))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
