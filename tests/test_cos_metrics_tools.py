"""Unit tests for cos_metrics.tools — Prometheus client + scale-signal evaluator.

Prometheus HTTP responses are mocked with respx so the tests are hermetic.
The policy is built by hand here; the YAML loader and the shipped policy
files are covered in test_cos_metrics_policy.py.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from cos_metrics import tools
from cos_metrics.policy import (
    AppMetricsPolicy,
    CosMetricsPolicy,
    Signal,
)

PROM = "http://prom.test:9090"


@pytest.fixture
def policy() -> CosMetricsPolicy:
    """Hand-built policy with Prometheus URL pointed at the respx mock."""
    return CosMetricsPolicy(
        prometheus_url=PROM,
        apps={
            "postgresql": AppMetricsPolicy(
                name="postgresql",
                breach_window="5m",
                breach_window_s=300,
                min_replicas=1,
                max_replicas=5,
                step_up=1,
                step_down=1,
                signals={
                    "cpu": Signal(
                        name="cpu",
                        promql='avg(rate(node_cpu{juju_application="postgresql"}[5m]))',
                        threshold_high=0.80,
                        threshold_low=0.20,
                    ),
                },
            ),
            "api": AppMetricsPolicy(
                name="api",
                breach_window="5m",
                breach_window_s=300,
                min_replicas=1,
                max_replicas=6,
                step_up=1,
                step_down=1,
                signals={
                    "cpu": Signal(
                        name="cpu",
                        promql='cpu_api',
                        threshold_high=0.80,
                        threshold_low=0.20,
                    ),
                    "qps": Signal(
                        name="qps",
                        promql='qps_api',
                        threshold_high=200.0,
                        threshold_low=20.0,
                    ),
                },
            ),
        },
    )


# Helpers to build mock Prometheus responses --------------------------------

def _matrix_response(values_per_series: list[list[tuple[float, float]]]) -> dict:
    """Build a Prometheus matrix-style query_range response."""
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"juju_application": "x"},
                 "values": [[ts, str(v)] for ts, v in series]}
                for series in values_per_series
            ],
        },
    }


def _vector_response(value: float) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {}, "value": [time.time(), str(value)]},
            ],
        },
    }


def _mostly_high_samples(threshold: float) -> list[tuple[float, float]]:
    """30 samples, 80% above threshold."""
    now = int(time.time())
    return [(now - 15 * (29 - i), threshold + 0.1 if i >= 6 else threshold - 0.5)
            for i in range(30)]


def _mostly_low_samples(threshold: float) -> list[tuple[float, float]]:
    now = int(time.time())
    return [(now - 15 * (29 - i), threshold - 0.1 if i >= 6 else threshold + 0.5)
            for i in range(30)]


def _no_breach_samples(low: float, high: float) -> list[tuple[float, float]]:
    """30 samples, all in the comfortable middle."""
    mid = (low + high) / 2
    now = int(time.time())
    return [(now - 15 * (29 - i), mid) for i in range(30)]


# ---------------------------------------------------------------------------
# prom_query / prom_query_range escape hatches
# ---------------------------------------------------------------------------

@respx.mock
async def test_prom_query_returns_data(policy):
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(0.42)),
    )
    out = await tools.prom_query("up", policy)
    assert out["ok"] is True
    assert out["result"]["resultType"] == "vector"


@respx.mock
async def test_prom_query_empty_query_rejected(policy):
    out = await tools.prom_query("", policy)
    assert out["ok"] is False
    assert "required" in out["reason"]


@respx.mock
async def test_prom_query_propagates_http_500(policy):
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(500, text="kaboom"),
    )
    out = await tools.prom_query("up", policy)
    assert out["ok"] is False
    assert "500" in out["reason"]


@respx.mock
async def test_prom_query_handles_network_error(policy):
    respx.get(f"{PROM}/api/v1/query").mock(side_effect=httpx.ConnectError("nope"))
    out = await tools.prom_query("up", policy)
    assert out["ok"] is False


@respx.mock
async def test_prom_query_range_passes_params(policy):
    route = respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([])),
    )
    out = await tools.prom_query_range("up", "1000", "1300", "15s", policy)
    assert out["ok"] is True
    assert route.called
    sent = route.calls.last.request.url.params
    assert sent["query"] == "up"
    assert sent["start"] == "1000"
    assert sent["end"] == "1300"
    assert sent["step"] == "15s"


# ---------------------------------------------------------------------------
# cos_active_alerts
# ---------------------------------------------------------------------------

@respx.mock
async def test_active_alerts_returns_all(policy):
    respx.get(f"{PROM}/api/v1/alerts").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "data": {"alerts": [
                {"labels": {"alertname": "HighCPU", "severity": "critical"}},
                {"labels": {"alertname": "DiskNoise", "severity": "info"}},
            ]},
        }),
    )
    out = await tools.cos_active_alerts(None, policy)
    assert out["ok"] is True
    assert len(out["alerts"]) == 2


@respx.mock
async def test_active_alerts_severity_filter(policy):
    respx.get(f"{PROM}/api/v1/alerts").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "data": {"alerts": [
                {"labels": {"alertname": "A", "severity": "critical"}},
                {"labels": {"alertname": "B", "severity": "info"}},
            ]},
        }),
    )
    out = await tools.cos_active_alerts("critical", policy)
    assert [a["labels"]["alertname"] for a in out["alerts"]] == ["A"]


# ---------------------------------------------------------------------------
# get_scale_signal — the headline tool
# ---------------------------------------------------------------------------

@respx.mock
async def test_scale_signal_recommends_up_on_high_breach(policy):
    """Single signal mostly above threshold_high => recommend +step_up."""
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([
            _mostly_high_samples(0.80),
        ])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["ok"] is True, out
    assert out["current_replicas"] == 3
    assert out["recommended_replicas"] == 4
    assert out["delta"] == 1
    assert "threshold_high" in out["reason"]


@respx.mock
async def test_scale_signal_recommends_down_when_all_signals_low(policy):
    """All signals below threshold_low => recommend -step_down."""
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([
            _mostly_low_samples(0.20),
        ])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["ok"] is True, out
    assert out["current_replicas"] == 3
    assert out["recommended_replicas"] == 2
    assert "threshold_low" in out["reason"]


@respx.mock
async def test_scale_signal_no_change_when_quiet(policy):
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([
            _no_breach_samples(0.20, 0.80),
        ])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["ok"] is True
    assert out["recommended_replicas"] == 3
    assert out["delta"] == 0
    assert out["reason"] == "no signal breach; holding"


@respx.mock
async def test_scale_signal_clamps_at_max(policy):
    """current=5, max=5 -> recommendation can't exceed 5."""
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([
            _mostly_high_samples(0.80),
        ])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(5)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["recommended_replicas"] == 5
    assert out["delta"] == 0


