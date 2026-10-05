"""Prometheus HTTP API client and scale-signal evaluator for cos-metrics.

The LLM never writes PromQL. ``get_scale_signal(app)`` looks up the recipes
declared in ``policy/cos-metrics.yaml`` for that app, evaluates each signal
over its ``breach_window`` via ``/api/v1/query_range``, and returns a
structured ``{current_replicas, recommended_replicas, reason, signals_evaluated}``
recommendation that an autoscale loop can act on.

Decision logic:
  - **Any** signal whose value spends >= 50% of the breach window above its
    ``threshold_high`` -> recommend ``current + step_up``, clamped at
    ``max_replicas``.
  - Conversely, **every signal that evaluated** must spend >= 50% below
    ``threshold_low`` before we recommend ``current - step_down``, clamped at
    ``min_replicas``. The asymmetry is intentional: easy to scale up under
    any pressure, hard to scale down.
  - No breach -> ``recommended_replicas == current_replicas``.

Signals that fail to evaluate (query error, no series, no parseable
samples) are skipped rather than blocking the decision, and are counted in
``signals_failed``. If every signal fails the result is ``ok=False``.

``current_replicas`` is read from Prometheus
(``count(up{juju_application=<app>} == 1)``); the autoscale loop should
never have to ask juju directly. If Prometheus has no data for the app we
return ``ok=False`` rather than guessing.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from .policy import CosMetricsPolicy, Signal

# Per-request httpx timeout; a slow Prometheus under load can exceed 10s.
DEFAULT_TIMEOUT_S = float(os.environ.get("COS_METRICS_TIMEOUT_S", "30.0"))


# ---------------------------------------------------------------------------
# Low-level Prometheus HTTP helpers
# ---------------------------------------------------------------------------

async def _prom_get(
    client: httpx.AsyncClient,
    base: str,
    path: str,
    params: dict[str, str],
) -> tuple[dict[str, Any] | None, str | None]:
    """GET <base><path>?<params>; return (data, error). Successful Prometheus
    responses have ``status: success`` with the payload under ``data``."""
    try:
        resp = await client.get(f"{base}{path}", params=params)
    except httpx.HTTPError as e:
        return None, f"prometheus {path}: {type(e).__name__}: {e}"
    if resp.status_code != 200:
        body = resp.text[:200]
        return None, f"prometheus {path}: HTTP {resp.status_code}: {body}"
    try:
        body = resp.json()
    except ValueError:
        return None, f"prometheus {path}: invalid JSON"
    if body.get("status") != "success":
        err = body.get("error") or body.get("errorType") or "unknown"
        return None, f"prometheus {path}: {err}"
    return body.get("data") or {}, None


async def _prom_call(
    policy: CosMetricsPolicy, path: str, params: dict[str, str]
) -> tuple[dict[str, Any] | None, str | None]:
    """One-shot :func:`_prom_get` with its own client."""
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
        return await _prom_get(client, policy.prometheus_url, path, params)


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------

async def prom_query(query: str, policy: CosMetricsPolicy) -> dict[str, Any]:
    """Run an instant PromQL query (escape hatch)."""
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "reason": "query is required"}
    data, err = await _prom_call(policy, "/api/v1/query", {"query": query})
    if err:
        return {"ok": False, "reason": err}
    return {"ok": True, "result": data}


async def prom_query_range(
    query: str, start: str, end: str, step: str, policy: CosMetricsPolicy
) -> dict[str, Any]:
    """Run a range PromQL query (escape hatch).

    ``start`` / ``end`` accept unix timestamps (string of int) or RFC3339;
    ``step`` is a duration like ``"15s"``.
    """
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "reason": "query is required"}
    data, err = await _prom_call(
        policy, "/api/v1/query_range",
        {"query": query, "start": str(start), "end": str(end), "step": str(step)},
    )
    if err:
        return {"ok": False, "reason": err}
    return {"ok": True, "result": data}


async def cos_active_alerts(
    severity: str | None, policy: CosMetricsPolicy
) -> dict[str, Any]:
    """List active Prometheus alerts (``state`` pending or firing), optionally
    filtered by severity. ``/api/v1/alerts`` returns both states unfiltered."""
    data, err = await _prom_call(policy, "/api/v1/alerts", {})
    if err:
        return {"ok": False, "reason": err}
    alerts = (data or {}).get("alerts") or []
    if severity:
        alerts = [
            a for a in alerts
            if (a.get("labels") or {}).get("severity") == severity
        ]
    return {"ok": True, "alerts": alerts}


# ---------------------------------------------------------------------------
# Scale signal computation — the headline tool
# ---------------------------------------------------------------------------

async def _eval_signal(
    signal: Signal,
    breach_window_s: int,
    client: httpx.AsyncClient,
    prom_url: str,
) -> dict[str, Any]:
    """Evaluate a single signal: range-query over breach_window, compute
    breach fractions and the most recent value."""
    end = int(time.time())
    start = end - breach_window_s
    # ~30 samples across the window, minimum 15s step
    step_s = max(15, breach_window_s // 30)
    data, err = await _prom_get(
        client,
        prom_url,
        "/api/v1/query_range",
        {
            "query": signal.promql,
            "start": str(start),
            "end": str(end),
            "step": f"{step_s}s",
        },
    )
    if err:
        return {"name": signal.name, "ok": False, "reason": err}

    result = (data or {}).get("result") or []
    if not result:
        return {
            "name": signal.name,
            "ok": False,
            "reason": "no series returned for this signal",
        }

    # Aggregate by taking max-across-series at each timestamp.
    # Worst-case for scale-up; safe-case for scale-down.
    by_ts: dict[float, float] = {}
    for series in result:
        for ts, val in series.get("values") or []:
            try:
                t = float(ts)
                v = float(val)
            except (TypeError, ValueError):
                continue
            if t not in by_ts or v > by_ts[t]:
                by_ts[t] = v
    if not by_ts:
        return {
            "name": signal.name,
            "ok": False,
            "reason": "series had no parseable samples",
        }

    samples_sorted = sorted(by_ts.items())
    last_value = samples_sorted[-1][1]
    total = len(samples_sorted)
    above_high = sum(1 for _, v in samples_sorted if v > signal.threshold_high)
    below_low = sum(1 for _, v in samples_sorted if v < signal.threshold_low)
    breach_high_fraction = above_high / total
    breach_low_fraction = below_low / total

    if breach_high_fraction >= 0.5:
        breach = "high"
    elif breach_low_fraction >= 0.5:
        breach = "low"
    else:
        breach = "none"

    return {
        "name": signal.name,
        "ok": True,
        "current_value": last_value,
        "threshold_high": signal.threshold_high,
        "threshold_low": signal.threshold_low,
        "breach": breach,
        "breach_high_fraction": round(breach_high_fraction, 3),
        "breach_low_fraction": round(breach_low_fraction, 3),
        "samples": total,
    }


async def _current_replicas(
    app: str, client: httpx.AsyncClient, prom_url: str
) -> tuple[int | None, str | None]:
    """Read current replica count from Prometheus.

    The naive ``count(up{juju_application=<app>} == 1)`` over-counts when
    each unit exposes multiple scrape targets (postgres_exporter +
    node_exporter + grafana-agent self = 3 series per unit, giving 9 for
    a 3-unit cluster). The right answer is **distinct ``juju_unit``
    labels** that have at least one ``up==1`` series:

        count(count by (juju_unit)(up{juju_application=<app>} == 1))
    """
    q = (
        f'count(count by (juju_unit) '
        f'(up{{juju_application="{app}"}} == 1))'
    )
    data, err = await _prom_get(client, prom_url, "/api/v1/query", {"query": q})
    if err:
        return None, err
    result = (data or {}).get("result") or []
    if not result:
        return None, f"no up{{juju_application={app}}} series"
    try:
        return int(float(result[0]["value"][1])), None
    except (KeyError, ValueError, IndexError, TypeError) as e:
        return None, f"unparseable replica count: {e}"


async def get_scale_signal(
    app: str, policy: CosMetricsPolicy
) -> dict[str, Any]:
    """Compute a scale recommendation for ``app`` from its policy recipes."""
    recipe = policy.apps.get(app)
    if recipe is None:
        return {
            "ok": False,
            "reason": (
                f"no scale-signal recipe for app {app!r}; "
                f"add it to policy/cos-metrics.yaml"
            ),
        }

    breach_window_s = recipe.breach_window_s

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
        signal_results: list[dict[str, Any]] = []
        for signal in recipe.signals.values():
            signal_results.append(
                await _eval_signal(
                    signal, breach_window_s, client, policy.prometheus_url
                )
            )
        current, current_err = await _current_replicas(
            app, client, policy.prometheus_url
        )

    if current is None:
        return {
            "ok": False,
            "reason": current_err or "could not determine current_replicas",
            "signals_evaluated": signal_results,
        }

    healthy_signals = [r for r in signal_results if r.get("ok")]
    signals_failed = len(signal_results) - len(healthy_signals)
    if not healthy_signals:
        return {
            "ok": False,
            "reason": "all signals failed to evaluate",
            "signals_evaluated": signal_results,
            "current_replicas": current,
        }

    breach_high = [r for r in healthy_signals if r["breach"] == "high"]
    breach_low = [r for r in healthy_signals if r["breach"] == "low"]
    all_below = breach_low and len(breach_low) == len(healthy_signals)

    if breach_high:
        recommended = min(current + recipe.step_up, recipe.max_replicas)
        leader = max(breach_high, key=lambda r: r["breach_high_fraction"])
        reason = (
            f"signal {leader['name']!r} breached threshold_high "
            f"({leader['breach_high_fraction']:.0%} of {recipe.breach_window})"
        )
    elif all_below:
        recommended = max(current - recipe.step_down, recipe.min_replicas)
        reason = (
            f"all {len(healthy_signals)} evaluated signals below threshold_low "
            f"for {recipe.breach_window}"
        )
        if signals_failed:
            reason += f"; skipped {signals_failed} signal(s) that failed to evaluate"
    else:
        recommended = current
        reason = "no signal breach; holding"

    # Bound clamp: recommended is already clamped above; if current itself
    # is outside bounds, surface that as a flag.
    out_of_bounds = current < recipe.min_replicas or current > recipe.max_replicas

    return {
        "ok": True,
        "app": app,
        "current_replicas": current,
        "recommended_replicas": recommended,
        "delta": recommended - current,
        "reason": reason,
        "breach_window": recipe.breach_window,
        "breach_window_seconds": breach_window_s,
        "signals_evaluated": signal_results,
        "signals_failed": signals_failed,
        "bounds": {
            "min_replicas": recipe.min_replicas,
            "max_replicas": recipe.max_replicas,
        },
        "step": {"up": recipe.step_up, "down": recipe.step_down},
        "out_of_bounds": out_of_bounds,
    }


__all__ = [
    "cos_active_alerts",
    "get_scale_signal",
    "prom_query",
    "prom_query_range",
]
