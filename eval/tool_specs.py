"""Build OpenAI-compatible tool specs from FastMCP-registered tools.

Centralized so the eval harness — and any future client (e.g. an offline
benchmark, a cached snapshot in tests) — can pick up the same specs without
each one reaching into ``mcp._tool_manager._tools`` independently.

If FastMCP's internals shift, only this module needs to change.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from typing import Any

from juju_docs import server as jd
from juju_ops import server as jo

OpenAISpec = dict[str, Any]
ToolFn = Callable[..., Awaitable[Any]]


def _registered_tools(server: Any) -> list[Any]:
    """Pull the registered tools off a FastMCP server in a stable shape.

    FastMCP exposes tools through ``_tool_manager._tools``. Accessed via this
    helper so a future API change is a one-line fix.
    """
    tm = getattr(server, "_tool_manager", None)
    if tm is None:
        return []
    return list(tm._tools.values())  # noqa: SLF001


def _validated_caller(tool: Any) -> ToolFn:
    """Call ``tool`` through FastMCP's pydantic argument validation.

    Matches what an MCP client gets in production: ``"false"`` is coerced to
    ``False``, and bad or unknown arguments raise instead of reaching the
    tool function.
    """

    async def call(**kwargs: Any) -> Any:
        return await tool.run(kwargs)

    call.__name__ = tool.name
    return call


def _specs_from(server: Any) -> tuple[list[OpenAISpec], dict[str, ToolFn]]:
    specs: list[OpenAISpec] = []
    fns: dict[str, ToolFn] = {}
    for tool in _registered_tools(server):
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.parameters,
                },
            }
        )
        fns[tool.name] = _validated_caller(tool)
    return specs, fns


def collect() -> tuple[list[OpenAISpec], dict[str, ToolFn]]:
    """Return (openai_tool_specs, name->callable) for the eval harness.

    cos-metrics is skipped with a warning when its policy fails to load, so
    a missing cos policy doesn't take juju-ops and juju-docs down with it.
    """
    servers = [jd.mcp, jo.mcp]
    try:
        from cos_metrics import server as cs

        cs._policy()  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        print(f"cos-metrics not registered: {type(e).__name__}: {e}", file=sys.stderr)
    else:
        servers.append(cs.mcp)

    specs: list[OpenAISpec] = []
    fns: dict[str, ToolFn] = {}
    for srv in servers:
        srv_specs, srv_fns = _specs_from(srv)
        specs.extend(srv_specs)
        fns.update(srv_fns)
    return specs, fns