@respx.mock
async def test_scale_signal_clamps_at_min(policy):
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([
            _mostly_low_samples(0.20),
        ])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(1)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["recommended_replicas"] == 1
    assert out["delta"] == 0


@respx.mock
async def test_scale_signal_unknown_app_rejected(policy):
    out = await tools.get_scale_signal("rogue-app", policy)
    assert out["ok"] is False
    assert "no scale-signal recipe" in out["reason"]


@respx.mock
async def test_scale_signal_two_signals_one_high_recommends_up(policy):
    """For 'api' app with 2 signals: cpu high, qps quiet => still scale up
    (any signal high is enough)."""
    # cpu series: high; qps series: middle
    def respond(request):
        q = request.url.params.get("query", "")
        if "cpu_api" in q:
            return httpx.Response(200, json=_matrix_response([
                _mostly_high_samples(0.80),
            ]))
        if "qps_api" in q:
            return httpx.Response(200, json=_matrix_response([
                _no_breach_samples(20.0, 200.0),
            ]))
        return httpx.Response(200, json=_matrix_response([]))
    respx.get(f"{PROM}/api/v1/query_range").mock(side_effect=respond)
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(2)),
    )
    out = await tools.get_scale_signal("api", policy)
    assert out["ok"] is True
    assert out["recommended_replicas"] == 3   # 2 + step_up=1
    assert out["delta"] == 1


@respx.mock
async def test_scale_signal_two_signals_only_one_low_holds(policy):
    """ONE signal low + one quiet => DO NOT scale down (need ALL signals low)."""
    def respond(request):
        q = request.url.params.get("query", "")
        if "cpu_api" in q:
            return httpx.Response(200, json=_matrix_response([
                _mostly_low_samples(0.20),
            ]))
        if "qps_api" in q:
            return httpx.Response(200, json=_matrix_response([
                _no_breach_samples(20.0, 200.0),
            ]))
        return httpx.Response(200, json=_matrix_response([]))
    respx.get(f"{PROM}/api/v1/query_range").mock(side_effect=respond)
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("api", policy)
    assert out["ok"] is True
    assert out["recommended_replicas"] == 3   # held
    assert out["delta"] == 0


@respx.mock
async def test_scale_signal_prometheus_unreachable(policy):
    respx.get(f"{PROM}/api/v1/query_range").mock(
        side_effect=httpx.ConnectError("down"),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    # All signals fail to evaluate, but current_replicas still works
    assert out["ok"] is False
    assert "all signals failed" in out["reason"]


@respx.mock
async def test_scale_signal_no_replica_data(policy):
    """current_replicas can't be derived => return ok=False, surface why."""
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([
            _no_breach_samples(0.20, 0.80),
        ])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}}),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["ok"] is False
    assert out["reason"] == "no up{juju_application=postgresql} series"
    assert len(out["signals_evaluated"]) == 1


