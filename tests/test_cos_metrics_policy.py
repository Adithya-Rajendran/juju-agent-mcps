"""Tests for cos_metrics.policy — schema validation and the shipped YAMLs."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cos_metrics.policy import (
    CosMetricsPolicyError,
    load_policy,
    parse_duration,
)
from juju_ops.policy import load_policy as load_juju_ops_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
COS_POLICY_FILES = ["cos-metrics.yaml", "cos-metrics-demo.yaml"]


def _write_policy(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "cos-metrics.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def _app_body(**overrides) -> dict:
    body = {
        "min_replicas": 1,
        "max_replicas": 3,
        "signals": {
            "s": {"promql": "up", "threshold_high": 1.0, "threshold_low": 0.5},
        },
    }
    body.update(overrides)
    return body


def test_loads_repo_default_policy(monkeypatch) -> None:
    monkeypatch.setenv(
        "COS_METRICS_POLICY", str(REPO_ROOT / "policy" / "cos-metrics.yaml"),
    )
    policy = load_policy()
    assert policy.prometheus_url.startswith("http")
    assert "api" in policy.apps
    api = policy.apps["api"]
    assert api.min_replicas == 1
    assert api.max_replicas == 6
    assert api.breach_window_s == 300
    assert "cpu_saturation" in api.signals
    assert api.signals["cpu_saturation"].threshold_high > api.signals["cpu_saturation"].threshold_low


@pytest.mark.parametrize("name", COS_POLICY_FILES)
def test_replica_bounds_match_juju_ops_policy(name, monkeypatch) -> None:
    """get_scale_signal must never recommend a count juju-ops would refuse."""
    monkeypatch.delenv("JUJU_OPS_DEFAULT_MODEL", raising=False)
    cos = load_policy(REPO_ROOT / "policy" / name)
    ops = load_juju_ops_policy(REPO_ROOT / "policy" / "juju-ops.yaml")
    assert "postgresql" in cos.apps
    for app, recipe in cos.apps.items():
        assert app in ops.apps, f"{name}: {app} missing from juju-ops.yaml"
        bounds = (recipe.min_replicas, recipe.max_replicas)
        assert bounds == (ops.apps[app].min_units, ops.apps[app].max_units), app


def test_missing_file_raises(tmp_path) -> None:
    with pytest.raises(CosMetricsPolicyError):
        load_policy(tmp_path / "missing.yaml")


def test_rejects_top_level_non_mapping(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(CosMetricsPolicyError):
        load_policy(p)


def test_requires_schema_version(tmp_path) -> None:
    p = _write_policy(tmp_path, {"prometheus": {"url": "http://x"}, "applications": {}})
    with pytest.raises(CosMetricsPolicyError, match="schema_version"):
        load_policy(p)


@pytest.mark.parametrize("version", [2, 99, "1", True])
def test_rejects_unsupported_schema_version(tmp_path, version) -> None:
    p = _write_policy(tmp_path, {
        "schema_version": version,
        "prometheus": {"url": "http://x"},
        "applications": {},
    })
    with pytest.raises(CosMetricsPolicyError, match="schema_version"):
        load_policy(p)


def test_requires_prometheus_url(tmp_path) -> None:
    p = _write_policy(tmp_path, {"schema_version": 1, "applications": {}})
    with pytest.raises(CosMetricsPolicyError, match="prometheus.url"):
        load_policy(p)


def test_env_overrides_prometheus_url(tmp_path, monkeypatch) -> None:
    p = _write_policy(tmp_path, {
        "schema_version": 1,
        "prometheus": {"url": "http://from-yaml"},
        "applications": {},
    })
    monkeypatch.setenv("COS_PROM_URL", "http://from-env")
    policy = load_policy(p)
    assert policy.prometheus_url == "http://from-env"


def test_rejects_min_above_max(tmp_path) -> None:
    p = _write_policy(tmp_path, {
        "schema_version": 1,
        "prometheus": {"url": "http://x"},
        "applications": {"api": _app_body(min_replicas=5, max_replicas=1)},
    })
    with pytest.raises(CosMetricsPolicyError, match="min_replicas"):
        load_policy(p)


def test_rejects_signal_with_inverted_thresholds(tmp_path) -> None:
    p = _write_policy(tmp_path, {
        "schema_version": 1,
        "prometheus": {"url": "http://x"},
        "applications": {
            "api": _app_body(signals={
                "s": {"promql": "up", "threshold_high": 0.2, "threshold_low": 0.9},
            }),
        },
    })
    with pytest.raises(CosMetricsPolicyError, match="threshold_low"):
        load_policy(p)


# ---------------------------------------------------------------------------
# breach_window parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dur,expected", [
    ("5m", 300),
    ("30s", 30),
    ("2h", 7200),
    ("1d", 86400),
    ("1w", 604800),
    ("1m30s", 90),
    ("1h5m", 3900),
    ("", None),
    ("garbage", None),
    ("Xm", None),
    ("5", None),
    ("5M", None),
    ("5min", None),
    ("-5m", None),
    ("0m", None),
    (" 5m", None),
])
def test_parse_duration(dur, expected) -> None:
    assert parse_duration(dur) == expected


def test_breach_window_parsed_at_load(tmp_path) -> None:
    p = _write_policy(tmp_path, {
        "schema_version": 1,
        "prometheus": {"url": "http://x"},
        "applications": {"api": _app_body(breach_window="1m30s")},
    })
    api = load_policy(p).apps["api"]
    assert api.breach_window == "1m30s"
    assert api.breach_window_s == 90


@pytest.mark.parametrize("bad", ["5min", "5M", "0s", "", 300, None])
def test_rejects_malformed_breach_window(tmp_path, bad) -> None:
    p = _write_policy(tmp_path, {
        "schema_version": 1,
        "prometheus": {"url": "http://x"},
        "applications": {"api": _app_body(breach_window=bad)},
    })
    with pytest.raises(CosMetricsPolicyError, match="breach_window"):
        load_policy(p)
