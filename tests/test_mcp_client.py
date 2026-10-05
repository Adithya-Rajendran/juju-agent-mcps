"""Tests for eval/auto/mcp_client.py: the autoscaler's MCP-over-HTTP client.

A throwaway FastMCP server is served in-process through httpx's ASGI
transport, so the client speaks the real streamable-HTTP MCP protocol
without opening a socket.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from eval.auto import mcp_client

URL = "http://127.0.0.1:8003/mcp"
DOWN_URL = "http://127.0.0.1:1/mcp"


def _demo_server() -> FastMCP:
    mcp = FastMCP("demo")

    @mcp.tool()
    async def get_scale_signal(app: str) -> dict[str, Any]:
        """Scale signal for an app."""
        return {"ok": True, "app": app, "signals_evaluated": []}

    @mcp.tool()
    async def explode() -> dict[str, Any]:
        """Always fails."""
        raise RuntimeError("boom")

    return mcp


@contextlib.asynccontextmanager
async def _served() -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client whose requests to 127.0.0.1:8003 hit the demo server.

    A context manager rather than a fixture: the session manager's task
    group must be entered and exited in the same task.
    """
    server = _demo_server()
    app = server.streamable_http_app()
    asgi = httpx.ASGITransport(app=app)
    mounts = {"http://127.0.0.1:8003": asgi}
    async with (
        server.session_manager.run(),
        httpx.AsyncClient(mounts=mounts, timeout=10) as client,
    ):
        yield client


async def test_collect_tools_builds_openai_specs() -> None:
    async with _served() as http:
        specs, fns = await mcp_client.collect_tools(http, (URL,))
        by_name = {s["function"]["name"]: s for s in specs}
        assert set(by_name) == set(fns) == {"get_scale_signal", "explode"}
        spec = by_name["get_scale_signal"]
        assert spec["type"] == "function"
        assert spec["function"]["description"] == "Scale signal for an app."
        params = spec["function"]["parameters"]
        assert params["type"] == "object"
        assert params["required"] == ["app"]
        assert "title" not in params


async def test_collect_tools_keeps_first_duplicate() -> None:
    async with _served() as http:
        specs, _ = await mcp_client.collect_tools(http, (URL, URL))
        assert len(specs) == 2


async def test_tool_callable_returns_tool_dict() -> None:
    async with _served() as http:
        _, fns = await mcp_client.collect_tools(http, (URL,))
        assert await fns["get_scale_signal"](app="pg") == {
            "ok": True, "app": "pg", "signals_evaluated": [],
        }


async def test_tool_error_becomes_not_ok() -> None:
    async with _served() as http:
        _, fns = await mcp_client.collect_tools(http, (URL,))
        result = await fns["explode"]()
        assert result["ok"] is False
        assert "boom" in result["reason"]


async def test_get_scale_signal_uses_cos_endpoint() -> None:
    async with _served() as http:
        result = await mcp_client.get_scale_signal(http, app="postgresql", cos_endpoint=URL)
        assert result["app"] == "postgresql"


async def test_unreachable_endpoint() -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        with pytest.raises(RuntimeError, match="not reachable"):
            await mcp_client.collect_tools(client, (DOWN_URL,))
        result = await mcp_client.get_scale_signal(client, cos_endpoint=DOWN_URL)
    assert result["ok"] is False
    assert "ConnectError" in result["reason"]


async def test_check_endpoints_reports_down(capsys) -> None:
    assert await mcp_client.check_endpoints((DOWN_URL,)) is False
    assert "not reachable" in capsys.readouterr().err


def test_main_exit_code(monkeypatch) -> None:
    seen = []

    async def fake_check(endpoints, wait_s):
        seen.append((endpoints, wait_s))
        return endpoints == mcp_client.DEFAULT_ENDPOINTS

    monkeypatch.setattr(mcp_client, "check_endpoints", fake_check)
    assert mcp_client.main([]) == 0
    assert mcp_client.main(["--wait", "3", DOWN_URL]) == 1
    assert seen == [(mcp_client.DEFAULT_ENDPOINTS, 0.0), ((DOWN_URL,), 3.0)]


def _result(**kw: Any) -> CallToolResult:
    return CallToolResult(**kw)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_result(content=[], structuredContent={"ok": True}), {"ok": True}),
        (_result(content=[TextContent(type="text", text='{"a": 1}')]), {"a": 1}),
        (_result(content=[TextContent(type="text", text="[1]")]), {"ok": True, "result": [1]}),
        (_result(content=[TextContent(type="text", text="hi")]), {"ok": True, "result": "hi"}),
        (_result(content=[], isError=True), {"ok": False, "reason": "tool returned an error"}),
    ],
)
def test_result_to_dict(result, expected) -> None:
    assert mcp_client._result_to_dict(result) == expected  # noqa: SLF001


def test_describe_unwraps_exception_groups() -> None:
    err = ExceptionGroup("tg", [ExceptionGroup("inner", [ValueError("x")])])
    assert mcp_client._describe(err) == "ValueError: x"  # noqa: SLF001
