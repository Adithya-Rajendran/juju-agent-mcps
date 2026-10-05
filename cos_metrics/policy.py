"""Load and validate the cos-metrics policy YAML.

Mirrors :mod:`juju_ops.policy`: loaded once at server start, the schema is
verified strictly, and the LLM never sees the file. PromQL recipes are
keyed by ``(application, signal)`` and evaluated server-side; the LLM only
ever asks for ``get_scale_signal(app)`` and gets a structured recommendation.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY_PATH = "policy/cos-metrics.yaml"
SUPPORTED_SCHEMA_VERSION = 1

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_TOKEN_RE = re.compile(r"(\d+)([smhdw])")
_DURATION_RE = re.compile(r"(?:\d+[smhdw])+")


class CosMetricsPolicyError(ValueError):
    """Raised when the cos-metrics policy file is missing required fields."""


@dataclass(frozen=True)
class Signal:
    """A PromQL recipe for one (application, signal) pair.

    ``threshold_high`` is the breach-up boundary; if the signal stays
    above it for >=50% of the breach window the autoscaler recommends
    scaling up. ``threshold_low`` is the symmetric boundary for scale-down.
    """

    name: str
    promql: str
    threshold_high: float
    threshold_low: float


@dataclass(frozen=True)
class AppMetricsPolicy:
    """Per-application autoscale envelope and signal recipes.

    ``breach_window`` is a Prometheus duration string (``5m``, ``1m30s``);
    ``breach_window_s`` is the same window in seconds, parsed at load time.
    ``step_up`` / ``step_down`` are the unit deltas applied per breach;
    ``min_replicas`` / ``max_replicas`` clamp the result.
    """

    name: str
    breach_window: str
    breach_window_s: int
    min_replicas: int
    max_replicas: int
    step_up: int
    step_down: int
    signals: dict[str, Signal]


@dataclass(frozen=True)
class CosMetricsPolicy:
    """Loaded cos-metrics policy. The single source of truth for which
    PromQL the server evaluates and how to interpret the result.

    Constructed by :func:`load_policy`. The LLM never sees this object;
    it asks for ``get_scale_signal(app)`` and gets a structured
    recommendation back.
    """

    prometheus_url: str
    apps: dict[str, AppMetricsPolicy]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def parse_duration(text: str) -> int | None:
    """Convert a Prometheus duration (``5m``, ``1m30s``, ``2h``) to seconds.

    Returns None for anything that isn't one or more ``<int><unit>`` tokens
    with units ``s``/``m``/``h``/``d``/``w``, or that adds up to zero.
    """
    if not _DURATION_RE.fullmatch(text):
        return None
    total = sum(
        int(n) * _DURATION_UNITS[u] for n, u in _DURATION_TOKEN_RE.findall(text)
    )
    return total or None


def _coerce_signal(name: str, body: dict[str, Any], app: str) -> Signal:
    if not isinstance(body, dict):
        raise CosMetricsPolicyError(
            f"applications.{app}.signals.{name} must be a mapping"
        )
    promql = body.get("promql")
    if not isinstance(promql, str) or not promql.strip():
        raise CosMetricsPolicyError(
            f"applications.{app}.signals.{name}.promql must be a non-empty string"
        )
    th_hi = body.get("threshold_high")
    th_lo = body.get("threshold_low")
    if not isinstance(th_hi, (int, float)) or not isinstance(th_lo, (int, float)):
        raise CosMetricsPolicyError(
            f"applications.{app}.signals.{name}: threshold_high / threshold_low "
            "must be numeric"
        )
    if th_lo > th_hi:
        raise CosMetricsPolicyError(
            f"applications.{app}.signals.{name}: threshold_low ({th_lo}) > "
            f"threshold_high ({th_hi})"
        )
    return Signal(
        name=name,
        promql=promql,
        threshold_high=float(th_hi),
        threshold_low=float(th_lo),
    )


def _coerce_app(name: str, body: dict[str, Any]) -> AppMetricsPolicy:
    if not isinstance(body, dict):
        raise CosMetricsPolicyError(f"applications.{name} must be a mapping")
    bw = body.get("breach_window", "5m")
    bw_s = parse_duration(bw) if isinstance(bw, str) else None
    if bw_s is None:
        raise CosMetricsPolicyError(
            f"applications.{name}.breach_window must be a positive Prometheus "
            f"duration like '5m' or '1m30s' (got {bw!r})"
        )
    min_r = body.get("min_replicas", 1)
    max_r = body.get("max_replicas", 1)
    if not isinstance(min_r, int) or not isinstance(max_r, int):
        raise CosMetricsPolicyError(
            f"applications.{name}: min_replicas / max_replicas must be ints"
        )
    if min_r > max_r:
        raise CosMetricsPolicyError(
            f"applications.{name}: min_replicas ({min_r}) > max_replicas ({max_r})"
        )
    step_up = body.get("step_up", 1)
    step_down = body.get("step_down", 1)
    if not isinstance(step_up, int) or step_up < 1:
        raise CosMetricsPolicyError(
            f"applications.{name}.step_up must be an int >= 1"
        )
    if not isinstance(step_down, int) or step_down < 1:
        raise CosMetricsPolicyError(
            f"applications.{name}.step_down must be an int >= 1"
        )
    signals_raw = body.get("signals") or {}
    if not isinstance(signals_raw, dict) or not signals_raw:
        raise CosMetricsPolicyError(
            f"applications.{name}.signals must be a non-empty mapping"
        )
    signals = {
        s_name: _coerce_signal(s_name, s_body, name)
        for s_name, s_body in signals_raw.items()
    }
    return AppMetricsPolicy(
        name=name,
        breach_window=bw,
        breach_window_s=bw_s,
        min_replicas=int(min_r),
        max_replicas=int(max_r),
        step_up=int(step_up),
        step_down=int(step_down),
        signals=signals,
    )


def load_policy(
    path: str | os.PathLike[str] | None = None,
) -> CosMetricsPolicy:
    """Load and validate a cos-metrics policy YAML.

    Resolution order:
      1. explicit ``path`` argument
      2. ``$COS_METRICS_POLICY`` env var
      3. ``./policy/cos-metrics.yaml`` relative to the cwd
    """
    p = Path(
        path
        or os.environ.get("COS_METRICS_POLICY")
        or DEFAULT_POLICY_PATH
    )
    if not p.exists():
        raise CosMetricsPolicyError(f"policy file not found at {p}")

    with p.open() as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise CosMetricsPolicyError(
            f"policy file {p} must be a YAML mapping at top level"
        )

    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != SUPPORTED_SCHEMA_VERSION:
        raise CosMetricsPolicyError(
            f"schema_version is required and must be {SUPPORTED_SCHEMA_VERSION} "
            f"(got {schema_version!r})"
        )

    prom_raw = raw.get("prometheus") or {}
    if not isinstance(prom_raw, dict):
        raise CosMetricsPolicyError("prometheus must be a mapping")
    url = os.environ.get("COS_PROM_URL") or prom_raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise CosMetricsPolicyError(
            "prometheus.url is required (set in policy or via COS_PROM_URL)"
        )

    apps_raw = raw.get("applications") or {}
    if not isinstance(apps_raw, dict):
        raise CosMetricsPolicyError("applications must be a mapping")
    apps = {name: _coerce_app(name, body) for name, body in apps_raw.items()}

    return CosMetricsPolicy(prometheus_url=url, apps=apps)


__all__ = [
    "AppMetricsPolicy",
    "CosMetricsPolicy",
    "CosMetricsPolicyError",
    "Signal",
    "load_policy",
    "parse_duration",
]