# ---------------------------------------------------------------------------
# Prometheus error shapes
# ---------------------------------------------------------------------------

@respx.mock
async def test_prom_query_surfaces_prometheus_error_status(policy):
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json={
            "status": "error", "errorType": "bad_data", "error": "parse error at char 3",
        }),
    )
    out = await tools.prom_query("up{", policy)
    assert out == {"ok": False, "reason": "prometheus /api/v1/query: parse error at char 3"}


@respx.mock
async def test_prom_query_rejects_non_json_body(policy):
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, content=b"<html>proxy error</html>"),
    )
    out = await tools.prom_query("up", policy)
    assert out == {"ok": False, "reason": "prometheus /api/v1/query: invalid JSON"}


@respx.mock
async def test_active_alerts_propagates_error(policy):
    respx.get(f"{PROM}/api/v1/alerts").mock(return_value=httpx.Response(502))
    out = await tools.cos_active_alerts(None, policy)
    assert out["ok"] is False
    assert "HTTP 502" in out["reason"]


# ---------------------------------------------------------------------------
# get_scale_signal edge cases
# ---------------------------------------------------------------------------

def _samples(values: list[float]) -> list[tuple[float, float]]:
    now = int(time.time())
    return [(now - 15 * (len(values) - 1 - i), v) for i, v in enumerate(values)]


@respx.mock
async def test_scale_signal_queries_policy_breach_window(policy):
    route = respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([
            _no_breach_samples(0.20, 0.80),
        ])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    params = route.calls.last.request.url.params
    assert int(params["end"]) - int(params["start"]) == 300
    assert params["step"] == "15s"
    assert out["breach_window_seconds"] == 300
    assert out["signals_failed"] == 0


@respx.mock
async def test_scale_signal_empty_matrix_is_a_failed_signal(policy):
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["ok"] is False
    assert out["reason"] == "all signals failed to evaluate"
    assert out["signals_evaluated"][0]["reason"] == "no series returned for this signal"


@respx.mock
async def test_scale_signal_unparseable_samples_is_a_failed_signal(policy):
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "data": {"resultType": "matrix", "result": [
                {"metric": {}, "values": [[time.time(), "abc"], ["x", "0.5"]]},
            ]},
        }),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["ok"] is False
    assert out["signals_evaluated"][0]["reason"] == "series had no parseable samples"


@respx.mock
async def test_scale_signal_unparseable_replica_count(policy):
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([
            _no_breach_samples(0.20, 0.80),
        ])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "abc"]}]},
        }),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["ok"] is False
    assert out["reason"].startswith("unparseable replica count")


@respx.mock
@pytest.mark.parametrize("n_high,expected_delta", [(15, 1), (14, 0)])
async def test_scale_signal_half_window_boundary(policy, n_high, expected_delta):
    """Exactly 50% of samples above threshold_high counts as a breach."""
    values = [0.9] * n_high + [0.5] * (30 - n_high)
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([_samples(values)])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["ok"] is True
    assert out["delta"] == expected_delta


@respx.mock
async def test_scale_signal_takes_max_across_series(policy):
    """Two series: one quiet, one hot. The hot one drives the decision."""
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_matrix_response([
            _no_breach_samples(0.20, 0.80),
            _mostly_high_samples(0.80),
        ])),
    )
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("postgresql", policy)
    assert out["delta"] == 1


@respx.mock
async def test_scale_signal_failed_signal_is_skipped_for_scale_down(policy):
    """One signal low + one failing => scale down on the signal that evaluated,
    and report the skipped one in signals_failed."""
    def respond(request):
        q = request.url.params.get("query", "")
        if "cpu_api" in q:
            return httpx.Response(200, json=_matrix_response([
                _mostly_low_samples(0.20),
            ]))
        return httpx.Response(200, json=_matrix_response([]))
    respx.get(f"{PROM}/api/v1/query_range").mock(side_effect=respond)
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_vector_response(3)),
    )
    out = await tools.get_scale_signal("api", policy)
    assert out["ok"] is True
    assert out["delta"] == -1
    assert out["signals_failed"] == 1
    assert out["reason"] == (
        "all 1 evaluated signals below threshold_low for 5m; "
        "skipped 1 signal(s) that failed to evaluate"
    )
    failed = [s for s in out["signals_evaluated"] if not s["ok"]]
    assert [s["name"] for s in failed] == ["qps"]
