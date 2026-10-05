"""Tests for cos_metrics.server — tool registration, LLM-facing descriptions,
lazy policy loading and the tool -> tools.* glue."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from cos_metrics import server as srv
from cos_metrics.policy import CosMetricsPolicyError

REPO_ROOT = Path(__file__).resolve().parent.parent
PROM = "http://prom.test:9090"


@pytest.fixture
def loaded_policy(monkeypatch):
    """Load the shipped policy with Prometheus pointed at the respx mock."""
    monkeypatch.setenv("COS_METRICS_POLICY", str(REPO_ROOT / "policy" / "cos-metrics.yaml"))
    monkeypatch.setenv("COS_PROM_URL", PROM)
    monkeypatch.setattr(srv, "_POLICY", None)
    return srv._policy()  # noqa: SLF001


def _tools() -> dict:
    return {t.name: t for t in srv.mcp._tool_manager.list_tools()}  # noqa: SLF001


def test_all_tools_registered_have_descriptions() -> None:
    tools = _tools()
    assert set(tools) == {
        "get_scale_signal", "cos_active_alerts", "prom_query", "prom_query_range",
    }
    for t in tools.values():
        assert t.description, f"{t.name} missing description"


def test_get_scale_signal_description_names_existing_juju_ops_tools() -> None:
    desc = _tools()["get_scale_signal"].description
    assert "juju_scale_application" not in desc
    assert "juju_add_unit(app, count=delta)" in desc
    assert "juju_remove_unit(unit)" in desc
    assert "signals_failed" in desc


def test_prom_query_range_description_has_no_relative_time_hint() -> None:
    desc = _tools()["prom_query_range"].description
    assert '"-5m"' not in desc


def test_policy_is_loaded_lazily_and_cached(loaded_policy) -> None:
    assert loaded_policy.prometheus_url == PROM
    assert srv._policy() is loaded_policy  # noqa: SLF001


def test_main_loads_policy_before_serving(monkeypatch) -> None:
    monkeypatch.setattr(srv, "_POLICY", None)
    monkeypatch.setenv("COS_METRICS_POLICY", "/nonexistent/cos-metrics.yaml")
    ran = []
    monkeypatch.setattr(srv.mcp, "run", lambda transport: ran.append(transport))
    with pytest.raises(CosMetricsPolicyError):
        srv.main([])
    assert not ran


def test_main_runs_server_with_valid_policy(monkeypatch, loaded_policy) -> None:
    ran = []
    monkeypatch.setattr(srv.mcp, "run", lambda transport: ran.append(transport))
    srv.main([])
    assert ran == ["stdio"]


@respx.mock
async def test_tools_use_loaded_policy(loaded_policy) -> None:
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json={
            "status": "success", "data": {"resultType": "vector", "result": []},
        }),
    )
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={
            "status": "success", "data": {"resultType": "matrix", "result": []},
        }),
    )
    respx.get(f"{PROM}/api/v1/alerts").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"alerts": []}}),
    )
    assert (await srv.prom_query("up"))["ok"] is True
    assert (await srv.prom_query_range("up", "1", "2"))["ok"] is True
    assert await srv.cos_active_alerts() == {"ok": True, "alerts": []}
    unknown = await srv.get_scale_signal("rogue-app")
    assert unknown["ok"] is False
    assert "no scale-signal recipe" in unknown["reason"]
