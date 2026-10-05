"""Command-line handling shared by the three servers' ``main()``.

Nothing is served: ``FastMCP.run`` is replaced with a recorder, and each
test works on a copy of the server's settings so the module-level FastMCP
instances are left untouched.
"""

from __future__ import annotations

import pytest

from cos_metrics import server as cos_server
from juju_docs import server as docs_server
from juju_ops import server as ops_server

SERVERS = [
    pytest.param((docs_server, "juju-docs-mcp", 8001), id="juju-docs"),
    pytest.param((ops_server, "juju-ops-mcp", 8002), id="juju-ops"),
    pytest.param((cos_server, "cos-metrics-mcp", 8003), id="cos-metrics"),
]


@pytest.fixture(params=SERVERS)
def server(request, monkeypatch):
    """(module, prog, default port, recorded run() transports)."""
    srv, prog, port = request.param
    if hasattr(srv, "_POLICY"):
        # Stand-in so main()'s eager policy load doesn't need a YAML.
        monkeypatch.setattr(srv, "_POLICY", object())
    monkeypatch.setattr(srv.mcp, "settings", srv.mcp.settings.model_copy(deep=True))
    monkeypatch.setattr(srv.mcp, "_session_manager", None)
    runs: list[str] = []
    monkeypatch.setattr(srv.mcp, "run", lambda transport: runs.append(transport))
    return srv, prog, port, runs


def test_parser_defaults(server) -> None:
    srv, prog, port, _ = server
    parser = srv.build_parser()
    assert parser.prog == prog
    args = parser.parse_args([])
    assert (args.transport, args.host, args.port) == ("stdio", "127.0.0.1", port)


def test_default_is_stdio_and_leaves_http_settings_alone(server) -> None:
    srv, _, _, runs = server
    before = srv.mcp.settings.model_dump()
    srv.main([])
    assert runs == ["stdio"]
    assert srv.mcp.settings.model_dump() == before


def test_streamable_http_on_loopback_keeps_dns_rebinding_protection(server) -> None:
    srv, _, _, runs = server
    srv.main(["--transport", "streamable-http", "--port", "9123"])
    assert runs == ["streamable-http"]
    settings = srv.mcp.settings
    assert (settings.host, settings.port) == ("127.0.0.1", 9123)
    assert settings.transport_security.enable_dns_rebinding_protection


def test_streamable_http_default_port(server) -> None:
    srv, _, port, _ = server
    srv.main(["--transport", "streamable-http"])
    assert srv.mcp.settings.port == port


def test_non_loopback_host_drops_localhost_allowlist_and_warns(server, capsys) -> None:
    srv, prog, _, runs = server
    srv.main(["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9000"])
    assert runs == ["streamable-http"]
    assert srv.mcp.settings.host == "0.0.0.0"
    assert srv.mcp.settings.transport_security is None
    err = capsys.readouterr().err
    assert prog in err
    assert "without authentication" in err


@pytest.mark.parametrize(
    "argv",
    [["--transport", "sse"], ["--port", "http"], ["--bogus"]],
)
def test_bad_arguments_exit(server, argv) -> None:
    srv, _, _, runs = server
    with pytest.raises(SystemExit) as exc:
        srv.main(argv)
    assert exc.value.code == 2
    assert not runs


def test_streamable_http_app_builds(server) -> None:
    srv, _, _, _ = server
    srv.main(["--transport", "streamable-http", "--port", "9124"])
    app = srv.mcp.streamable_http_app()
    assert [route.path for route in app.routes] == ["/mcp"]
